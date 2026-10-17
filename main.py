from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import inspect
import math
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .binding_store import (
    BindCooldownActive,
    BindStatus,
    BindingDatabaseError,
    MySQLBindingStore,
)
from .game_store import (
    GameCooldownActive,
    GameDatabaseError,
    GameOperation,
    GameOperationConflict,
    GameStatus,
    MySQLGameStore,
    roll_success,
)
from .reward_store import (
    MIN_RED_PACKET_UNITS,
    CheckinReservation,
    DailyRewardStats,
    MySQLRewardStore,
    RedPacketClaimReservation,
    RedPacketUnavailable,
    RewardBudgetExhausted,
    RewardDatabaseError,
    RewardJob,
    RewardJobStatus,
    money_to_units,
    units_to_decimal,
)
from .tokenrouter_client import (
    TokenRouterClient,
    TokenRouterDashboardOverview,
    TokenRouterError,
    TokenRouterUserRanking,
    TokenRouterUserRPMStatus,
    TokenRouterUser,
    is_insufficient_balance_error,
)


_SUPPORTED_QQ_PLATFORMS = frozenset({"aiocqhttp", "qq_official"})
_GAME_REFUND_MAX_ATTEMPTS = 5
_REPLY_MARKUP_TRANSLATION = str.maketrans(
    {
        "\\": "＼",
        "`": "｀",
        "*": "＊",
        "_": "＿",
        "{": "｛",
        "}": "｝",
        "[": "［",
        "]": "］",
        "(": "（",
        ")": "）",
        "#": "＃",
        "!": "！",
        "|": "｜",
        ">": "＞",
        "<": "＜",
        "~": "～",
        "$": "＄",
    }
)


@dataclass(frozen=True, slots=True)
class GroupCommandContext:
    platform: str
    sender_id: str
    group_id: str


@dataclass(frozen=True, slots=True)
class PrivateAdminContext:
    platform: str
    sender_id: str


@register(
    "astrbot_plugin_fapi",
    "FAPI",
    "QQ 与 TokenRouter 账号互通、签到、群红包和打劫",
    "4.1.9",
)
class FapiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._allowed_group_id = str(config.get("allowed_group_id", "")).strip()
        self._admin_private_enabled = bool(
            config.get("admin_private_enabled", True)
        )
        self._admin_config_error = ""
        try:
            self._admin_balance_adjustment_max_units = money_to_units(
                config.get("admin_balance_adjustment_max", "1000.00")
            )
        except ValueError as exc:
            self._admin_config_error = str(exc)
            self._admin_balance_adjustment_max_units = 0
        table_names = {
            "mysql_table": str(
                config.get("mysql_table", "astrbot_fapi_bindings")
            ).strip(),
            "mysql_binding_cooldowns_table": str(
                config.get(
                    "mysql_binding_cooldowns_table",
                    "astrbot_fapi_binding_cooldowns",
                )
            ).strip(),
            "mysql_reward_jobs_table": str(
                config.get("mysql_reward_jobs_table", "astrbot_fapi_reward_jobs")
            ).strip(),
            "mysql_checkins_table": str(
                config.get("mysql_checkins_table", "astrbot_fapi_checkins")
            ).strip(),
            "mysql_red_packets_table": str(
                config.get("mysql_red_packets_table", "astrbot_fapi_red_packets")
            ).strip(),
            "mysql_red_claims_table": str(
                config.get("mysql_red_claims_table", "astrbot_fapi_red_claims")
            ).strip(),
            "mysql_reward_budgets_table": str(
                config.get(
                    "mysql_reward_budgets_table",
                    "astrbot_fapi_reward_budgets",
                )
            ).strip(),
            "mysql_daily_broadcasts_table": str(
                config.get(
                    "mysql_daily_broadcasts_table",
                    "astrbot_fapi_daily_broadcasts",
                )
            ).strip(),
            "mysql_game_operations_table": str(
                config.get(
                    "mysql_game_operations_table",
                    "astrbot_fapi_game_operations",
                )
            ).strip(),
            "mysql_game_cooldowns_table": str(
                config.get(
                    "mysql_game_cooldowns_table",
                    "astrbot_fapi_game_cooldowns",
                )
            ).strip(),
        }
        self._validate_unique_table_names(table_names)
        request_timeout = float(config.get("request_timeout", 10))
        self.client = TokenRouterClient(
            base_url=str(config.get("tokenrouter_url", "")),
            admin_api_key=str(config.get("admin_api_key", "")),
            timeout=request_timeout,
            verify_ssl=bool(config.get("verify_ssl", True)),
        )
        self.bindings = MySQLBindingStore(
            host=str(config.get("mysql_host", "")),
            port=int(config.get("mysql_port", 3306)),
            user=str(config.get("mysql_user", "")),
            password=str(config.get("mysql_password", "")),
            database=str(config.get("mysql_database", "")),
            table=table_names["mysql_table"],
            cooldown_table=table_names["mysql_binding_cooldowns_table"],
            connect_timeout=int(config.get("mysql_connect_timeout", 10)),
            ssl_enabled=bool(config.get("mysql_ssl_enabled", True)),
            ssl_verify=bool(config.get("mysql_ssl_verify", True)),
        )
        mysql_options = {
            "host": str(config.get("mysql_host", "")),
            "port": int(config.get("mysql_port", 3306)),
            "user": str(config.get("mysql_user", "")),
            "password": str(config.get("mysql_password", "")),
            "database": str(config.get("mysql_database", "")),
            "connect_timeout": int(config.get("mysql_connect_timeout", 10)),
            "ssl_enabled": bool(config.get("mysql_ssl_enabled", True)),
            "ssl_verify": bool(config.get("mysql_ssl_verify", True)),
        }
        self.rewards = MySQLRewardStore(
            **mysql_options,
            jobs_table=table_names["mysql_reward_jobs_table"],
            checkins_table=table_names["mysql_checkins_table"],
            packets_table=table_names["mysql_red_packets_table"],
            claims_table=table_names["mysql_red_claims_table"],
            budgets_table=table_names["mysql_reward_budgets_table"],
            broadcasts_table=table_names["mysql_daily_broadcasts_table"],
        )
        self.games = MySQLGameStore(
            **mysql_options,
            table=table_names["mysql_game_operations_table"],
            cooldown_table=table_names["mysql_game_cooldowns_table"],
        )
        self._reward_delivery_lock = asyncio.Lock()
        self._reward_worker_task: asyncio.Task | None = None
        self._game_command_lock = asyncio.Lock()
        self._game_delivery_lock = asyncio.Lock()
        self._game_worker_task: asyncio.Task | None = None
        self._daily_broadcast_task: asyncio.Task | None = None
        self._daily_broadcast_enabled = bool(
            config.get("daily_broadcast_enabled", False)
        )
        self._daily_broadcast_umo = str(
            config.get("daily_broadcast_umo", "")
        ).strip()
        self._daily_broadcast_error = ""
        try:
            broadcast_time = str(
                config.get("daily_broadcast_time", "23:55")
            ).strip()
            broadcast_hour, broadcast_minute = (
                int(part) for part in broadcast_time.split(":")
            )
            if not 0 <= broadcast_hour <= 23 or not 0 <= broadcast_minute <= 59:
                raise ValueError
            self._daily_broadcast_hour = broadcast_hour
            self._daily_broadcast_minute = broadcast_minute
        except (TypeError, ValueError):
            self._daily_broadcast_hour = 23
            self._daily_broadcast_minute = 55
            self._daily_broadcast_error = "每日播报时间必须使用 HH:MM 格式"
        if self._daily_broadcast_enabled:
            if not self._daily_broadcast_umo:
                self._daily_broadcast_error = "已开启每日播报但未配置 UMO"
            elif self._daily_broadcast_umo.rsplit(":", 1)[-1] != self._allowed_group_id:
                self._daily_broadcast_error = "每日播报 UMO 与指定群不一致"
        self._game_claim_lease_seconds = max(
            180,
            min(3600, math.ceil(request_timeout) * 4 + 60),
        )
        self._reward_config_error = ""
        self._reward_timezone_error = ""
        self._game_config_error = ""
        try:
            self._reward_timezone = ZoneInfo(
                str(config.get("reward_timezone", "Asia/Shanghai")).strip()
            )
        except (ValueError, ZoneInfoNotFoundError) as exc:
            self._reward_timezone = timezone.utc
            self._reward_timezone_error = str(exc)
            self._reward_config_error = str(exc)

        try:
            self._checkin_reward_min_units = money_to_units(
                config.get("checkin_reward_min", "0.01")
            )
            self._checkin_reward_max_units = money_to_units(
                config.get("checkin_reward_max", "0.10")
            )
            self._checkin_daily_budget_units = money_to_units(
                config.get("checkin_daily_budget", "10.00")
            )
            self._red_packet_max_total_units = money_to_units(
                config.get("red_packet_max_total", "20.00")
            )
            self._red_packet_daily_budget_units = money_to_units(
                config.get("red_packet_daily_budget", "100.00")
            )
            self._red_packet_max_count = int(
                config.get("red_packet_max_count", 100)
            )
            self._red_packet_expire_minutes = int(
                config.get("red_packet_expire_minutes", 60)
            )
            if self._checkin_reward_max_units < self._checkin_reward_min_units:
                raise ValueError("签到奖励最大值不能小于最小值")
            if (
                self._checkin_reward_min_units % MIN_RED_PACKET_UNITS != 0
                or self._checkin_reward_max_units % MIN_RED_PACKET_UNITS != 0
            ):
                raise ValueError("签到奖励必须使用 0.01 的整数倍")
            if self._checkin_daily_budget_units < self._checkin_reward_min_units:
                raise ValueError("签到每日预算不能小于签到奖励最小值")
            if not 1 <= self._red_packet_max_count <= 1000:
                raise ValueError("红包最大份数必须在 1 到 1000 之间")
            if not 1 <= self._red_packet_expire_minutes <= 1440:
                raise ValueError("红包有效期必须在 1 到 1440 分钟之间")
        except ValueError as exc:
            if not self._reward_config_error:
                self._reward_config_error = str(exc)

        try:
            self._robbery_enabled = bool(config.get("robbery_enabled", True))
            self._robbery_amount_units = money_to_units(
                config.get("robbery_amount", "0.10")
            )
            self._robbery_success_rate = int(
                config.get("robbery_success_rate", 30)
            )
            self._robbery_backfire_enabled = bool(
                config.get("robbery_backfire_enabled", True)
            )
            self._robbery_backfire_rate = int(
                config.get("robbery_backfire_rate", 20)
            )
            self._robbery_backfire_amount_units = money_to_units(
                config.get("robbery_backfire_amount", "0.05")
            )
            self._robbery_cooldown_seconds = int(
                config.get("robbery_cooldown_seconds", 300)
            )
            for name, rate in (
                ("打劫", self._robbery_success_rate),
                ("打劫反噬", self._robbery_backfire_rate),
            ):
                if not 0 <= rate <= 100:
                    raise ValueError(f"{name}成功率必须在 0 到 100 之间")
            if not 0 <= self._robbery_cooldown_seconds <= 86400:
                raise ValueError("打劫冷却时间必须在 0 到 86400 秒之间")
        except ValueError as exc:
            self._game_config_error = str(exc)

        self._transfer_config_error = ""
        try:
            self._transfer_max_units = money_to_units(
                config.get("transfer_max_amount", "1000.00")
            )
        except ValueError as exc:
            self._transfer_config_error = str(exc)
            self._transfer_max_units = 0

    async def initialize(self) -> None:
        try:
            await self.bindings.initialize()
        except BindingDatabaseError as exc:
            logger.error("FAPI 远程 MySQL 初始化失败：%s", exc)
        try:
            await self.rewards.initialize()
        except RewardDatabaseError as exc:
            logger.error("FAPI 奖励数据表初始化失败：%s", exc)
        try:
            await self.games.initialize()
        except GameDatabaseError as exc:
            logger.error("FAPI 游戏数据表初始化失败：%s", exc)
        if self._daily_broadcast_error:
            logger.error("FAPI 每日播报配置无效：%s", self._daily_broadcast_error)
        self._ensure_background_workers()

    @filter.command_group("fapi")
    def fapi(self):
        """FAPI 中转站账号绑定。"""

    @fapi.command("管理", alias={"admin"})
    async def admin_private(
        self,
        event: AstrMessageEvent,
        raw_args: GreedyStr,
    ):
        """在私聊中执行受限的中转站管理操作。"""
        admin_context, context_error = await self._private_admin_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert admin_context is not None
        if not self._admin_private_enabled:
            yield event.plain_result("管理员私聊功能当前未开启。")
            return

        command_text = str(raw_args).strip()
        action, separator, arguments = command_text.partition(" ")
        arguments = arguments.strip() if separator else ""
        normalized_action = action.casefold()
        if normalized_action in {"", "帮助", "菜单", "help"}:
            if arguments:
                yield event.plain_result("用法：/fapi 管理")
                return
            adjustment_limit = (
                "配置错误"
                if self._admin_config_error
                else self._format_units(self._admin_balance_adjustment_max_units)
            )
            yield event.plain_result(
                "FAPI 管理员私聊菜单\n"
                "/fapi 管理 概览\n"
                "/fapi 管理 RPM [邮箱]\n"
                "/fapi 管理 排行 [数量]\n"
                "/fapi 管理 状态 <邮箱>\n"
                "/fapi 管理 查询 <邮箱>（状态的别名）\n"
                "/fapi 管理 账单 <邮箱>\n"
                "/fapi 管理 全局流水 [数量]\n"
                "/fapi 管理 红包列表 [数量]\n"
                "/fapi 管理 红包详情 <编号>\n"
                "/fapi 管理 关闭红包 <编号>\n"
                "/fapi 管理 红包退款 <编号>\n"
                "/fapi 管理 封禁 <邮箱>\n"
                "/fapi 管理 解封 <邮箱>\n"
                "/fapi 管理 封禁列表 [数量]\n"
                "/fapi 管理 未完成任务 [数量]\n"
                "/fapi 管理 重试 <任务编号>\n"
                "/fapi 管理 加余额 <邮箱> <金额>\n"
                "/fapi 管理 减余额 <邮箱> <金额>\n\n"
                "排行默认 10 人、最多 20 人；邮箱必须完全一致；"
                "余额操作单次上限："
                f"{adjustment_limit}"
            )
            return

        action_aliases = {
            "概览": "overview",
            "今日概览": "overview",
            "全日概览": "overview",
            "全站概览": "overview",
            "overview": "overview",
            "rpm": "rpm",
            "排行": "ranking",
            "排名": "ranking",
            "ranking": "ranking",
            "rank": "ranking",
            "查询": "query",
            "状态": "query",
            "用户": "query",
            "query": "query",
            "user": "query",
            "账单": "ledger",
            "bill": "ledger",
            "全局流水": "activity",
            "流水": "activity",
            "activity": "activity",
            "红包列表": "packet_list",
            "红包详情": "packet_detail",
            "关闭红包": "packet_close",
            "红包退款": "packet_refund",
            "封禁": "block",
            "解封": "unblock",
            "封禁列表": "blocked",
            "未完成任务": "incomplete",
            "任务": "incomplete",
            "tasks": "incomplete",
            "重试": "retry",
            "retry": "retry",
            "加余额": "add",
            "增加余额": "add",
            "add": "add",
            "减余额": "subtract",
            "扣余额": "subtract",
            "subtract": "subtract",
            "sub": "subtract",
        }
        operation = action_aliases.get(normalized_action)
        if operation is None:
            yield event.plain_result("未知管理操作，请发送 /fapi 管理。")
            return

        email = ""
        amount = ""
        if operation in {
            "query", "rpm", "ledger", "retry", "block", "unblock",
            "packet_detail", "packet_close", "packet_refund",
        }:
            email = arguments
        elif operation in {"ranking", "incomplete", "activity", "packet_list", "blocked"}:
            email = arguments
        elif operation in {"add", "subtract"}:
            email, separator, amount = arguments.rpartition(" ")
            email = email.strip()
            amount = amount.strip() if separator else ""
            if not email or not amount:
                operation_name = "加余额" if operation == "add" else "减余额"
                yield event.plain_result(
                    f"用法：/fapi 管理 {operation_name} <邮箱> <金额>"
                )
                return
        elif arguments:
            yield event.plain_result("用法：/fapi 管理 概览")
            return

        if operation == "overview":
            try:
                overview = await self.client.get_dashboard_overview()
            except TokenRouterError as exc:
                logger.warning(
                    "FAPI 管理员查询全站概览失败：status=%s, code=%s, error=%s",
                    exc.status_code,
                    exc.api_code,
                    exc,
                )
                yield event.plain_result(self._admin_read_error_reply(exc))
                return
            except Exception:
                logger.exception("FAPI 管理员查询全站概览出现未预期错误")
                yield event.plain_result("管理数据查询暂时不可用，请稍后重试。")
                return
            yield event.plain_result(self._dashboard_overview_reply(overview))
            return

        if operation == "incomplete":
            try:
                task_limit = 10 if not email.strip() else int(email.strip())
            except ValueError:
                task_limit = 0
            if not 1 <= task_limit <= 20:
                yield event.plain_result("任务数量必须是 1 到 20 的整数。")
                return
            try:
                reply = await self._incomplete_tasks_reply(task_limit)
            except (RewardDatabaseError, GameDatabaseError) as exc:
                logger.error("FAPI 管理员查询未完成任务失败：%s", exc)
                yield event.plain_result("任务数据库暂时不可用，请稍后重试。")
                return
            yield event.plain_result(reply)
            return

        if operation == "retry":
            operation_id = email.strip()
            if not operation_id:
                yield event.plain_result("用法：/fapi 管理 重试 <任务编号>")
                return
            try:
                retry_reply = await self._retry_task(operation_id)
            except (RewardDatabaseError, GameDatabaseError) as exc:
                logger.error("FAPI 管理员重试任务失败：%s", exc)
                yield event.plain_result("任务数据库暂时不可用，请稍后重试。")
                return
            yield event.plain_result(retry_reply)
            return

        if operation in {"activity", "packet_list", "blocked"}:
            raw_limit = email.strip()
            try:
                list_limit = 10 if not raw_limit else int(raw_limit)
            except ValueError:
                list_limit = 0
            if not 1 <= list_limit <= 20:
                yield event.plain_result("数量必须是 1 到 20 的整数。")
                return
            try:
                if operation == "activity":
                    reply = await self._global_activity_reply(list_limit)
                elif operation == "packet_list":
                    reply = await self._admin_packet_list_reply(list_limit)
                else:
                    reply = await self._blocked_users_reply(list_limit)
            except (BindingDatabaseError, RewardDatabaseError, GameDatabaseError) as exc:
                logger.error("FAPI 管理员读取后台列表失败：%s", exc)
                yield event.plain_result("后台数据暂时不可用，请稍后重试。")
                return
            yield event.plain_result(reply)
            return

        if operation in {"packet_detail", "packet_close", "packet_refund"}:
            try:
                packet_id = int(email.strip())
            except ValueError:
                packet_id = 0
            if packet_id <= 0:
                yield event.plain_result("红包编号必须是正整数。")
                return
            try:
                if operation == "packet_detail":
                    packet = await self.rewards.get_red_packet_admin(packet_id)
                    reply = self._admin_packet_reply(packet) if packet else "未找到该红包。"
                elif operation == "packet_close":
                    packet = await self.rewards.close_red_packet(packet_id)
                    reply = (
                        f"红包 #{packet_id} 已关闭，剩余 {self._format_units(packet.remaining_units)}。"
                        if packet else "红包不存在或当前不可关闭。"
                    )
                else:
                    reply = await self._refund_packet(packet_id)
            except (BindingDatabaseError, RewardDatabaseError) as exc:
                logger.error("FAPI 管理员处理红包失败：%s", exc)
                yield event.plain_result("红包管理数据暂时不可用，请稍后重试。")
                return
            yield event.plain_result(reply)
            return

        if operation in {"block", "unblock"}:
            user, user_error = await self._resolve_private_admin_user(email)
            if user_error:
                yield event.plain_result(user_error)
                return
            assert user is not None
            try:
                changed = await self.bindings.set_user_blocked(
                    user.user_id,
                    operation == "block",
                )
            except BindingDatabaseError as exc:
                logger.error("FAPI 管理员更新用户封禁状态失败：%s", exc)
                yield event.plain_result("封禁数据暂时不可用，请稍后重试。")
                return
            if not changed:
                yield event.plain_result("该用户尚未绑定插件账号。")
                return
            yield event.plain_result(
                f"已{'封禁' if operation == 'block' else '解封'}："
                f"{self._safe_reply_field(user.email)}（ID {user.user_id}）"
            )
            return

        if operation == "ranking":
            if getattr(self, "_reward_timezone_error", ""):
                yield event.plain_result(
                    "排行时区配置不正确，请检查 reward_timezone。"
                )
                return
            raw_limit = email.strip()
            try:
                ranking_limit = 10 if not raw_limit else int(raw_limit)
            except ValueError:
                ranking_limit = 0
            if not 1 <= ranking_limit <= 20:
                yield event.plain_result("排行数量必须是 1 到 20 的整数。")
                return
            ranking_date = datetime.now(self._admin_stats_timezone()).date().isoformat()
            try:
                ranking = await self.client.get_user_spending_ranking(
                    ranking_date=ranking_date,
                    timezone_name=self._admin_stats_timezone_name(),
                    limit=ranking_limit,
                )
            except TokenRouterError as exc:
                logger.warning(
                    "FAPI 管理员查询用户排行失败：status=%s, code=%s, error=%s",
                    exc.status_code,
                    exc.api_code,
                    exc,
                )
                yield event.plain_result(self._admin_read_error_reply(exc))
                return
            except Exception:
                logger.exception("FAPI 管理员查询用户排行出现未预期错误")
                yield event.plain_result("管理数据查询暂时不可用，请稍后重试。")
                return
            yield event.plain_result(self._user_ranking_reply(ranking))
            return

        if operation == "rpm" and amount:
            yield event.plain_result("用法：/fapi 管理 RPM [邮箱]")
            return

        if operation == "rpm" and not email.strip():
            try:
                overview = await self.client.get_dashboard_overview()
            except TokenRouterError as exc:
                logger.warning(
                    "FAPI 管理员查询全站 RPM 失败：status=%s, code=%s, error=%s",
                    exc.status_code,
                    exc.api_code,
                    exc,
                )
                yield event.plain_result(self._admin_read_error_reply(exc))
                return
            except Exception:
                logger.exception("FAPI 管理员查询全站 RPM 出现未预期错误")
                yield event.plain_result("管理数据查询暂时不可用，请稍后重试。")
                return
            yield event.plain_result(
                "全站实时吞吐（近5分钟均值）\n"
                f"RPM：{overview.rpm:,}\n"
                f"TPM：{overview.tpm:,}\n"
                f"当前小时活跃用户：{overview.hourly_active_users:,}"
            )
            return

        amount_units = 0
        idempotency_key = ""
        if operation in {"add", "subtract"}:
            if self._admin_config_error:
                yield event.plain_result("管理员余额配置不正确，请检查插件配置。")
                return
            try:
                amount_units = money_to_units(amount)
            except ValueError as exc:
                yield event.plain_result(f"金额不正确：{exc}")
                return
            if amount_units > self._admin_balance_adjustment_max_units:
                yield event.plain_result(
                    "单次余额调整不能超过 "
                    f"{self._format_units(self._admin_balance_adjustment_max_units)}。"
                )
                return

            idempotency_key = self._admin_balance_operation_key(
                event,
                admin_context,
                operation,
            ) or ""
            if not idempotency_key:
                yield event.plain_result(
                    "无法确认当前消息编号，为避免重复调账，本次操作已取消。"
                )
                return

        user, user_error = await self._resolve_private_admin_user(email)
        if user_error:
            yield event.plain_result(user_error)
            return
        assert user is not None

        if operation == "ledger":
            try:
                reply = await self._ledger_reply(
                    user.user_id,
                    email=user.email,
                    admin=True,
                )
            except (RewardDatabaseError, GameDatabaseError) as exc:
                logger.error("FAPI 管理员查询用户账单失败：%s", exc)
                yield event.plain_result("账单数据库暂时不可用，请稍后重试。")
                return
            yield event.plain_result(reply)
            return

        if operation == "rpm":
            try:
                rpm_status = await self.client.get_user_rpm_status(user.user_id)
            except TokenRouterError as exc:
                logger.warning(
                    "FAPI 管理员查询用户 RPM 失败：user=%s, status=%s, "
                    "code=%s, error=%s",
                    user.user_id,
                    exc.status_code,
                    exc.api_code,
                    exc,
                )
                yield event.plain_result(self._admin_read_error_reply(exc))
                return
            except Exception:
                logger.exception("FAPI 管理员查询用户 RPM 出现未预期错误")
                yield event.plain_result("管理数据查询暂时不可用，请稍后重试。")
                return
            yield event.plain_result(self._user_rpm_reply(user, rpm_status))
            return

        if operation == "query":
            try:
                summary = await self.client.get_balance_summary(user.user_id)
            except TokenRouterError as exc:
                logger.warning(
                    "FAPI 管理员查询用户失败：status=%s, code=%s, error=%s",
                    exc.status_code,
                    exc.api_code,
                    exc,
                )
                yield event.plain_result(self._api_error_reply(exc))
                return
            except Exception:
                logger.exception("FAPI 管理员查询用户出现未预期错误")
                yield event.plain_result("管理员查询服务暂时不可用，请稍后重试。")
                return

            yield event.plain_result(
                "中转站用户\n"
                f"ID：{user.user_id}\n"
                f"邮箱：{self._safe_reply_field(user.email)}\n"
                f"用户名：{self._safe_reply_field(user.username)}\n"
                f"状态：{self._safe_reply_field(user.status or '未知')}\n"
                f"余额：{self._format_usd(summary.balance)}\n"
                f"已使用（近30天）：{self._format_usd(summary.used_last_30_days)}\n"
                f"并发上限：{summary.concurrency}"
            )
            return

        try:
            result = await self.client.adjust_balance(
                user_id=user.user_id,
                amount=units_to_decimal(amount_units),
                operation=operation,
                idempotency_key=idempotency_key,
                notes=(
                    "FAPI private admin balance adjustment by "
                    f"{admin_context.sender_id}"
                ),
            )
        except TokenRouterError as exc:
            logger.warning(
                "FAPI 管理员余额调整失败：user=%s, operation=%s, "
                "amount_units=%s, idempotency_key=%s, status=%s, code=%s, "
                "unknown=%s, error=%s",
                user.user_id,
                operation,
                amount_units,
                idempotency_key,
                exc.status_code,
                exc.api_code,
                exc.outcome_unknown,
                exc,
            )
            if exc.outcome_unknown:
                yield event.plain_result(
                    "余额调整结果暂时无法确认。请先查询该用户余额，"
                    "不要重复发送调账命令。"
                )
            elif operation == "subtract" and is_insufficient_balance_error(exc):
                yield event.plain_result("用户余额不足，本次未扣减。")
            else:
                yield event.plain_result(self._admin_adjustment_error_reply(exc))
            return
        except Exception:
            logger.exception("FAPI 管理员余额调整出现未预期错误")
            yield event.plain_result("余额调整服务暂时不可用，请稍后重试。")
            return

        operation_name = "增加" if operation == "add" else "扣减"
        logger.info(
            "FAPI 管理员余额调整成功：admin=%s, user=%s, operation=%s, "
            "amount_units=%s, idempotency_key=%s",
            admin_context.sender_id,
            user.user_id,
            operation,
            amount_units,
            idempotency_key,
        )
        yield event.plain_result(
            "余额调整成功\n"
            f"邮箱：{self._safe_reply_field(user.email)}（ID {user.user_id}）\n"
            f"用户名：{self._safe_reply_field(user.username)}\n"
            f"操作：{operation_name}\n"
            f"金额：{self._format_units(amount_units)}\n"
            f"调整后余额：{self._format_usd(result.balance)}"
        )

    @fapi.command("帮助", alias={"help", "菜单"})
    async def help_menu(self, event: AstrMessageEvent):
        """显示 FAPI 指令菜单。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert command_context is not None

        if command_context.platform == "qq_official":
            binding_usage = "/fapi 绑定 <完整邮箱>"
        else:
            binding_usage = (
                "/fapi 绑定\n"
                "/fapi 绑定 <完整邮箱>"
            )
        official_website = "官网:https://www.f-api.site"

        yield event.plain_result(
            "FAPI 功能菜单\n"
            f"{binding_usage}\n"
            "/fapi 绑定状态\n"
            "/fapi 余额\n"
            "/fapi 签到\n"
            "/fapi 签到排行\n"
            "/fapi 红包 <总金额> <份数>\n"
            "/fapi 抢红包 [红包编号]\n"
            "/fapi 红包状态 [红包编号]\n"
            "/fapi 红包排行\n"
            "/fapi 账单\n"
            "/fapi 打劫 @用户\n"
            "/fapi 转账 @用户 <金额>\n"
            "/fapi 帮助\n\n"
            f"{official_website}"
        )

    @fapi.command("绑定", alias={"bind"})
    async def bind(
        self,
        event: AstrMessageEvent,
        email: str = "",
        extra: str = "",
    ):
        """按唯一邮箱绑定中转站账号。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert command_context is not None
        qq_id = command_context.sender_id

        if extra.strip():
            yield event.plain_result("绑定不再使用用户名，请使用：/fapi 绑定 <完整邮箱>")
            return

        supplied_email = email.strip()
        if not supplied_email and command_context.platform != "aiocqhttp":
            yield event.plain_result(
                "QQ 官方机器人无法取得真实 QQ 号，请使用："
                "/fapi 绑定 <完整邮箱>"
            )
            return
        target_email = supplied_email or f"{qq_id}@qq.com"
        if not self._looks_like_email(target_email):
            yield event.plain_result(
                "邮箱格式不完整，请使用：/fapi 绑定 <完整邮箱>"
            )
            return

        try:
            users = await self.client.find_users_by_exact_email(target_email)
        except TokenRouterError as exc:
            logger.warning(
                "FAPI 绑定查询失败：status=%s, code=%s, error=%s",
                exc.status_code,
                exc.api_code,
                exc,
            )
            yield event.plain_result(self._api_error_reply(exc))
            return
        except Exception:
            logger.exception("FAPI 绑定查询出现未预期错误")
            yield event.plain_result("绑定服务暂时不可用，请稍后重试。")
            return

        if not users:
            yield event.plain_result(
                "未找到邮箱完全匹配的中转站用户，绑定已暂停。"
            )
            return
        if len(users) > 1:
            yield event.plain_result(
                "存在多个相同邮箱的中转站用户，无法安全绑定，请联系管理员。"
            )
            return

        user = users[0]

        try:
            result = await self.bindings.bind(
                qq_id=qq_id,
                tokenrouter_user_id=user.user_id,
                email=user.email,
            )
        except BindCooldownActive as exc:
            yield event.plain_result(
                "绑定冷却中，请在 "
                f"{self._format_duration(exc.retry_after_seconds)} 后再绑定。"
            )
            return
        except BindingDatabaseError as exc:
            logger.error("FAPI 保存用户绑定失败：%s", exc)
            yield event.plain_result("远程绑定数据库暂时不可用，请联系管理员。")
            return

        if (
            result.status == BindStatus.USER_CONFLICT
            and command_context.platform == "qq_official"
        ):
            yield event.plain_result(
                "该中转站账号已存在其他绑定。若这是个人机器人留下的旧 QQ 绑定，"
                "官机 OpenID 无法自动确认同一身份，请联系管理员迁移。"
            )
            return

        replies = {
            BindStatus.CREATED: "绑定成功。",
            BindStatus.EXISTING: "当前账号已完成绑定，无需重复操作。",
            BindStatus.QQ_CONFLICT: "当前账号已绑定其他中转站账号，请联系管理员处理。",
            BindStatus.USER_CONFLICT: "该中转站账号已绑定其他 QQ 账号，请联系管理员处理。",
        }
        yield event.plain_result(replies[result.status])

    @fapi.command("绑定状态", alias={"status"})
    async def binding_status(self, event: AstrMessageEvent):
        """查询发送者 QQ 的绑定状态。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert command_context is not None
        qq_id = command_context.sender_id

        try:
            binding = await self.bindings.get_by_qq(qq_id)
        except BindingDatabaseError as exc:
            logger.error("FAPI 查询用户绑定失败：%s", exc)
            yield event.plain_result("远程绑定数据库暂时不可用，请联系管理员。")
            return

        if binding is None:
            yield event.plain_result("当前账号尚未绑定中转站账号。")
            return
        yield event.plain_result("当前账号已绑定中转站账号。")

    @fapi.command("签到排行", alias={"checkin_rank"})
    async def checkin_ranking(self, event: AstrMessageEvent):
        """显示累计签到次数排行。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        try:
            reply = await self._reward_ranking_reply(
                source_type="checkin",
                title="签到排行",
            )
        except (RewardDatabaseError, BindingDatabaseError) as exc:
            logger.error("FAPI 查询签到排行失败：%s", exc)
            yield event.plain_result("排行数据库暂时不可用，请稍后重试。")
            return
        yield event.plain_result(reply)

    @fapi.command("红包排行", alias={"redpacket_rank"})
    async def red_packet_ranking(self, event: AstrMessageEvent):
        """显示累计成功领取红包次数排行。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        try:
            reply = await self._reward_ranking_reply(
                source_type="red_packet",
                title="抢红包排行",
            )
        except (RewardDatabaseError, BindingDatabaseError) as exc:
            logger.error("FAPI 查询红包排行失败：%s", exc)
            yield event.plain_result("排行数据库暂时不可用，请稍后重试。")
            return
        yield event.plain_result(reply)

    @fapi.command("账单", alias={"bill"})
    async def ledger(self, event: AstrMessageEvent):
        """显示当前绑定账号最近的插件余额记录。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        try:
            binding = await self.bindings.get_by_qq(command_context.sender_id)
        except BindingDatabaseError as exc:
            logger.error("FAPI 查询账单绑定关系失败：%s", exc)
            yield event.plain_result("远程绑定数据库暂时不可用，请稍后重试。")
            return
        if binding is None:
            yield event.plain_result("当前账号尚未绑定中转站账号，请先使用 /fapi 绑定。")
            return
        try:
            reply = await self._ledger_reply(
                binding.tokenrouter_user_id,
                email=binding.email,
                admin=False,
            )
        except (RewardDatabaseError, GameDatabaseError) as exc:
            logger.error("FAPI 查询用户账单失败：%s", exc)
            yield event.plain_result("账单数据库暂时不可用，请稍后重试。")
            return
        yield event.plain_result(reply)

    @fapi.command("余额", alias={"balance"})
    async def balance(self, event: AstrMessageEvent):
        """查询当前 QQ 已绑定账号的余额和近 30 天用量。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert command_context is not None
        qq_id = command_context.sender_id

        try:
            binding = await self.bindings.get_by_qq(qq_id)
        except BindingDatabaseError as exc:
            logger.error("FAPI 查询余额绑定关系失败：%s", exc)
            yield event.plain_result("远程绑定数据库暂时不可用，请联系管理员。")
            return

        if binding is None:
            yield event.plain_result("当前账号尚未绑定中转站账号，请先使用 /fapi 绑定。")
            return

        try:
            summary = await self.client.get_balance_summary(
                binding.tokenrouter_user_id
            )
        except TokenRouterError as exc:
            logger.warning(
                "FAPI 余额查询失败：status=%s, code=%s, error=%s",
                exc.status_code,
                exc.api_code,
                exc,
            )
            yield event.plain_result(self._api_error_reply(exc))
            return
        except Exception:
            logger.exception("FAPI 余额查询出现未预期错误")
            yield event.plain_result("余额查询服务暂时不可用，请稍后重试。")
            return

        yield event.plain_result(
            f"邮箱：{self._mask_email(binding.email)}\n"
            f"余额：{self._format_usd(summary.balance)}\n"
            f"已使用（近30天）：{self._format_usd(summary.used_last_30_days)}\n"
            f"并发上限：{summary.concurrency}"
        )

    @fapi.command("签到", alias={"checkin"})
    async def checkin(self, event: AstrMessageEvent):
        """每日签到并向已绑定账号发放余额。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert command_context is not None
        qq_id = command_context.sender_id
        if self._reward_config_error:
            yield event.plain_result("奖励配置不正确，请联系管理员检查插件配置。")
            return

        try:
            binding = await self.bindings.get_by_qq(qq_id)
        except BindingDatabaseError as exc:
            logger.error("FAPI 查询签到绑定关系失败：%s", exc)
            yield event.plain_result("远程绑定数据库暂时不可用，请联系管理员。")
            return
        if binding is None:
            yield event.plain_result("当前账号尚未绑定中转站账号，请先使用 /fapi 绑定。")
            return
        reward_date = datetime.now(self._reward_timezone).date()
        try:
            reservation = await self.rewards.reserve_checkin(
                qq_id=qq_id,
                tokenrouter_user_id=binding.tokenrouter_user_id,
                reward_date=reward_date,
                minimum_amount_units=self._checkin_reward_min_units,
                maximum_amount_units=self._checkin_reward_max_units,
                daily_budget_units=self._checkin_daily_budget_units,
            )
        except RewardBudgetExhausted:
            yield event.plain_result("今天的签到奖励预算已经发完了，明天再来吧。")
            return
        except RewardDatabaseError as exc:
            logger.error("FAPI 预留签到奖励失败：%s", exc)
            yield event.plain_result("签到数据库暂时不可用，请稍后重试。")
            return

        if reservation.job.status == RewardJobStatus.SUCCEEDED:
            yield event.plain_result(
                "今天已经签到过了。\n"
                f"连续签到：{reservation.streak} 天\n"
                f"累计签到：{reservation.total_days} 天"
            )
            return

        delivered = await self._deliver_reward(reservation.job)
        if not delivered:
            yield event.plain_result(
                "签到成功，奖励已进入补发队列，请稍后使用 /fapi 余额确认。"
            )
            return
        yield event.plain_result(self._checkin_success_reply(reservation))

    @fapi.command("红包", alias={"redpacket"})
    async def create_red_packet(
        self,
        event: AstrMessageEvent,
        amount: str = "",
        count: int = 0,
    ):
        """由已绑定用户使用自身余额创建群红包。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert command_context is not None
        qq_id = command_context.sender_id
        group_id = command_context.group_id
        if self._reward_config_error:
            yield event.plain_result("奖励配置不正确，请联系管理员检查插件配置。")
            return
        if not amount or count <= 0:
            yield event.plain_result("用法：/fapi 红包 <总金额> <份数>")
            return
        try:
            total_units = money_to_units(amount)
        except ValueError as exc:
            yield event.plain_result(f"红包金额不正确：{exc}")
            return
        if total_units > self._red_packet_max_total_units:
            yield event.plain_result(
                f"单个红包不能超过 {self._format_units(self._red_packet_max_total_units)}。"
            )
            return
        if count > self._red_packet_max_count:
            yield event.plain_result(
                f"单个红包不能超过 {self._red_packet_max_count} 份。"
            )
            return
        if total_units < count * MIN_RED_PACKET_UNITS:
            yield event.plain_result("红包总金额不足，每份至少需要 USD 0.01。")
            return

        now = datetime.now(self._reward_timezone)
        expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
            minutes=self._red_packet_expire_minutes
        )
        try:
            creation_key = self._red_packet_creation_key(
                event,
                command_context.platform,
                group_id,
            )
            if creation_key is None:
                yield event.plain_result("无法确认当前消息编号，为避免重复创建红包，本次操作已取消。")
                return
            binding = await self.bindings.get_by_qq(qq_id)
            if binding is None:
                yield event.plain_result(
                    "当前账号尚未绑定 TokenRouter 账户，请先使用 /fapi 绑定。"
                )
                return
            await self.client.adjust_balance(
                user_id=binding.tokenrouter_user_id,
                amount=units_to_decimal(total_units),
                operation="subtract",
                idempotency_key=f"{creation_key}:debit",
                notes=(
                    f"FAPI user-funded red packet group={group_id} "
                    f"creation={creation_key}"
                ),
            )
            packet = await self.rewards.create_red_packet(
                creation_key=creation_key,
                group_id=group_id,
                creator_qq_id=qq_id,
                total_units=total_units,
                total_count=count,
                reward_date=now.date(),
                daily_budget_units=self._red_packet_daily_budget_units,
                expires_at=expires_at,
            )
        except BindingDatabaseError as exc:
            logger.error("FAPI 查询红包创建者绑定关系失败：%s", exc)
            yield event.plain_result("远程绑定数据库暂时不可用，请稍后重试。")
            return
        except TokenRouterError as exc:
            logger.warning(
                "FAPI 创建红包扣款失败：creator=%s, amount_units=%s, "
                "status=%s, code=%s, unknown=%s, error=%s",
                qq_id,
                total_units,
                exc.status_code,
                exc.api_code,
                exc.outcome_unknown,
                exc,
            )
            if is_insufficient_balance_error(exc):
                yield event.plain_result("余额不足，无法创建红包。")
            elif exc.outcome_unknown:
                yield event.plain_result(
                    "红包扣款结果暂时无法确认，请使用同一条消息稍后重试。"
                )
            else:
                yield event.plain_result("红包扣款失败，请稍后重试。")
            return
        except RewardBudgetExhausted:
            refunded = await self._refund_red_packet_creation(
                binding.tokenrouter_user_id,
                total_units,
                creation_key,
            )
            yield event.plain_result(
                "今天的红包创建额度已用完，金额已退回余额。"
                if refunded
                else "今天的红包创建额度已用完，退款处理中，请稍后查询余额。"
            )
            return
        except RewardDatabaseError as exc:
            logger.error("FAPI 创建群红包失败：%s", exc)
            refunded = await self._refund_red_packet_creation(
                binding.tokenrouter_user_id,
                total_units,
                creation_key,
            )
            yield event.plain_result(
                "红包创建失败，金额已退回余额。"
                if refunded
                else "红包创建失败，退款处理中，请稍后查询余额。"
            )
            return

        yield event.plain_result(
            f"群红包 #{packet.packet_id} 已创建\n"
            f"总金额：{self._format_units(packet.total_units)}\n"
            f"份数：{packet.total_count}\n"
            f"有效期：{self._red_packet_expire_minutes} 分钟\n"
            f"领取指令：/fapi 抢红包 {packet.packet_id}"
        )

    @fapi.command("抢红包", alias={"claim"})
    async def claim_red_packet(
        self,
        event: AstrMessageEvent,
        packet_id: str = "",
    ):
        """领取当前群的指定红包或最新红包。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert command_context is not None
        qq_id = command_context.sender_id
        group_id = command_context.group_id
        parsed_packet_id = self._parse_optional_packet_id(packet_id)
        if parsed_packet_id is None:
            yield event.plain_result("红包编号不正确。")
            return
        try:
            binding = await self.bindings.get_by_qq(qq_id)
        except BindingDatabaseError as exc:
            logger.error("FAPI 查询红包绑定关系失败：%s", exc)
            yield event.plain_result("远程绑定数据库暂时不可用，请联系管理员。")
            return
        if binding is None:
            yield event.plain_result("当前账号尚未绑定中转站账号，请先使用 /fapi 绑定。")
            return
        try:
            reservation = await self.rewards.reserve_red_packet_claim(
                group_id=group_id,
                qq_id=qq_id,
                tokenrouter_user_id=binding.tokenrouter_user_id,
                packet_id=parsed_packet_id or None,
            )
        except RedPacketUnavailable as exc:
            yield event.plain_result(str(exc))
            return
        except RewardDatabaseError as exc:
            logger.error("FAPI 预留红包奖励失败：%s", exc)
            yield event.plain_result("红包数据库暂时不可用，请稍后重试。")
            return

        if reservation.job.status == RewardJobStatus.SUCCEEDED:
            yield event.plain_result(
                f"你已经领取过红包 #{reservation.packet_id}，金额："
                f"{self._format_units(reservation.job.amount_units)}"
            )
            return
        delivered = await self._deliver_reward(reservation.job)
        if not delivered:
            yield event.plain_result(
                f"红包 #{reservation.packet_id} 已抢到，奖励正在补发，请稍后查询余额。"
            )
            return
        yield event.plain_result(
            f"抢到红包 #{reservation.packet_id}\n"
            f"金额：{self._format_units(reservation.job.amount_units)}\n"
            "奖励已加入中转站余额。"
        )

    @fapi.command("红包状态", alias={"redpacket_status"})
    async def red_packet_status(
        self,
        event: AstrMessageEvent,
        packet_id: str = "",
    ):
        """查看当前群红包状态。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert command_context is not None
        group_id = command_context.group_id
        parsed_packet_id = self._parse_optional_packet_id(packet_id)
        if parsed_packet_id is None:
            yield event.plain_result("红包编号不正确。")
            return
        try:
            packet = await self.rewards.get_red_packet(
                group_id=group_id,
                packet_id=parsed_packet_id or None,
            )
        except RewardDatabaseError as exc:
            logger.error("FAPI 查询红包状态失败：%s", exc)
            yield event.plain_result("红包数据库暂时不可用，请稍后重试。")
            return
        if packet is None:
            yield event.plain_result("当前群没有找到该红包。")
            return
        claimed = packet.total_count - packet.remaining_count
        reserved_units = packet.total_units - packet.remaining_units
        yield event.plain_result(
            f"红包 #{packet.packet_id}\n"
            f"已抢：{claimed}/{packet.total_count} 份\n"
            f"已分配：{self._format_units(reserved_units)} / "
            f"{self._format_units(packet.total_units)}\n"
            f"已到账：{packet.credited_count} 份"
        )

    @fapi.command("打劫", alias={"打结", "rob"})
    async def robbery(self, event: AstrMessageEvent):
        """尝试从被 @ 用户的绑定账号转入固定金额。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert command_context is not None
        if self._game_config_error:
            yield event.plain_result("游戏配置不正确，请联系管理员检查插件配置。")
            return
        if not self._robbery_enabled:
            yield event.plain_result("打劫功能当前未开启。")
            return

        target_qq_id = self._mentioned_target_id(event, command_context)
        if target_qq_id is None:
            yield event.plain_result("请只 @ 一名其他群成员：/fapi 打劫 @用户")
            return
        operation_key = self._game_operation_key(
            event,
            command_context,
            "robbery",
        )
        if operation_key is None:
            yield event.plain_result("无法确认当前消息编号，为避免重复扣款，本次操作已取消。")
            return

        early_reply = ""
        try:
            async with self._game_command_lock:
                operation = await self.games.get_by_key(operation_key)
                if operation is None:
                    actor_binding, target_binding = await asyncio.gather(
                        self.bindings.get_by_qq(command_context.sender_id),
                        self.bindings.get_by_qq(target_qq_id),
                    )
                    if actor_binding is None:
                        early_reply = (
                            "当前账号尚未绑定中转站账号，请先使用 /fapi 绑定。"
                        )
                    elif target_binding is None:
                        early_reply = "被 @ 用户尚未绑定中转站账号，无法打劫。"
                    else:
                        won = roll_success(self._robbery_success_rate)
                        backfired = (
                            not won
                            and self._robbery_backfire_enabled
                            and roll_success(self._robbery_backfire_rate)
                        )
                        if won:
                            debit_user_id = target_binding.tokenrouter_user_id
                            credit_user_id = actor_binding.tokenrouter_user_id
                            amount_units = self._robbery_amount_units
                        elif backfired:
                            debit_user_id = actor_binding.tokenrouter_user_id
                            credit_user_id = target_binding.tokenrouter_user_id
                            amount_units = self._robbery_backfire_amount_units
                        else:
                            debit_user_id = target_binding.tokenrouter_user_id
                            credit_user_id = None
                            amount_units = 0
                        reservation = await self.games.reserve_game(
                            operation_key=operation_key,
                            game_type="robbery",
                            group_id=command_context.group_id,
                            actor_qq_id=command_context.sender_id,
                            actor_user_id=actor_binding.tokenrouter_user_id,
                            target_qq_id=target_qq_id,
                            target_user_id=target_binding.tokenrouter_user_id,
                            debit_user_id=debit_user_id,
                            credit_user_id=credit_user_id,
                            debit_units=amount_units,
                            credit_units=amount_units,
                            won=won,
                            cooldown_seconds=self._robbery_cooldown_seconds,
                        )
                        operation = reservation.operation
        except GameCooldownActive as exc:
            yield event.plain_result(
                f"打劫冷却中，请在 {exc.retry_after_seconds} 秒后再试。"
            )
            return
        except BindingDatabaseError as exc:
            logger.error("FAPI 查询打劫绑定关系失败：%s", exc)
            yield event.plain_result("远程绑定数据库暂时不可用，请稍后重试。")
            return
        except (GameDatabaseError, GameOperationConflict) as exc:
            logger.error("FAPI 预留打劫操作失败：%s", exc)
            yield event.plain_result("游戏数据库暂时不可用，请稍后重试。")
            return

        if early_reply:
            yield event.plain_result(early_reply)
            return
        settled = await self._settle_game_operation(operation)
        yield event.plain_result(self._game_reply(settled))

    @fapi.command("转账", alias={"transfer", "转余额"})
    async def transfer(
        self,
        event: AstrMessageEvent,
        raw_args: GreedyStr,
    ):
        """将已绑定账户余额转给一名被 @ 的群成员。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert command_context is not None
        if self._transfer_config_error:
            yield event.plain_result("转账配置不正确，请联系管理员检查插件配置。")
            return
        amount_candidates: list[str] = []
        for argument in str(raw_args).strip().split():
            try:
                money_to_units(argument)
            except ValueError:
                continue
            amount_candidates.append(argument)
        if len(amount_candidates) != 1:
            yield event.plain_result("用法：/fapi 转账 @用户 <金额>")
            return
        try:
            amount_units = money_to_units(amount_candidates[0])
        except ValueError:
            yield event.plain_result("转账金额必须是大于 0、最多 8 位小数的数字。")
            return
        if amount_units > self._transfer_max_units:
            yield event.plain_result(
                f"单次转账不能超过 {self._format_units(self._transfer_max_units)}。"
            )
            return

        target_qq_id = self._mentioned_target_id(event, command_context)
        if target_qq_id is None:
            yield event.plain_result("请只 @ 一名其他群成员：/fapi 转账 @用户 <金额>")
            return
        if target_qq_id == command_context.sender_id:
            yield event.plain_result("不能向自己转账。")
            return
        operation_key = self._game_operation_key(event, command_context, "transfer")
        if operation_key is None:
            yield event.plain_result("无法确认当前消息编号，为避免重复扣款，本次转账已取消。")
            return

        early_reply = ""
        try:
            async with self._game_command_lock:
                operation = await self.games.get_by_key(operation_key)
                if operation is None:
                    sender_binding, target_binding = await asyncio.gather(
                        self.bindings.get_by_qq(command_context.sender_id),
                        self.bindings.get_by_qq(target_qq_id),
                    )
                    if sender_binding is None:
                        early_reply = "当前账号尚未绑定 TokenRouter 账户，请先使用 /fapi 绑定。"
                    elif target_binding is None:
                        early_reply = "被 @ 用户尚未绑定 TokenRouter 账户，无法转账。"
                    else:
                        operation = (
                            await self.games.reserve_game(
                                operation_key=operation_key,
                                game_type="transfer",
                                group_id=command_context.group_id,
                                actor_qq_id=command_context.sender_id,
                                actor_user_id=sender_binding.tokenrouter_user_id,
                                target_qq_id=target_qq_id,
                                target_user_id=target_binding.tokenrouter_user_id,
                                debit_user_id=sender_binding.tokenrouter_user_id,
                                credit_user_id=target_binding.tokenrouter_user_id,
                                debit_units=amount_units,
                                credit_units=amount_units,
                                won=True,
                            )
                        ).operation
        except BindingDatabaseError as exc:
            logger.error("FAPI 查询转账绑定关系失败：%s", exc)
            yield event.plain_result("远程绑定数据库暂时不可用，请稍后重试。")
            return
        except (GameDatabaseError, GameOperationConflict) as exc:
            logger.error("FAPI 预留转账操作失败：%s", exc)
            yield event.plain_result("转账记录暂时不可用，请稍后重试。")
            return

        if early_reply:
            yield event.plain_result(early_reply)
            return
        assert operation is not None
        settled = await self._settle_game_operation(operation)
        yield event.plain_result(self._game_reply(settled))

    async def terminate(self) -> None:
        for attribute, task_name in (
            ("_daily_broadcast_task", "每日播报"),
            ("_game_worker_task", "游戏结算"),
            ("_reward_worker_task", "奖励补发"),
        ):
            task = getattr(self, attribute)
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("FAPI %s后台任务停止失败", task_name)
            finally:
                setattr(self, attribute, None)

        for resource_name, resource in (
            ("游戏数据库", self.games),
            ("奖励数据库", self.rewards),
            ("绑定数据库", self.bindings),
            ("TokenRouter 客户端", self.client),
        ):
            try:
                await resource.close()
            except Exception:
                logger.exception("FAPI 关闭%s失败", resource_name)

    async def _deliver_reward(self, job: RewardJob) -> bool:
        async with self._reward_delivery_lock:
            try:
                current = await self.rewards.get_reward_job(job.operation_id)
            except RewardDatabaseError as exc:
                logger.error("FAPI 读取奖励任务失败：%s", exc)
                return False
            if current is None:
                logger.error("FAPI 奖励任务不存在：%s", job.operation_id)
                return False
            if current.status == RewardJobStatus.SUCCEEDED:
                return True

            try:
                await self.rewards.record_reward_attempt(current.operation_id)
                await self.client.create_and_redeem_reward(
                    user_id=current.tokenrouter_user_id,
                    amount=units_to_decimal(current.amount_units),
                    redeem_code=current.redeem_code,
                    idempotency_key=current.redeem_code,
                    notes=(
                        f"FAPI kind={current.source_type} "
                        f"operation={current.operation_id}"
                    ),
                )
            except TokenRouterError as exc:
                retry_seconds = min(300, 2 ** min(current.attempt_count + 1, 8))
                if exc.retry_after is not None:
                    retry_seconds = max(
                        retry_seconds,
                        min(3600, math.ceil(exc.retry_after)),
                    )
                logger.warning(
                    "FAPI 奖励发放失败：operation=%s, status=%s, code=%s, error=%s",
                    current.operation_id,
                    exc.status_code,
                    exc.api_code,
                    exc,
                )
                with contextlib.suppress(RewardDatabaseError):
                    await self.rewards.record_reward_error(
                        current.operation_id,
                        str(exc),
                        retry_seconds=retry_seconds,
                    )
                return False
            except RewardDatabaseError as exc:
                logger.error("FAPI 记录奖励发放过程失败：%s", exc)
                return False
            except Exception:
                logger.exception("FAPI 奖励发放出现未预期错误")
                return False

            try:
                await self.rewards.mark_reward_succeeded(current.operation_id)
            except RewardDatabaseError:
                # 远端固定兑换码已完成；保留 pending 后重放可安全补齐本地状态。
                logger.exception(
                    "FAPI 奖励已到账但本地状态回写失败：operation=%s",
                    current.operation_id,
                )
            return True

    async def _reward_worker_loop(self) -> None:
        while True:
            try:
                jobs = await self.rewards.list_pending_jobs()
                for job in jobs:
                    await self._deliver_reward(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("FAPI 奖励补发任务执行失败")
            await asyncio.sleep(15)

    async def _settle_game_operation(
        self,
        operation: GameOperation,
        *,
        already_claimed: bool = False,
    ) -> GameOperation:
        async with self._game_delivery_lock:
            try:
                current = (
                    operation
                    if already_claimed
                    else await self.games.claim(
                        operation.operation_id,
                        lease_seconds=self._game_claim_lease_seconds,
                    )
                )
            except GameDatabaseError as exc:
                logger.error("FAPI 领取游戏结算任务失败：%s", exc)
                return operation
            if current is None:
                return await self._reload_game(operation)
            claim_token = current.lease_token
            if not claim_token:
                logger.error(
                    "FAPI 游戏结算任务缺少数据库租约：operation=%s",
                    current.operation_id,
                )
                return await self._reload_game(current)

            for _ in range(3):
                if current.status not in {
                    GameStatus.PENDING_DEBIT,
                    GameStatus.PENDING_CREDIT,
                    GameStatus.PENDING_REFUND,
                }:
                    return current

                try:
                    current = await self.games.renew_claim(
                        current.operation_id,
                        claim_token,
                        lease_seconds=self._game_claim_lease_seconds,
                    )
                except GameDatabaseError as exc:
                    logger.warning(
                        "FAPI 游戏结算租约已失效：operation=%s, error=%s",
                        current.operation_id,
                        exc,
                    )
                    return await self._reload_game(current)

                if current.status == GameStatus.PENDING_DEBIT:
                    user_id = current.debit_user_id
                    amount_units = current.debit_units
                    balance_operation = "subtract"
                    phase = "debit"
                elif current.status == GameStatus.PENDING_CREDIT:
                    if current.credit_user_id is None:
                        logger.error(
                            "FAPI 游戏派奖任务缺少收款用户：operation=%s",
                            current.operation_id,
                        )
                        await self.games.mark_pending_refund(
                            current.operation_id,
                            claim_token=claim_token,
                            lease_seconds=self._game_claim_lease_seconds,
                        )
                        current = await self._reload_game(current)
                        continue
                    user_id = current.credit_user_id
                    amount_units = current.credit_units
                    balance_operation = "add"
                    phase = "credit"
                else:
                    user_id = current.debit_user_id
                    amount_units = current.debit_units
                    balance_operation = "add"
                    phase = "refund"

                try:
                    await self.client.adjust_balance(
                        user_id=user_id,
                        amount=units_to_decimal(amount_units),
                        operation=balance_operation,
                        idempotency_key=(
                            f"fapi-game:v1:{current.operation_id}:{phase}"
                        ),
                        notes=(
                            f"FAPI game={current.game_type} phase={phase} "
                            f"operation={current.operation_id}"
                        ),
                    )
                except TokenRouterError as exc:
                    logger.warning(
                        "FAPI 游戏结算失败：operation=%s, phase=%s, "
                        "status=%s, code=%s, error=%s",
                        current.operation_id,
                        phase,
                        exc.status_code,
                        exc.api_code,
                        exc,
                    )
                    try:
                        if phase == "debit" and is_insufficient_balance_error(exc):
                            await self.games.mark_insufficient(
                                current.operation_id,
                                claim_token=claim_token,
                            )
                        elif phase == "debit" and not self._retryable_game_error(exc):
                            await self.games.mark_failed(
                                current.operation_id,
                                claim_token=claim_token,
                            )
                        elif phase == "credit" and not self._retryable_game_error(exc):
                            await self.games.mark_pending_refund(
                                current.operation_id,
                                claim_token=claim_token,
                                lease_seconds=self._game_claim_lease_seconds,
                            )
                            current = await self._reload_game(current)
                            continue
                        elif phase == "refund" and not self._retryable_game_error(exc):
                            status = await self.games.record_refund_error(
                                current.operation_id,
                                str(exc),
                                max_attempts=_GAME_REFUND_MAX_ATTEMPTS,
                                base_delay_seconds=self._game_retry_delay(exc),
                                claim_token=claim_token,
                            )
                            if status == GameStatus.MANUAL_REVIEW:
                                logger.error(
                                    "FAPI 游戏退款已转人工处理：operation=%s, error=%s",
                                    current.operation_id,
                                    exc,
                                )
                        else:
                            await self.games.record_error(
                                current.operation_id,
                                str(exc),
                                base_delay_seconds=self._game_retry_delay(exc),
                                claim_token=claim_token,
                            )
                    except GameDatabaseError as store_exc:
                        logger.error("FAPI 记录游戏结算错误失败：%s", store_exc)
                    return await self._reload_game(current)
                except Exception as exc:
                    logger.exception("FAPI 游戏结算出现未预期错误")
                    with contextlib.suppress(GameDatabaseError):
                        await self.games.record_error(
                            current.operation_id,
                            str(exc),
                            claim_token=claim_token,
                        )
                    return await self._reload_game(current)

                try:
                    if phase == "debit":
                        await self.games.mark_debited(
                            current.operation_id,
                            claim_token=claim_token,
                            lease_seconds=self._game_claim_lease_seconds,
                        )
                    elif phase == "credit":
                        await self.games.mark_succeeded(
                            current.operation_id,
                            claim_token=claim_token,
                        )
                    else:
                        await self.games.mark_refunded(
                            current.operation_id,
                            claim_token=claim_token,
                        )
                except GameDatabaseError:
                    # 远端写入使用固定幂等键；保留旧状态后重试可安全补齐。
                    logger.exception(
                        "FAPI 游戏余额已调整但本地状态回写失败：operation=%s, phase=%s",
                        current.operation_id,
                        phase,
                    )
                    return current
                current = await self._reload_game(current)
            return current

    async def _reload_game(self, fallback: GameOperation) -> GameOperation:
        try:
            return await self.games.get(fallback.operation_id) or fallback
        except GameDatabaseError as exc:
            logger.error("FAPI 重读游戏结算任务失败：%s", exc)
            return fallback

    async def _game_worker_loop(self) -> None:
        while True:
            try:
                operations = await self.games.claim_pending(
                    lease_seconds=self._game_claim_lease_seconds,
                )
                for operation in operations:
                    await self._settle_game_operation(
                        operation,
                        already_claimed=True,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("FAPI 游戏结算补偿任务执行失败")
            await asyncio.sleep(15)

    async def _daily_broadcast_loop(self) -> None:
        while True:
            now = datetime.now(self._reward_timezone)
            next_run = now.replace(
                hour=self._daily_broadcast_hour,
                minute=self._daily_broadcast_minute,
                second=0,
                microsecond=0,
            )
            if next_run <= now:
                next_run += timedelta(days=1)
            await asyncio.sleep(max(1.0, (next_run - now).total_seconds()))
            try:
                await self._send_daily_broadcast()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("FAPI 每日自动播报失败")

    async def _send_daily_broadcast(self) -> None:
        report_date, start_utc, end_utc = self._daily_stats_window()
        lease_token = await self.rewards.claim_daily_broadcast(
            report_date=report_date,
            target_umo=self._daily_broadcast_umo,
        )
        if lease_token is None:
            return
        overview = await self.client.get_dashboard_overview()
        stats = await self.rewards.get_daily_stats(
            reward_date=report_date,
            start_utc=start_utc,
            end_utc=end_utc,
        )
        from astrbot.api.event import MessageChain

        chain = MessageChain().message(
            self._daily_broadcast_reply(overview, stats)
        )
        await self.context.send_message(self._daily_broadcast_umo, chain)
        await self.rewards.mark_daily_broadcast_sent(
            report_date=report_date,
            target_umo=self._daily_broadcast_umo,
            lease_token=lease_token,
        )

    def _daily_stats_window(self) -> tuple[date, datetime, datetime]:
        now = datetime.now(self._reward_timezone)
        start_local = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
        end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
        return now.date(), start_utc, end_utc

    @staticmethod
    def _daily_broadcast_reply(
        overview: TokenRouterDashboardOverview,
        stats: DailyRewardStats,
    ) -> str:
        freshness = "（统计可能延迟）" if overview.stats_stale else ""
        return (
            f"FAPI 每日播报 {stats.reward_date.isoformat()}{freshness}\n"
            f"全站请求：{overview.today_requests:,}\n"
            f"全站 Token：{overview.today_tokens:,}\n"
            f"全站消费：{FapiPlugin._format_usd(overview.today_actual_cost)}\n"
            f"签到：{stats.checkin_count:,} 人 / "
            f"{FapiPlugin._format_units(stats.checkin_units)}\n"
            f"红包：创建 {stats.red_packet_count:,} 个 / "
            f"{FapiPlugin._format_units(stats.red_packet_units)}\n"
            f"红包到账：{stats.claim_count:,} 份 / "
            f"{FapiPlugin._format_units(stats.claim_units)}\n"
            f"实时吞吐：RPM {overview.rpm:,} / TPM {overview.tpm:,}"
        )

    def _ensure_background_workers(self) -> None:
        if self._reward_worker_task is None or self._reward_worker_task.done():
            self._reward_worker_task = asyncio.create_task(
                self._reward_worker_loop(),
                name="fapi-reward-worker",
            )
        if self._game_worker_task is None or self._game_worker_task.done():
            self._game_worker_task = asyncio.create_task(
                self._game_worker_loop(),
                name="fapi-game-worker",
            )
        if (
            self._daily_broadcast_enabled
            and not self._daily_broadcast_error
            and self._daily_broadcast_umo
            and (
                self._daily_broadcast_task is None
                or self._daily_broadcast_task.done()
            )
        ):
            self._daily_broadcast_task = asyncio.create_task(
                self._daily_broadcast_loop(),
                name="fapi-daily-broadcast",
            )

    @staticmethod
    def _validate_unique_table_names(table_names: dict[str, str]) -> None:
        owners_by_name: dict[str, list[str]] = {}
        display_names: dict[str, str] = {}
        for option_name, table_name in table_names.items():
            normalized = table_name.casefold()
            owners_by_name.setdefault(normalized, []).append(option_name)
            display_names.setdefault(normalized, table_name)
        duplicates = [
            f"{display_names[name]} ({', '.join(owners)})"
            for name, owners in owners_by_name.items()
            if len(owners) > 1
        ]
        if duplicates:
            raise ValueError("MySQL 数据表名称必须互不相同：" + "；".join(duplicates))

    @staticmethod
    def _retryable_game_error(exc: TokenRouterError) -> bool:
        return bool(
            exc.outcome_unknown
            or exc.status_code is None
            or exc.status_code in {408, 409, 425, 429}
            or exc.status_code >= 500
        )

    @staticmethod
    def _game_retry_delay(exc: TokenRouterError) -> int:
        if exc.retry_after is None:
            return 5
        return max(1, min(3600, math.ceil(exc.retry_after)))

    @staticmethod
    def _game_reply(operation: GameOperation) -> str:
        pending = {
            GameStatus.PENDING_DEBIT,
            GameStatus.PENDING_CREDIT,
            GameStatus.PENDING_REFUND,
        }
        if operation.game_type == "transfer":
            if operation.status in pending:
                return "转账正在结算，请稍后使用余额或账单确认。"
            if operation.status == GameStatus.MANUAL_REVIEW:
                return "转账入账异常，退款需要管理员人工处理。"
            if operation.status == GameStatus.INSUFFICIENT:
                return "余额不足，本次转账未发生余额变动。"
            if operation.status == GameStatus.REFUNDED:
                return "转账入账失败，扣除的金额已退回付款账户。"
            if operation.status == GameStatus.FAILED:
                return "转账失败，本次未扣款。"
            if operation.status == GameStatus.SUCCEEDED:
                return (
                    "转账成功\n"
                    f"金额：{FapiPlugin._format_units(operation.credit_units)}\n"
                    "金额已转入对方账户。"
                )
            return "转账状态无法识别，请稍后查询账单。"
        if operation.game_type == "robbery":
            backfired = FapiPlugin._is_robbery_backfire(operation)
            if operation.status in pending:
                if backfired:
                    return "打劫失败并触发反噬，余额正在结算，请稍后查询。"
                return "打劫结果已锁定，余额正在结算，请稍后查询。"
            if operation.status == GameStatus.MANUAL_REVIEW:
                if backfired:
                    return "反噬结算异常，退款需要管理员人工处理。"
                return "打劫结算异常，退款需要管理员人工处理。"
            if operation.status == GameStatus.INSUFFICIENT:
                if backfired:
                    return "打劫失败并触发反噬，但你的余额不足，本次没有余额变动。"
                return "对方余额不足，本次打劫未得手。"
            if operation.status == GameStatus.REFUNDED:
                if backfired:
                    return "反噬结算失败，扣除的金额已经退回你的账户。"
                return "转账结算失败，扣除的金额已经退回对方账户。"
            if operation.status == GameStatus.FAILED:
                if backfired:
                    return "反噬结算失败，本次未扣款。"
                return "打劫结算失败，本次未扣款。"
            if operation.won:
                return (
                    "打劫成功\n"
                    f"获得：{FapiPlugin._format_units(operation.credit_units)}\n"
                    "金额已从对方账户转入你的账户。"
                )
            if backfired:
                return (
                    "打劫失败，触发反噬\n"
                    f"损失：{FapiPlugin._format_units(operation.debit_units)}\n"
                    "金额已转入对方账户。"
                )
            return "打劫失败，本次没有余额变动。"

        return "游戏结算状态不可识别。"

    @staticmethod
    def _is_robbery_backfire(operation: GameOperation) -> bool:
        return bool(
            operation.game_type == "robbery"
            and not operation.won
            and operation.debit_units > 0
            and operation.credit_units == operation.debit_units
            and operation.debit_user_id == operation.actor_user_id
            and operation.target_user_id is not None
            and operation.credit_user_id == operation.target_user_id
        )

    @staticmethod
    def _checkin_success_reply(reservation: CheckinReservation) -> str:
        return (
            "签到成功\n"
            f"奖励：{FapiPlugin._format_units(reservation.job.amount_units)}\n"
            f"连续签到：{reservation.streak} 天\n"
            f"累计签到：{reservation.total_days} 天"
        )

    async def _private_admin_context(
        self,
        event: AstrMessageEvent,
    ) -> tuple[PrivateAdminContext | None, str | None]:
        try:
            platform = str(event.get_platform_name()).strip().casefold()
        except Exception:
            platform = ""
        if platform not in _SUPPORTED_QQ_PLATFORMS:
            return None, "该指令仅支持 QQ 机器人平台。"

        get_group_id = getattr(event, "get_group_id", None)
        if not callable(get_group_id):
            return None, "无法确认当前会话类型，请稍后重试。"
        try:
            group_id = str(get_group_id() or "").strip()
        except Exception:
            return None, "无法确认当前会话类型，请稍后重试。"
        if group_id:
            return None, "管理员管理指令只能在私聊中使用。"

        try:
            sender_id = str(event.get_sender_id()).strip()
        except Exception:
            sender_id = ""
        if not self._valid_platform_identifier(sender_id):
            return None, "无法识别当前 QQ 用户，请稍后重试。"

        is_admin = getattr(event, "is_admin", None)
        try:
            allowed = is_admin() if callable(is_admin) else False
            if inspect.isawaitable(allowed):
                allowed = await allowed
        except Exception:
            allowed = False
        if allowed is not True:
            return None, "该指令仅限 AstrBot 管理员私聊使用。"

        return PrivateAdminContext(platform, sender_id), None

    async def _resolve_private_admin_user(
        self,
        email: str,
    ) -> tuple[TokenRouterUser | None, str | None]:
        exact_email = email.strip()
        if not exact_email:
            return None, "请输入邮箱，邮箱必须完全一致。"
        if len(exact_email) > 254 or not self._looks_like_email(exact_email):
            return None, "邮箱格式不正确。"

        try:
            users = await self.client.find_users_by_exact_email(exact_email)
        except TokenRouterError as exc:
            logger.warning(
                "FAPI 管理员精确查询用户失败：status=%s, code=%s, error=%s",
                exc.status_code,
                exc.api_code,
                exc,
            )
            return None, self._api_error_reply(exc)
        except Exception:
            logger.exception("FAPI 管理员精确查询用户出现未预期错误")
            return None, "管理员查询服务暂时不可用，请稍后重试。"

        if not users:
            return None, "未找到邮箱完全匹配的中转站用户。"
        if len(users) > 1:
            return None, "存在多个相同邮箱用户，为避免误操作，本次管理操作已取消。"
        return users[0], None

    @staticmethod
    def _admin_balance_operation_key(
        event: AstrMessageEvent,
        admin_context: PrivateAdminContext,
        operation: str,
    ) -> str | None:
        digest = FapiPlugin._operation_key(
            event,
            admin_context.platform,
            f"private:{admin_context.sender_id}",
            f"admin-balance:{operation}",
        )
        return f"fapi-admin-balance:v1:{digest}" if digest else None

    @staticmethod
    def _safe_reply_field(value: object, *, maximum: int = 254) -> str:
        text = "".join(
            (
                character
                if unicodedata.category(character)
                not in {"Cc", "Cf", "Cs", "Zl", "Zp"}
                else " "
            )
            for character in str(value)
        )
        return text.translate(_REPLY_MARKUP_TRANSLATION)[:maximum]

    def _admin_stats_timezone(self):
        return getattr(self, "_reward_timezone", timezone.utc)

    def _admin_stats_timezone_name(self) -> str:
        zone = self._admin_stats_timezone()
        return str(getattr(zone, "key", "UTC"))

    @staticmethod
    def _dashboard_overview_reply(overview: TokenRouterDashboardOverview) -> str:
        freshness = "\n统计状态：聚合数据可能延迟" if overview.stats_stale else ""
        return (
            "今日全站概览（按中转站统计日）\n"
            f"请求：{overview.today_requests:,}\n"
            f"Token：{overview.today_tokens:,}\n"
            f"实际消费：{FapiPlugin._format_usd(overview.today_actual_cost)}\n"
            f"活跃用户：{overview.active_users:,} / 总用户 {overview.total_users:,}\n"
            f"今日新增用户：{overview.today_new_users:,}\n"
            f"API Key：活跃 {overview.active_api_keys:,} / 总计 "
            f"{overview.total_api_keys:,}\n"
            f"近5分钟：RPM {overview.rpm:,} / TPM {overview.tpm:,}\n"
            f"平均响应：{overview.average_duration_ms:,.2f} ms\n"
            f"{overview.resource_kind}：正常 {overview.normal_resources:,} / "
            f"异常 {overview.error_resources:,} / 限流 "
            f"{overview.rate_limit_resources:,} / 过载 "
            f"{overview.overload_resources:,} / 总计 {overview.total_resources:,}"
            f"{freshness}"
        )

    @staticmethod
    def _rpm_usage_text(used: int, limit: int) -> str:
        return f"{used:,} / {'不限' if limit == 0 else f'{limit:,}'}"

    @staticmethod
    def _user_rpm_reply(
        user: TokenRouterUser,
        status: TokenRouterUserRPMStatus,
    ) -> str:
        lines = [
            "用户 RPM 状态（当前分钟）",
            f"邮箱：{FapiPlugin._safe_reply_field(user.email, maximum=254)}"
            f"（ID {user.user_id}）",
            f"用户名：{FapiPlugin._safe_reply_field(user.username, maximum=64)}",
            f"总 RPM：{FapiPlugin._rpm_usage_text(status.used, status.limit)}",
        ]
        if not status.per_group:
            lines.append("分组：无绑定分组 API Key")
            return "\n".join(lines)

        lines.append("分组 RPM：")
        shown_groups = status.per_group[:8]
        source_labels = {"group": "分组", "override": "用户覆盖", "": "未知"}
        for group in shown_groups:
            group_name = FapiPlugin._safe_reply_field(
                group.group_name or f"分组 {group.group_id}",
                maximum=48,
            )
            lines.append(
                f"- {group_name}："
                f"{FapiPlugin._rpm_usage_text(group.used, group.limit)} "
                f"({source_labels[group.source]})"
            )
        remaining = len(status.per_group) - len(shown_groups)
        if remaining > 0:
            lines.append(f"另有 {remaining} 个分组未显示")
        return "\n".join(lines)

    @staticmethod
    def _user_ranking_reply(ranking: TokenRouterUserRanking) -> str:
        lines = [
            f"用户消费排行（{ranking.start_date}）",
            f"全站合计：{FapiPlugin._format_usd(ranking.total_actual_cost)} / "
            f"{ranking.total_requests:,} 次请求 / {ranking.total_tokens:,} Token",
        ]
        if not ranking.items:
            lines.append("今日暂无用户消费记录。")
            return "\n".join(lines)

        for index, item in enumerate(ranking.items, start=1):
            username = FapiPlugin._safe_reply_field(
                item.username or f"用户 {item.user_id}",
                maximum=32,
            )
            lines.append(
                f"{index}. {username}（ID {item.user_id}） "
                f"{FapiPlugin._format_usd(item.actual_cost)} / "
                f"{item.requests:,} 次 / {item.tokens:,} Token"
            )
        return "\n".join(lines)

    async def _reward_ranking_reply(self, *, source_type: str, title: str) -> str:
        entries = await self.rewards.get_ranking(source_type=source_type, limit=10)
        if not entries:
            return f"{title}\n暂无已到账记录。"
        bindings = await self.bindings.get_by_user_ids(
            {entry.tokenrouter_user_id for entry in entries}
        )
        lines = [title]
        for index, entry in enumerate(entries, start=1):
            binding = bindings.get(entry.tokenrouter_user_id)
            identity = (
                self._mask_email(binding.email)
                if binding is not None
                else "已解绑用户"
            )
            label = "天" if source_type == "checkin" else "份"
            lines.append(
                f"{index}. {identity}：{entry.count} {label} / "
                f"{self._format_units(entry.amount_units)}"
            )
        return "\n".join(lines)

    async def _ledger_reply(
        self,
        tokenrouter_user_id: int,
        *,
        email: str,
        admin: bool,
    ) -> str:
        reward_entries, game_entries = await asyncio.gather(
            self.rewards.get_user_ledger(tokenrouter_user_id, limit=20),
            self.games.get_user_operations(tokenrouter_user_id, limit=20),
        )
        rows: list[tuple[float, str]] = []
        reward_labels = {"checkin": "签到", "red_packet": "红包"}
        for entry in reward_entries:
            status = "已到账" if entry.status == RewardJobStatus.SUCCEEDED else "待补发"
            label = reward_labels.get(entry.source_type, "奖励")
            rows.append(
                (
                    self._datetime_sort_key(entry.created_at),
                    f"{self._format_ledger_time(entry.created_at)} {label} "
                    f"+{self._format_units(entry.amount_units)}（{status}）",
                )
            )
        for operation in game_entries:
            line = self._game_ledger_line(operation, tokenrouter_user_id)
            if line:
                rows.append((self._datetime_sort_key(operation.created_at), line))
        rows.sort(key=lambda item: item[0], reverse=True)

        shown_email = (
            self._safe_reply_field(email)
            if admin
            else self._mask_email(email)
        )
        lines = [f"插件账单：{shown_email}"]
        if not rows:
            lines.append("暂无签到、红包或打劫记录。")
            return "\n".join(lines)
        lines.extend(line for _, line in rows[:10])
        lines.append("仅显示最近 10 条插件记录，不含站内其他消费与调账。")
        return "\n".join(lines)

    def _game_ledger_line(
        self,
        operation: GameOperation,
        tokenrouter_user_id: int,
    ) -> str:
        timestamp = self._format_ledger_time(operation.created_at)
        is_debit = operation.debit_user_id == tokenrouter_user_id
        is_credit = operation.credit_user_id == tokenrouter_user_id
        if not is_debit and not is_credit:
            return ""
        role = "支出" if is_debit else "收入"
        units = operation.debit_units if is_debit else operation.credit_units
        sign = "-" if is_debit else "+"
        status_labels = {
            GameStatus.PENDING_DEBIT: "待扣款",
            GameStatus.PENDING_CREDIT: "待入账" if is_credit else "已扣款，待对方入账",
            GameStatus.PENDING_REFUND: "退款中",
            GameStatus.SUCCEEDED: "已完成",
            GameStatus.INSUFFICIENT: "余额不足，未变动",
            GameStatus.FAILED: "失败，未变动",
            GameStatus.REFUNDED: "已退款",
            GameStatus.MANUAL_REVIEW: "退款待人工处理",
        }
        status = status_labels.get(operation.status, operation.status.value)
        if is_credit and operation.status in {
            GameStatus.PENDING_REFUND,
            GameStatus.MANUAL_REVIEW,
        }:
            amount = self._format_units(0)
            sign = ""
            status = (
                "入账失败，付款方退款中"
                if operation.status == GameStatus.PENDING_REFUND
                else "入账失败，付款方退款待人工处理"
            )
        elif operation.status in {
            GameStatus.INSUFFICIENT,
            GameStatus.FAILED,
            GameStatus.REFUNDED,
        }:
            amount = self._format_units(0)
            sign = ""
        else:
            amount = self._format_units(units)
        label = "转账" if operation.game_type == "transfer" else "打劫"
        return f"{timestamp} {label}{role} {sign}{amount}（{status}）"

    def _admin_packet_reply(self, packet) -> str:
        return (
            f"红包 #{packet.packet_id}\n"
            f"状态：{self._safe_reply_field(packet.status)}\n"
            f"创建者：{self._safe_reply_field(packet.creator_qq_id)}\n"
            f"群：{self._safe_reply_field(packet.group_id)}\n"
            f"总额：{self._format_units(packet.total_units)}\n"
            f"剩余：{self._format_units(packet.remaining_units)} / "
            f"{packet.remaining_count} 份\n"
            f"已到账：{packet.credited_count} 份\n"
            f"到期：{self._format_ledger_time(packet.expires_at)}"
        )

    async def _admin_packet_list_reply(self, limit: int) -> str:
        packets = await self.rewards.list_red_packets(limit)
        if not packets:
            return "红包列表\n暂无红包记录。"
        lines = ["红包列表"]
        for packet in packets:
            lines.append(
                f"#{packet.packet_id} {packet.status} "
                f"{self._format_units(packet.total_units)} / 剩余 "
                f"{self._format_units(packet.remaining_units)} / "
                f"{packet.remaining_count} 份"
            )
        return "\n".join(lines)

    async def _global_activity_reply(self, limit: int) -> str:
        reward_entries, game_entries = await asyncio.gather(
            self.rewards.list_recent_ledger(limit),
            self.games.list_recent_operations(limit),
        )
        rows: list[tuple[float, str]] = []
        for entry in reward_entries:
            rows.append(
                (
                    self._datetime_sort_key(entry.created_at),
                    f"{self._format_ledger_time(entry.created_at)} 奖励 "
                    f"{entry.source_type} +{self._format_units(entry.amount_units)} "
                    f"({entry.status.value})",
                )
            )
        for operation in game_entries:
            units = operation.debit_units or operation.credit_units
            rows.append(
                (
                    self._datetime_sort_key(operation.created_at),
                    f"{self._format_ledger_time(operation.created_at)} 游戏 "
                    f"{operation.game_type} {self._format_units(units)} "
                    f"({operation.status.value})",
                )
            )
        rows.sort(key=lambda item: item[0], reverse=True)
        if not rows:
            return "全局流水\n暂无插件流水。"
        return "\n".join(["全局流水", *(text for _, text in rows[:limit])])

    async def _blocked_users_reply(self, limit: int) -> str:
        bindings = await self.bindings.list_blocked(limit)
        if not bindings:
            return "封禁列表\n当前没有被封禁的绑定用户。"
        lines = ["封禁列表"]
        lines.extend(
            f"{self._mask_email(binding.email)}（ID {binding.tokenrouter_user_id}）"
            for binding in bindings
        )
        return "\n".join(lines)

    async def _refund_packet(self, packet_id: int) -> str:
        packet = await self.rewards.reserve_red_packet_refund(packet_id)
        if packet is None:
            return "红包不存在、没有可退余额，或已经完成退款。"
        binding = await self.bindings.get_by_qq_for_admin(packet.creator_qq_id)
        if binding is None:
            return "创建者绑定记录不存在，红包退款已保留待人工处理。"
        try:
            await self.client.adjust_balance(
                user_id=binding.tokenrouter_user_id,
                amount=units_to_decimal(packet.remaining_units),
                operation="add",
                idempotency_key=f"fapi-red-packet-refund:v1:{packet.packet_id}",
                notes=f"FAPI admin red packet refund packet={packet.packet_id}",
            )
        except TokenRouterError as exc:
            logger.warning(
                "FAPI 管理员红包退款失败：packet=%s, status=%s, code=%s, error=%s",
                packet.packet_id, exc.status_code, exc.api_code, exc,
            )
            return "红包退款结果暂未确认，已保留退款状态，可重试该命令。"
        if not await self.rewards.mark_red_packet_refunded(packet.packet_id):
            return "红包退款已入账，本地状态等待下一次重试确认。"
        return (
            f"红包 #{packet.packet_id} 已退款给创建者："
            f"{self._format_units(packet.remaining_units)}。"
        )

    async def _incomplete_tasks_reply(self, limit: int) -> str:
        reward_jobs, game_operations = await asyncio.gather(
            self.rewards.list_incomplete_jobs(limit=limit),
            self.games.list_incomplete(limit=limit),
        )
        tasks: list[tuple[float, str]] = []
        for job in reward_jobs:
            error = self._safe_reply_field(job.operation_id, maximum=36)
            tasks.append(
                (
                    self._datetime_sort_key(job.created_at),
                    f"奖励 {error} | {job.source_type} | pending | 尝试 {job.attempt_count}",
                )
            )
        for operation in game_operations:
            operation_id = self._safe_reply_field(operation.operation_id, maximum=36)
            tasks.append(
                (
                    self._datetime_sort_key(operation.created_at),
                    f"游戏 {operation_id} | {operation.game_type} | "
                    f"{operation.status.value} | 尝试 {operation.attempt_count}",
                )
            )
        tasks.sort(key=lambda item: item[0])
        if not tasks:
            return "未完成任务\n当前没有待补偿或待人工处理的任务。"
        lines = ["未完成任务"]
        lines.extend(text for _, text in tasks[:limit])
        lines.append("重试：/fapi 管理 重试 <完整任务编号>")
        return "\n".join(lines)

    async def _retry_task(self, operation_id: str) -> str:
        if len(operation_id) > 64 or not operation_id.isascii():
            return "任务编号格式不正确。"
        reward_job = await self.rewards.get_reward_job(operation_id)
        if reward_job is not None:
            if reward_job.status == RewardJobStatus.SUCCEEDED:
                return "该奖励任务已经完成，不会重复发放。"
            if await self.rewards.retry_reward_job(operation_id):
                return "奖励任务已重新排队，固定兑换码不变，不会重复入账。"
            return "奖励任务状态已经变化，请重新查询。"

        operation = await self.games.get(operation_id)
        if operation is None:
            return "未找到该任务编号。"
        if operation.status == GameStatus.MANUAL_REVIEW:
            await self.games.retry_manual_review(operation_id)
            return "人工退款任务已重新排队，固定幂等键不变。"
        if operation.status in {
            GameStatus.PENDING_DEBIT,
            GameStatus.PENDING_CREDIT,
            GameStatus.PENDING_REFUND,
        }:
            if await self.games.retry_pending(operation_id):
                return "游戏结算任务已提前重试，固定幂等键不变。"
            return "游戏任务状态已经变化，请重新查询。"
        return f"该游戏任务已是终态（{operation.status.value}），不会重复处理。"

    @staticmethod
    def _datetime_sort_key(value: datetime | None) -> float:
        if value is None:
            return 0.0
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return normalized.timestamp()

    def _format_ledger_time(self, value: datetime | None) -> str:
        if value is None:
            return "时间未知"
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return normalized.astimezone(self._reward_timezone).strftime("%m-%d %H:%M")

    def _group_command_context(
        self,
        event: AstrMessageEvent,
    ) -> tuple[GroupCommandContext | None, str | None]:
        if not self._valid_platform_identifier(self._allowed_group_id):
            return None, "插件尚未配置指定群，请联系管理员。"

        try:
            platform = str(event.get_platform_name()).strip().casefold()
        except Exception:
            platform = ""
        if platform not in _SUPPORTED_QQ_PLATFORMS:
            return None, "该指令仅支持 QQ 机器人平台。"

        get_group_id = getattr(event, "get_group_id", None)
        try:
            group_id = str(get_group_id() or "").strip() if callable(get_group_id) else ""
        except Exception:
            group_id = ""
        if not self._valid_platform_identifier(group_id):
            return None, "该指令只能在管理员配置的指定群中使用。"
        if group_id != self._allowed_group_id:
            return None, "该指令只能在管理员配置的指定群中使用。"

        try:
            sender_id = str(event.get_sender_id()).strip()
        except Exception:
            sender_id = ""
        if not self._valid_platform_identifier(sender_id):
            return None, "无法识别当前 QQ 用户，请稍后重试。"

        return GroupCommandContext(platform, sender_id, group_id), None

    @staticmethod
    def _valid_platform_identifier(value: str) -> bool:
        return bool(
            value
            and len(value) <= 128
            and not any(
                character.isspace()
                or ord(character) < 32
                or 127 <= ord(character) <= 159
                for character in value
            )
        )

    async def _refund_red_packet_creation(
        self,
        tokenrouter_user_id: int,
        total_units: int,
        creation_key: str,
    ) -> bool:
        try:
            await self.client.adjust_balance(
                user_id=tokenrouter_user_id,
                amount=units_to_decimal(total_units),
                operation="add",
                idempotency_key=f"{creation_key}:refund",
                notes=f"FAPI red packet creation refund creation={creation_key}",
            )
            return True
        except TokenRouterError as exc:
            logger.error(
                "FAPI 红包创建退款失败：user=%s, amount_units=%s, "
                "status=%s, code=%s, error=%s",
                tokenrouter_user_id,
                total_units,
                exc.status_code,
                exc.api_code,
                exc,
            )
        except Exception:
            logger.exception("FAPI 红包创建退款出现未预期错误")
        return False

    @staticmethod
    def _red_packet_creation_key(
        event: AstrMessageEvent,
        platform: str,
        group_id: str,
    ) -> str | None:
        digest = FapiPlugin._operation_key(
            event,
            platform,
            group_id,
            "",
        )
        return f"fapi-red-packet:v1:{digest}" if digest else None

    @staticmethod
    def _operation_key(
        event: AstrMessageEvent,
        platform: str,
        group_id: str,
        operation_type: str,
    ) -> str | None:
        message_obj = getattr(event, "message_obj", None)
        message_id = str(getattr(message_obj, "message_id", "") or "").strip()
        raw = getattr(message_obj, "raw_message", None)
        if not message_id and isinstance(raw, dict):
            message_id = str(raw.get("message_id") or "").strip()
        get_message_id = getattr(event, "get_message_id", None)
        if not message_id and callable(get_message_id):
            try:
                message_id = str(get_message_id() or "").strip()
            except Exception:
                return None
        if message_id:
            payload = f"{platform}\0{group_id}\0{message_id}"
            if operation_type:
                payload = f"{operation_type}\0{payload}"
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            return digest
        return None

    @staticmethod
    def _game_operation_key(
        event: AstrMessageEvent,
        command_context: GroupCommandContext,
        game_type: str,
    ) -> str | None:
        digest = FapiPlugin._operation_key(
            event,
            command_context.platform,
            command_context.group_id,
            game_type,
        )
        return f"fapi-game:v1:{game_type}:{digest}" if digest else None

    @staticmethod
    def _mentioned_target_id(
        event: AstrMessageEvent,
        command_context: GroupCommandContext,
    ) -> str | None:
        sender_id = command_context.sender_id
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        candidates: set[str] = set()

        get_self_id = getattr(event, "get_self_id", None)
        try:
            self_id = str(get_self_id() or "").strip() if callable(get_self_id) else ""
        except Exception:
            self_id = ""
        if not self_id:
            self_id = str(getattr(message_obj, "self_id", "") or "").strip()

        if command_context.platform == "qq_official":
            mentions = FapiPlugin._object_field(raw, "mentions") or []
            raw_data = FapiPlugin._object_field(raw, "raw_data")
            if not mentions and isinstance(raw_data, dict):
                mentions = raw_data.get("mentions") or []
            for mention in mentions:
                is_you = FapiPlugin._object_field(mention, "is_you")
                if is_you is True:
                    continue
                member_openid = str(
                    FapiPlugin._object_field(mention, "member_openid") or ""
                ).strip()
                mention_id = str(
                    FapiPlugin._object_field(mention, "id") or ""
                ).strip()
                if member_openid and mention_id and member_openid != mention_id:
                    return None
                if member_openid:
                    target_id = member_openid
                elif is_you is False:
                    target_id = mention_id
                else:
                    continue
                if FapiPlugin._valid_platform_identifier(target_id):
                    candidates.add(target_id)
        else:
            get_messages = getattr(event, "get_messages", None)
            try:
                components = list(get_messages() or []) if callable(get_messages) else []
            except Exception:
                components = []
            if not components:
                components = list(getattr(message_obj, "message", None) or [])
            for component in components:
                component_type = getattr(component, "type", "")
                component_type = getattr(component_type, "value", component_type)
                if (
                    str(component_type).casefold() != "at"
                    and component.__class__.__name__.casefold() != "at"
                ):
                    continue
                target_id = str(getattr(component, "qq", "") or "").strip()
                if FapiPlugin._valid_platform_identifier(target_id):
                    candidates.add(target_id)

            if isinstance(raw, dict):
                for segment in raw.get("message") or []:
                    if not isinstance(segment, dict) or segment.get("type") != "at":
                        continue
                    data = segment.get("data")
                    if not isinstance(data, dict):
                        continue
                    target_id = str(data.get("qq") or "").strip()
                    if FapiPlugin._valid_platform_identifier(target_id):
                        candidates.add(target_id)

        candidates.discard("")
        candidates.discard("all")
        candidates.discard(sender_id)
        if self_id:
            candidates.discard(self_id)
        return next(iter(candidates)) if len(candidates) == 1 else None

    @staticmethod
    def _object_field(value: object, field: str):
        if isinstance(value, dict):
            return value.get(field)
        return getattr(value, field, None)

    @staticmethod
    def _looks_like_email(value: str) -> bool:
        if not 3 <= len(value) <= 254 or any(
            character.isspace()
            or ord(character) < 32
            or 127 <= ord(character) <= 159
            for character in value
        ):
            return False
        local_part, separator, domain = value.rpartition("@")
        return bool(
            separator
            and local_part
            and "." in domain
            and not domain.startswith(".")
            and not domain.endswith(".")
        )

    @staticmethod
    def _mask_email(value: str) -> str:
        local_part, separator, domain = value.rpartition("@")
        if not separator or not local_part or not domain:
            return "***"
        visible = local_part[:2] if len(local_part) > 1 else local_part[:1]
        return FapiPlugin._safe_reply_field(f"{visible}...@{domain}", maximum=254)

    @staticmethod
    def _format_usd(value) -> str:
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("金额格式不正确") from exc
        if not decimal_value.is_finite():
            raise ValueError("金额必须是有限数字")
        if abs(decimal_value) < Decimal("0.000000005"):
            decimal_value = Decimal(0)
        whole, fraction = f"{decimal_value:,.8f}".split(".")
        fraction = fraction.rstrip("0")
        fraction = fraction.ljust(2, "0")
        # QQ 官方机器人默认使用 Markdown，美元符号会被配对解析为公式。
        return f"USD {whole}.{fraction}"

    @staticmethod
    def _parse_optional_packet_id(value: object) -> int | None:
        text = str(value).strip()
        if not text:
            return 0
        try:
            packet_id = int(text)
        except (TypeError, ValueError):
            return None
        return packet_id if packet_id >= 0 else None

    @staticmethod
    def _format_units(units: int) -> str:
        return FapiPlugin._format_usd(units_to_decimal(units))

    @staticmethod
    def _format_duration(seconds: int) -> str:
        safe_seconds = max(0, int(seconds))
        if safe_seconds == 0:
            return "无需等待"
        hours, remainder = divmod(safe_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        parts: list[str] = []
        if hours:
            parts.append(f"{hours} 小时")
        if minutes:
            parts.append(f"{minutes} 分钟")
        if seconds and not hours:
            parts.append(f"{seconds} 秒")
        return " ".join(parts)

    @staticmethod
    def _api_error_reply(exc: TokenRouterError) -> str:
        if exc.status_code in {401, 403}:
            return "中转站鉴权失败，请联系管理员检查插件配置。"
        if exc.status_code == 429:
            return "中转站查询太频繁，请稍后再试。"
        return "中转站用户查询失败，请稍后重试。"

    @staticmethod
    def _admin_adjustment_error_reply(exc: TokenRouterError) -> str:
        if exc.status_code in {401, 403}:
            return "中转站鉴权失败，请检查插件配置。"
        if exc.status_code == 409:
            return "余额调整发生幂等冲突，请先核对用户余额，不要重复操作。"
        if exc.status_code == 429:
            return "中转站请求太频繁，本次余额调整未完成，请稍后再试。"
        return "中转站余额调整失败，本次操作未完成。"

    @staticmethod
    def _admin_read_error_reply(exc: TokenRouterError) -> str:
        if exc.status_code in {401, 403}:
            return "中转站鉴权失败，请检查插件配置。"
        if exc.status_code == 429:
            return "中转站查询太频繁，请稍后再试。"
        if exc.status_code == 501 or str(exc.api_code or "").upper() == "RPM_STATUS_UNAVAILABLE":
            return "中转站 RPM 状态服务当前不可用。"
        return "管理数据查询暂时不可用，请稍后重试。"
