from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import math
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .binding_store import BindStatus, BindingDatabaseError, MySQLBindingStore
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
    TokenRouterError,
    is_insufficient_balance_error,
)


_SUPPORTED_QQ_PLATFORMS = frozenset({"aiocqhttp", "qq_official"})
_GAME_REFUND_MAX_ATTEMPTS = 5
_SLOT_ANIMATION_PATH = Path(__file__).resolve().parent / "assets" / "slot_machine.gif"
_SLOT_ANIMATION_DELAY_SECONDS = 1.5


@dataclass(frozen=True, slots=True)
class GroupCommandContext:
    platform: str
    sender_id: str
    group_id: str


@register(
    "astrbot_plugin_fapi",
    "FAPI",
    "QQ 与 TokenRouter 账号互通、签到、群红包和余额游戏",
    "3.5.0",
)
class FapiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._allowed_group_id = str(config.get("allowed_group_id", "")).strip()
        table_names = {
            "mysql_table": str(
                config.get("mysql_table", "astrbot_fapi_bindings")
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
        self._game_claim_lease_seconds = max(
            180,
            min(3600, math.ceil(request_timeout) * 4 + 60),
        )
        self._reward_config_error = ""
        self._game_config_error = ""
        try:
            self._reward_timezone = ZoneInfo(
                str(config.get("reward_timezone", "Asia/Shanghai")).strip()
            )
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
        except (ValueError, ZoneInfoNotFoundError) as exc:
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
            self._slot_enabled = bool(config.get("slot_enabled", True))
            self._slot_animation_enabled = bool(
                config.get("slot_animation_enabled", True)
            )
            self._slot_animation_delay_seconds = _SLOT_ANIMATION_DELAY_SECONDS
            self._slot_cost_units = money_to_units(config.get("slot_cost", "0.10"))
            self._slot_prize_units = money_to_units(
                config.get("slot_prize", "0.20")
            )
            self._slot_success_rate = int(config.get("slot_success_rate", 40))
            self._slot_cooldown_seconds = int(
                config.get("slot_cooldown_seconds", 10)
            )
            for name, rate in (
                ("打劫", self._robbery_success_rate),
                ("打劫反噬", self._robbery_backfire_rate),
                ("老虎机", self._slot_success_rate),
            ):
                if not 0 <= rate <= 100:
                    raise ValueError(f"{name}成功率必须在 0 到 100 之间")
            for name, cooldown in (
                ("打劫", self._robbery_cooldown_seconds),
                ("老虎机", self._slot_cooldown_seconds),
            ):
                if not 0 <= cooldown <= 86400:
                    raise ValueError(f"{name}冷却时间必须在 0 到 86400 秒之间")
            if self._slot_prize_units < self._slot_cost_units:
                raise ValueError("老虎机奖金不能小于单次投入")
        except ValueError as exc:
            self._game_config_error = str(exc)

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
        self._ensure_background_workers()

    @filter.command_group("fapi")
    def fapi(self):
        """FAPI 中转站账号绑定。"""

    @fapi.command("帮助", alias={"help", "菜单"})
    async def help_menu(self, event: AstrMessageEvent):
        """显示 FAPI 指令菜单。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert command_context is not None

        if command_context.platform == "qq_official":
            binding_usage = "/fapi 绑定 <用户名> <完整邮箱>"
            red_packet_permission = "红包创建：仅 AstrBot 管理员"
        else:
            binding_usage = (
                "/fapi 绑定 <用户名>\n"
                "/fapi 绑定 <用户名> <完整邮箱>"
            )
            red_packet_permission = "红包创建：群主、群管理员或 AstrBot 管理员"

        yield event.plain_result(
            "FAPI 功能菜单\n"
            f"{binding_usage}\n"
            "/fapi 绑定状态\n"
            "/fapi 余额\n"
            "/fapi 签到\n"
            "/fapi 红包 <总金额> <份数>\n"
            "/fapi 抢红包 [红包编号]\n"
            "/fapi 红包状态 [红包编号]\n"
            "/fapi 打劫 @用户\n"
            "/fapi 老虎机\n"
            "/fapi 帮助\n\n"
            f"{red_packet_permission}"
        )

    @fapi.command("绑定", alias={"bind"})
    async def bind(
        self,
        event: AstrMessageEvent,
        username: str = "",
        email: str = "",
    ):
        """按精确用户名和邮箱绑定中转站账号。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert command_context is not None
        qq_id = command_context.sender_id

        exact_username = username.strip()
        if not exact_username:
            yield event.plain_result(
                "请输入用户名：/fapi 绑定 <用户名>\n"
                "非 QQ 邮箱账号请使用：/fapi 绑定 <用户名> <完整邮箱>"
            )
            return

        supplied_email = email.strip()
        if not supplied_email and command_context.platform != "aiocqhttp":
            yield event.plain_result(
                "QQ 官方机器人无法取得真实 QQ 号，请使用："
                "/fapi 绑定 <用户名> <完整邮箱>"
            )
            return
        target_email = supplied_email or f"{qq_id}@qq.com"
        if supplied_email and not self._looks_like_email(supplied_email):
            yield event.plain_result(
                "邮箱格式不完整，请使用：/fapi 绑定 <用户名> <完整邮箱>"
            )
            return

        try:
            users = await self.client.find_users_by_exact_username(exact_username)
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
                f"未找到用户名完全匹配“{exact_username}”的中转站用户，绑定已暂停。"
            )
            return

        matches = [
            user
            for user in users
            if user.email == target_email
        ]
        if not matches:
            if not supplied_email:
                yield event.plain_result(
                    "该用户名存在，但邮箱不是当前 QQ 对应的 "
                    f"{qq_id}@qq.com。请使用：/fapi 绑定 {exact_username} <完整邮箱>"
                )
            else:
                yield event.plain_result(
                    "用户名与邮箱未同时完全匹配同一个中转站用户，绑定已暂停。"
                )
            return
        if len(matches) > 1:
            yield event.plain_result(
                "存在多个用户名和邮箱均相同的中转站用户，无法安全绑定，请联系管理员。"
            )
            return

        user = matches[0]

        try:
            result = await self.bindings.bind(
                qq_id=qq_id,
                tokenrouter_user_id=user.user_id,
                email=user.email,
            )
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
            f"用户名：{summary.username}\n"
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
        """由群管理创建平台余额红包。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert command_context is not None
        qq_id = command_context.sender_id
        group_id = command_context.group_id
        if not await self._can_create_red_packet(
            event,
            command_context.platform,
            group_id,
            qq_id,
        ):
            if command_context.platform == "qq_official":
                yield event.plain_result("QQ 官方机器人仅允许 AstrBot 管理员创建红包。")
            else:
                yield event.plain_result("只有群主、群管理员或机器人管理员可以创建红包。")
            return
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
        except RewardBudgetExhausted:
            yield event.plain_result("今天的平台红包预算不足，无法创建。")
            return
        except RewardDatabaseError as exc:
            logger.error("FAPI 创建群红包失败：%s", exc)
            yield event.plain_result("红包数据库暂时不可用，请稍后重试。")
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
        if (
            settled.game_type == "slots"
            and self._slot_animation_enabled
            and _SLOT_ANIMATION_PATH.is_file()
        ):
            try:
                yield event.image_result(str(_SLOT_ANIMATION_PATH))
                await asyncio.sleep(self._slot_animation_delay_seconds)
            except Exception:
                logger.exception("FAPI 老虎机动画构造失败")
        yield event.plain_result(self._game_reply(settled))

    @fapi.command("老虎机", alias={"slot"})
    async def slot_machine(self, event: AstrMessageEvent):
        """扣除固定投入并按配置概率派发固定奖金。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert command_context is not None
        if self._game_config_error:
            yield event.plain_result("游戏配置不正确，请联系管理员检查插件配置。")
            return
        if not self._slot_enabled:
            yield event.plain_result("老虎机功能当前未开启。")
            return

        operation_key = self._game_operation_key(
            event,
            command_context,
            "slots",
        )
        if operation_key is None:
            yield event.plain_result("无法确认当前消息编号，为避免重复扣款，本次操作已取消。")
            return

        early_reply = ""
        try:
            async with self._game_command_lock:
                operation = await self.games.get_by_key(operation_key)
                if operation is None:
                    binding = await self.bindings.get_by_qq(
                        command_context.sender_id
                    )
                    if binding is None:
                        early_reply = (
                            "当前账号尚未绑定中转站账号，请先使用 /fapi 绑定。"
                        )
                    else:
                        won = roll_success(self._slot_success_rate)
                        reservation = await self.games.reserve_game(
                            operation_key=operation_key,
                            game_type="slots",
                            group_id=command_context.group_id,
                            actor_qq_id=command_context.sender_id,
                            actor_user_id=binding.tokenrouter_user_id,
                            debit_user_id=binding.tokenrouter_user_id,
                            credit_user_id=(
                                binding.tokenrouter_user_id if won else None
                            ),
                            debit_units=self._slot_cost_units,
                            credit_units=self._slot_prize_units if won else 0,
                            won=won,
                            cooldown_seconds=self._slot_cooldown_seconds,
                        )
                        operation = reservation.operation
        except GameCooldownActive as exc:
            yield event.plain_result(
                f"老虎机冷却中，请在 {exc.retry_after_seconds} 秒后再试。"
            )
            return
        except BindingDatabaseError as exc:
            logger.error("FAPI 查询老虎机绑定关系失败：%s", exc)
            yield event.plain_result("远程绑定数据库暂时不可用，请稍后重试。")
            return
        except (GameDatabaseError, GameOperationConflict) as exc:
            logger.error("FAPI 预留老虎机操作失败：%s", exc)
            yield event.plain_result("游戏数据库暂时不可用，请稍后重试。")
            return

        if early_reply:
            yield event.plain_result(early_reply)
            return
        settled = await self._settle_game_operation(operation)
        if self._slot_animation_enabled and _SLOT_ANIMATION_PATH.is_file():
            try:
                yield event.image_result(str(_SLOT_ANIMATION_PATH))
                await asyncio.sleep(self._slot_animation_delay_seconds)
            except Exception:
                logger.exception("FAPI slot animation construction failed")
        yield event.plain_result(self._game_reply(settled))

    async def terminate(self) -> None:
        for attribute, task_name in (
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

        if operation.status in pending:
            return "老虎机结果已锁定，余额正在结算，请稍后查询。"
        if operation.status == GameStatus.MANUAL_REVIEW:
            return "老虎机结算异常，退款需要管理员人工处理。"
        if operation.status == GameStatus.INSUFFICIENT:
            return "余额不足，无法支付本次老虎机投入。"
        if operation.status == GameStatus.REFUNDED:
            return "老虎机结算失败，本次投入已经退回账户。"
        if operation.status == GameStatus.FAILED:
            return "老虎机结算失败，本次未扣款。"
        if operation.won:
            return (
                "老虎机中奖\n"
                f"投入：{FapiPlugin._format_units(operation.debit_units)}\n"
                f"奖金：{FapiPlugin._format_units(operation.credit_units)}"
            )
        return (
            "老虎机未中奖\n"
            f"本次投入：{FapiPlugin._format_units(operation.debit_units)}"
        )

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

    @staticmethod
    async def _can_create_red_packet(
        event: AstrMessageEvent,
        platform: str,
        group_id: str,
        sender_id: str,
    ) -> bool:
        is_admin = getattr(event, "is_admin", None)
        try:
            if callable(is_admin) and is_admin():
                return True
        except Exception:
            pass

        if platform != "aiocqhttp":
            return False

        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        sender = raw.get("sender", {}) if isinstance(raw, dict) else {}
        role = str(sender.get("role", "")).strip().casefold()
        if role in {"owner", "admin"}:
            return True
        if role == "member":
            return False

        get_group = getattr(event, "get_group", None)
        if not callable(get_group):
            return False
        try:
            group = await get_group(group_id)
        except Exception:
            return False
        if group is None:
            return False
        owner_id = str(getattr(group, "group_owner", "") or "")
        admin_ids = {
            str(value)
            for value in (getattr(group, "group_admins", None) or [])
        }
        return sender_id == owner_id or sender_id in admin_ids

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
        if any(character.isspace() for character in value):
            return False
        local_part, separator, domain = value.rpartition("@")
        return bool(
            separator
            and local_part
            and "." in domain
            and not domain.startswith(".")
        )

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
    def _api_error_reply(exc: TokenRouterError) -> str:
        if exc.status_code in {401, 403}:
            return "中转站鉴权失败，请联系管理员检查插件配置。"
        if exc.status_code == 429:
            return "中转站查询太频繁，请稍后再试。"
        return "中转站用户查询失败，请稍后重试。"
