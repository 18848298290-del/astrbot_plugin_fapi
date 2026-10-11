from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .binding_store import BindStatus, BindingDatabaseError, MySQLBindingStore
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
from .tokenrouter_client import TokenRouterClient, TokenRouterError


_SUPPORTED_QQ_PLATFORMS = frozenset({"aiocqhttp", "qq_official"})


@dataclass(frozen=True, slots=True)
class GroupCommandContext:
    platform: str
    sender_id: str
    group_id: str


@register(
    "astrbot_plugin_fapi",
    "FAPI",
    "QQ 与 TokenRouter 账号互通、签到和群红包",
    "3.1.1",
)
class FapiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._allowed_group_id = str(config.get("allowed_group_id", "")).strip()
        self.client = TokenRouterClient(
            base_url=str(config.get("tokenrouter_url", "")),
            admin_api_key=str(config.get("admin_api_key", "")),
            timeout=float(config.get("request_timeout", 10)),
            verify_ssl=bool(config.get("verify_ssl", True)),
        )
        self.bindings = MySQLBindingStore(
            host=str(config.get("mysql_host", "")),
            port=int(config.get("mysql_port", 3306)),
            user=str(config.get("mysql_user", "")),
            password=str(config.get("mysql_password", "")),
            database=str(config.get("mysql_database", "")),
            table=str(config.get("mysql_table", "astrbot_fapi_bindings")),
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
            jobs_table=str(
                config.get("mysql_reward_jobs_table", "astrbot_fapi_reward_jobs")
            ),
            checkins_table=str(
                config.get("mysql_checkins_table", "astrbot_fapi_checkins")
            ),
            packets_table=str(
                config.get("mysql_red_packets_table", "astrbot_fapi_red_packets")
            ),
            claims_table=str(
                config.get("mysql_red_claims_table", "astrbot_fapi_red_claims")
            ),
            budgets_table=str(
                config.get("mysql_reward_budgets_table", "astrbot_fapi_reward_budgets")
            ),
        )
        self._reward_delivery_lock = asyncio.Lock()
        self._reward_worker_task: asyncio.Task | None = None
        self._reward_config_error = ""
        try:
            self._reward_timezone = ZoneInfo(
                str(config.get("reward_timezone", "Asia/Shanghai")).strip()
            )
            self._checkin_reward_units = money_to_units(
                config.get("checkin_reward", "0.05")
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
            if self._checkin_daily_budget_units < self._checkin_reward_units:
                raise ValueError("签到每日预算不能小于单次签到奖励")
            if not 1 <= self._red_packet_max_count <= 1000:
                raise ValueError("红包最大份数必须在 1 到 1000 之间")
            if not 1 <= self._red_packet_expire_minutes <= 1440:
                raise ValueError("红包有效期必须在 1 到 1440 分钟之间")
        except (ValueError, ZoneInfoNotFoundError) as exc:
            self._reward_config_error = str(exc)

    async def initialize(self) -> None:
        try:
            await self.bindings.initialize()
        except BindingDatabaseError as exc:
            logger.error("FAPI 远程 MySQL 初始化失败：%s", exc)
        try:
            await self.rewards.initialize()
        except RewardDatabaseError as exc:
            logger.error("FAPI 奖励数据表初始化失败：%s", exc)
        else:
            if not self._reward_config_error:
                self._reward_worker_task = asyncio.create_task(
                    self._reward_worker_loop(),
                    name="fapi-reward-worker",
                )

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
                amount_units=self._checkin_reward_units,
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
            yield event.plain_result("红包总金额不足，每份至少需要 $0.01。")
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
            "发送 /fapi 抢红包 领取"
        )

    @fapi.command("抢红包", alias={"claim"})
    async def claim_red_packet(
        self,
        event: AstrMessageEvent,
        packet_id: int = 0,
    ):
        """领取当前群的指定红包或最新红包。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert command_context is not None
        qq_id = command_context.sender_id
        group_id = command_context.group_id
        if packet_id < 0:
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
                packet_id=packet_id or None,
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
        packet_id: int = 0,
    ):
        """查看当前群红包状态。"""
        command_context, context_error = self._group_command_context(event)
        if context_error:
            yield event.plain_result(context_error)
            return
        assert command_context is not None
        group_id = command_context.group_id
        if packet_id < 0:
            yield event.plain_result("红包编号不正确。")
            return
        try:
            packet = await self.rewards.get_red_packet(
                group_id=group_id,
                packet_id=packet_id or None,
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

    async def terminate(self) -> None:
        if self._reward_worker_task is not None:
            self._reward_worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reward_worker_task
            self._reward_worker_task = None
        await self.rewards.close()
        await self.bindings.close()
        await self.client.close()

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
            digest = hashlib.sha256(
                f"{platform}\0{group_id}\0{message_id}".encode("utf-8")
            ).hexdigest()
            return f"fapi-red-packet:v1:{digest}"
        return None

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
        return f"${whole}.{fraction}"

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
