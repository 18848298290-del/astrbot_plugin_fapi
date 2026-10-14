import asyncio
import importlib.util
import logging
import sys
import types
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace


def load_plugin_module():
    root = Path(__file__).resolve().parents[1]
    package_name = "fapi_plugin_test_package"
    package = types.ModuleType(package_name)
    package.__path__ = [str(root)]

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_module = types.ModuleType("astrbot.api.event")
    star_module = types.ModuleType("astrbot.api.star")
    core_module = types.ModuleType("astrbot.core")
    core_module.__path__ = []
    core_star_module = types.ModuleType("astrbot.core.star")
    core_star_module.__path__ = []
    core_filter_module = types.ModuleType("astrbot.core.star.filter")
    core_filter_module.__path__ = []
    command_module = types.ModuleType("astrbot.core.star.filter.command")

    class AstrBotConfig(dict):
        pass

    class Filter:
        @staticmethod
        def command_group(_name):
            def decorate(function):
                function.command = lambda *_args, **_kwargs: lambda handler: handler
                return function

            return decorate

    class Star:
        def __init__(self, context):
            self.context = context

    class GreedyStr(str):
        pass

    def register(*_args, **_kwargs):
        return lambda plugin_class: plugin_class

    api.AstrBotConfig = AstrBotConfig
    api.logger = logging.getLogger("fapi-plugin-test")
    event_module.AstrMessageEvent = object
    event_module.filter = Filter()
    star_module.Context = object
    star_module.Star = Star
    star_module.register = register
    command_module.GreedyStr = GreedyStr

    modules = {
        package_name: package,
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_module,
        "astrbot.api.star": star_module,
        "astrbot.core": core_module,
        "astrbot.core.star": core_star_module,
        "astrbot.core.star.filter": core_filter_module,
        "astrbot.core.star.filter.command": command_module,
    }
    sys.modules.update(modules)

    module_name = f"{package_name}.main"
    spec = importlib.util.spec_from_file_location(module_name, root / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    loaded_names = [
        *modules,
        module_name,
        f"{package_name}.binding_store",
        f"{package_name}.game_store",
        f"{package_name}.tokenrouter_client",
        f"{package_name}.reward_store",
    ]
    return module, loaded_names


class FakeEvent:
    def __init__(
        self,
        sender_id="12345",
        platform_name="aiocqhttp",
        group_id="987654321",
        role="member",
        astrbot_admin=False,
        group=None,
        message_id="message-1",
        self_id="bot-qq",
        messages=None,
        raw_message=None,
    ):
        self.sender_id = sender_id
        self.platform_name = platform_name
        self.group_id = group_id
        self.astrbot_admin = astrbot_admin
        self.group = group
        self.self_id = self_id
        self.messages = list(messages or [])
        if raw_message is None:
            raw_message = {"sender": {"role": role}} if role is not None else {}
        self.message_obj = SimpleNamespace(
            raw_message=raw_message,
            message_id=message_id,
            message=self.messages,
            self_id=self_id,
        )

    def get_sender_id(self):
        return self.sender_id

    def get_platform_name(self):
        return self.platform_name

    def get_group_id(self):
        return self.group_id

    def get_self_id(self):
        return self.self_id

    def get_messages(self):
        return self.messages

    def is_admin(self):
        return self.astrbot_admin

    async def get_group(self, _group_id):
        return self.group

    @staticmethod
    def plain_result(text):
        return text

async def collect(async_generator):
    return [item async for item in async_generator]


def user(user_id, username, email):
    return SimpleNamespace(
        user_id=user_id,
        username=username,
        email=email,
        status="active",
    )


def game_operation(
    module,
    *,
    game_type="robbery",
    status=None,
    won=True,
    operation_key="game-key",
    actor_qq_id="12345",
    actor_user_id=11,
    target_qq_id="67890",
    target_user_id=22,
    debit_user_id=22,
    credit_user_id=11,
    debit_units=10_000_000,
    credit_units=10_000_000,
):
    return module.GameOperation(
        operation_id="game-op-1",
        operation_key=operation_key,
        game_type=game_type,
        group_id="987654321",
        actor_qq_id=actor_qq_id,
        actor_user_id=actor_user_id,
        target_qq_id=target_qq_id,
        target_user_id=target_user_id,
        debit_user_id=debit_user_id,
        credit_user_id=credit_user_id,
        debit_units=debit_units,
        credit_units=credit_units,
        won=won,
        status=status or module.GameStatus.SUCCEEDED,
        attempt_count=0,
        last_error=None,
        next_attempt_at=None,
        created_at=None,
    )


class InMemoryGameStore:
    def __init__(self, module, operation=None, cooldown_seconds=None):
        self.module = module
        self.operation = operation
        self.cooldown_seconds = cooldown_seconds
        self.reserve_calls = []
        self.transition_calls = []

    async def get_by_key(self, operation_key):
        if self.operation is None or self.operation.operation_key != operation_key:
            return None
        return self.operation

    async def reserve_game(self, **values):
        self.reserve_calls.append(values)
        if self.cooldown_seconds is not None:
            raise self.module.GameCooldownActive(self.cooldown_seconds)
        status = (
            self.module.GameStatus.PENDING_DEBIT
            if values["debit_units"] > 0
            else self.module.GameStatus.PENDING_CREDIT
            if values["credit_units"] > 0
            else self.module.GameStatus.SUCCEEDED
        )
        self.operation = self.module.GameOperation(
            operation_id="game-op-1",
            operation_key=values["operation_key"],
            game_type=values["game_type"],
            group_id=values["group_id"],
            actor_qq_id=values["actor_qq_id"],
            actor_user_id=values["actor_user_id"],
            target_qq_id=values.get("target_qq_id"),
            target_user_id=values.get("target_user_id"),
            debit_user_id=values["debit_user_id"],
            credit_user_id=values.get("credit_user_id"),
            debit_units=values["debit_units"],
            credit_units=values["credit_units"],
            won=values["won"],
            status=status,
            attempt_count=0,
            last_error=None,
            next_attempt_at=None,
            created_at=None,
        )
        return SimpleNamespace(operation=self.operation, existing=False)

    async def get(self, operation_id):
        if self.operation is None or self.operation.operation_id != operation_id:
            return None
        return self.operation

    async def claim(self, operation_id, **_values):
        operation = await self.get(operation_id)
        if operation is None:
            return None
        self.operation = replace(operation, lease_token="test-game-lease")
        return self.operation

    async def renew_claim(self, operation_id, claim_token, **_values):
        operation = await self.get(operation_id)
        if operation is None or operation.lease_token != claim_token:
            raise self.module.GameOperationConflict("stale test claim")
        return operation

    async def claim_pending(self, **_values):
        if self.operation is None:
            return []
        claimed = await self.claim(self.operation.operation_id)
        return [claimed] if claimed is not None else []

    async def mark_debited(self, operation_id, **_values):
        self.transition_calls.append(("debited", operation_id))
        status = (
            self.module.GameStatus.PENDING_CREDIT
            if self.operation.credit_units > 0
            else self.module.GameStatus.SUCCEEDED
        )
        self.operation = replace(self.operation, status=status)

    async def mark_succeeded(self, operation_id, **_values):
        self.transition_calls.append(("succeeded", operation_id))
        self.operation = replace(
            self.operation,
            status=self.module.GameStatus.SUCCEEDED,
        )

    async def mark_insufficient(self, operation_id, **_values):
        self.transition_calls.append(("insufficient", operation_id))
        self.operation = replace(
            self.operation,
            status=self.module.GameStatus.INSUFFICIENT,
        )

    async def mark_failed(self, operation_id, **_values):
        self.transition_calls.append(("failed", operation_id))
        self.operation = replace(
            self.operation,
            status=self.module.GameStatus.FAILED,
        )

    async def mark_pending_refund(self, operation_id, **_values):
        self.transition_calls.append(("pending_refund", operation_id))
        self.operation = replace(
            self.operation,
            status=self.module.GameStatus.PENDING_REFUND,
        )

    async def mark_refunded(self, operation_id, **_values):
        self.transition_calls.append(("refunded", operation_id))
        self.operation = replace(
            self.operation,
            status=self.module.GameStatus.REFUNDED,
        )

    async def record_error(self, operation_id, error, **values):
        self.transition_calls.append(("error", operation_id, error, values))

    async def record_refund_error(self, operation_id, error, **values):
        self.transition_calls.append(("refund_error", operation_id, error, values))
        return self.operation.status


class GameBindings:
    def __init__(self, values):
        self.values = values
        self.calls = []

    async def get_by_qq(self, qq_id):
        self.calls.append(qq_id)
        user_id = self.values.get(qq_id)
        if user_id is None:
            return None
        return SimpleNamespace(tokenrouter_user_id=user_id)


class BalanceClient:
    def __init__(self, module, fail_on=None):
        self.module = module
        self.fail_on = fail_on
        self.calls = []

    async def adjust_balance(self, **values):
        self.calls.append(values)
        if self.fail_on == len(self.calls):
            raise self.module.TokenRouterError(
                "insufficient balance",
                status_code=400,
                api_code="INSUFFICIENT_BALANCE",
            )


class PluginBindingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module, self.loaded_names = load_plugin_module()
        self.plugin = self.module.FapiPlugin(
            object(),
            {"allowed_group_id": "987654321"},
        )
        await self.plugin.client.close()

    async def asyncTearDown(self):
        for name in self.loaded_names:
            sys.modules.pop(name, None)

    async def test_private_admin_menu_is_available_without_group_context(self):
        replies = await collect(
            self.plugin.admin_private(
                FakeEvent(group_id=None, astrbot_admin=True),
                "",
            )
        )

        self.assertEqual(len(replies), 1)
        self.assertIn("FAPI 管理员私聊菜单", replies[0])
        self.assertIn("/fapi 管理 概览", replies[0])
        self.assertIn("/fapi 管理 RPM [邮箱]", replies[0])
        self.assertIn("/fapi 管理 排行 [数量]", replies[0])
        self.assertIn("/fapi 管理 查询 <邮箱>", replies[0])
        self.assertIn("USD 1,000.00", replies[0])

    async def test_private_admin_rejects_regular_user_before_external_access(self):
        class ForbiddenClient:
            def __getattr__(_self, name):
                async def forbidden(*_args, **_kwargs):
                    raise AssertionError(f"管理员门禁前不应调用 {name}")

                return forbidden

        self.plugin.client = ForbiddenClient()
        replies = await collect(
            self.plugin.admin_private(
                FakeEvent(group_id=None, astrbot_admin=False),
                "查询 alice",
            )
        )

        self.assertEqual(replies, ["该指令仅限 AstrBot 管理员私聊使用。"])

    async def test_private_admin_rejects_group_even_for_astrbot_admin(self):
        replies = await collect(
            self.plugin.admin_private(
                FakeEvent(group_id="987654321", astrbot_admin=True),
                "",
            )
        )

        self.assertEqual(replies, ["管理员管理指令只能在私聊中使用。"])

    async def test_private_admin_fails_closed_when_group_lookup_raises(self):
        class ForbiddenClient:
            def __getattr__(_self, name):
                async def forbidden(*_args, **_kwargs):
                    raise AssertionError(f"会话门禁失败后不应调用 {name}")

                return forbidden

        event = FakeEvent(group_id="987654321", astrbot_admin=True)

        def broken_group_lookup():
            raise RuntimeError("adapter failure")

        event.get_group_id = broken_group_lookup
        self.plugin.client = ForbiddenClient()
        replies = await collect(self.plugin.admin_private(event, "查询 alice"))

        self.assertEqual(replies, ["无法确认当前会话类型，请稍后重试。"])

    async def test_private_admin_accepts_async_admin_check(self):
        event = FakeEvent(group_id=None)

        async def is_admin():
            return True

        event.is_admin = is_admin
        replies = await collect(self.plugin.admin_private(event, ""))

        self.assertIn("FAPI 管理员私聊菜单", replies[0])

    async def test_private_admin_queries_exact_user(self):
        class Client:
            async def find_users_by_exact_email(_self, email):
                self.assertEqual(email, "alice@example.com")
                return [user(7, "alice", "alice@example.com")]

            async def get_balance_summary(_self, user_id):
                self.assertEqual(user_id, 7)
                return SimpleNamespace(
                    balance=Decimal("12.5"),
                    used_last_30_days=Decimal("3.25"),
                    concurrency=8,
                )

        self.plugin.client = Client()
        replies = await collect(
            self.plugin.admin_private(
                FakeEvent(group_id=None, astrbot_admin=True),
                "查询 alice@example.com",
            )
        )

        self.assertEqual(len(replies), 1)
        self.assertIn("ID：7", replies[0])
        self.assertIn("邮箱：alice@example.com", replies[0])
        self.assertIn("余额：USD 12.50", replies[0])
        self.assertIn("已使用（近30天）：USD 3.25", replies[0])
        self.assertIn("并发上限：8", replies[0])

    async def test_private_admin_trims_query_email(self):
        class Client:
            async def find_users_by_exact_email(_self, email):
                self.assertEqual(email, "alice@example.com")
                return [user(7, "Alice Smith", "alice@example.com")]

            async def get_balance_summary(_self, user_id):
                self.assertEqual(user_id, 7)
                return SimpleNamespace(
                    balance=Decimal("12.5"),
                    used_last_30_days=Decimal("3.25"),
                    concurrency=8,
                )

        self.plugin.client = Client()
        replies = await collect(
            self.plugin.admin_private(
                FakeEvent(group_id=None, astrbot_admin=True),
                "查询   alice@example.com   ",
            )
        )

        self.assertIn("用户名：Alice Smith", replies[0])

    async def test_private_admin_queries_overview_and_global_rpm(self):
        class Client:
            async def get_dashboard_overview(_self):
                return SimpleNamespace(
                    total_users=50,
                    today_new_users=2,
                    active_users=9,
                    hourly_active_users=3,
                    total_api_keys=20,
                    active_api_keys=18,
                    today_requests=1234,
                    today_tokens=567890,
                    today_actual_cost=Decimal("12.3456"),
                    rpm=8,
                    tpm=900,
                    average_duration_ms=321.5,
                    resource_kind="提供商",
                    total_resources=6,
                    normal_resources=4,
                    error_resources=1,
                    rate_limit_resources=1,
                    overload_resources=0,
                    stats_stale=False,
                )

        self.plugin.client = Client()
        event = FakeEvent(group_id=None, astrbot_admin=True)

        overview = await collect(self.plugin.admin_private(event, "概览"))
        rpm = await collect(self.plugin.admin_private(event, "RPM"))

        self.assertIn("今日全站概览", overview[0])
        self.assertIn("请求：1,234", overview[0])
        self.assertIn("实际消费：USD 12.3456", overview[0])
        self.assertIn("RPM 8 / TPM 900", overview[0])
        self.assertIn("全站实时吞吐（近5分钟均值）", rpm[0])
        self.assertIn("RPM：8", rpm[0])

    async def test_private_admin_queries_user_rpm_and_limits_group_output(self):
        class Client:
            async def find_users_by_exact_email(_self, email):
                self.assertEqual(email, "alice@example.com")
                return [user(7, "alice", "alice@example.com")]

            async def get_user_rpm_status(_self, user_id):
                self.assertEqual(user_id, 7)
                groups = tuple(
                    SimpleNamespace(
                        group_id=index,
                        group_name=f"group-{index}",
                        used=index,
                        limit=0 if index == 1 else 20,
                        source="override" if index == 1 else "group",
                    )
                    for index in range(1, 11)
                )
                return SimpleNamespace(used=4, limit=0, per_group=groups)

        self.plugin.client = Client()
        replies = await collect(
            self.plugin.admin_private(
                FakeEvent(group_id=None, astrbot_admin=True),
                "RPM alice@example.com",
            )
        )

        self.assertIn("用户 RPM 状态（当前分钟）", replies[0])
        self.assertIn("邮箱：alice@example.com（ID 7）", replies[0])
        self.assertIn("用户名：alice", replies[0])
        self.assertIn("总 RPM：4 / 不限", replies[0])
        self.assertIn("group-1：1 / 不限 (用户覆盖)", replies[0])
        self.assertIn("另有 2 个分组未显示", replies[0])
        self.assertNotIn("group-9", replies[0])

    async def test_private_admin_queries_today_ranking_without_email(self):
        class Client:
            def __init__(_self):
                _self.values = None

            async def get_user_spending_ranking(_self, **values):
                _self.values = values
                return SimpleNamespace(
                    start_date=values["ranking_date"],
                    end_date=values["ranking_date"],
                    total_actual_cost=Decimal("5.5"),
                    total_requests=20,
                    total_tokens=3000,
                    items=(
                        SimpleNamespace(
                            user_id=7,
                            username="alice\u2028spoofed\u202e<br/>[fake](x)$",
                            actual_cost=Decimal("3.25"),
                            requests=12,
                            tokens=2000,
                        ),
                    ),
                )

        client = Client()
        self.plugin.client = client
        replies = await collect(
            self.plugin.admin_private(
                FakeEvent(group_id=None, astrbot_admin=True),
                "排行 5",
            )
        )

        self.assertEqual(client.values["limit"], 5)
        self.assertEqual(client.values["timezone_name"], "Asia/Shanghai")
        self.assertIn("用户消费排行", replies[0])
        self.assertIn("alice spoofed", replies[0])
        self.assertEqual(len(replies[0].splitlines()), 3)
        self.assertNotIn("\u2028", replies[0])
        self.assertNotIn("\u202e", replies[0])
        self.assertNotIn("<br/>", replies[0])
        self.assertNotIn("[fake](x)", replies[0])
        self.assertNotIn("@example.com", replies[0])

    async def test_private_admin_ranking_rejects_invalid_timezone(self):
        plugin = self.module.FapiPlugin(
            object(),
            {
                "allowed_group_id": "987654321",
                "reward_timezone": "Not/A_Zone",
            },
        )
        await plugin.client.close()

        class ForbiddenClient:
            async def get_user_spending_ranking(_self, **_values):
                raise AssertionError("时区无效时不应发起排行请求")

        plugin.client = ForbiddenClient()
        replies = await collect(
            plugin.admin_private(
                FakeEvent(group_id=None, astrbot_admin=True),
                "排行",
            )
        )

        self.assertEqual(
            replies,
            ["排行时区配置不正确，请检查 reward_timezone。"],
        )

    async def test_ranking_ignores_unrelated_reward_config_error(self):
        plugin = self.module.FapiPlugin(
            object(),
            {
                "allowed_group_id": "987654321",
                "reward_timezone": "Asia/Shanghai",
                "checkin_reward_min": "invalid",
            },
        )
        await plugin.client.close()

        class Client:
            async def get_user_spending_ranking(_self, **values):
                self.assertEqual(values["timezone_name"], "Asia/Shanghai")
                return SimpleNamespace(
                    start_date=values["ranking_date"],
                    end_date=values["ranking_date"],
                    total_actual_cost=Decimal("0"),
                    total_requests=0,
                    total_tokens=0,
                    items=(),
                )

        plugin.client = Client()
        replies = await collect(
            plugin.admin_private(
                FakeEvent(group_id=None, astrbot_admin=True),
                "排行",
            )
        )

        self.assertIn("用户消费排行", replies[0])

    async def test_private_admin_rejects_invalid_ranking_limit_before_api(self):
        class ForbiddenClient:
            async def get_user_spending_ranking(_self, **_values):
                raise AssertionError("非法排行数量不应发起请求")

        self.plugin.client = ForbiddenClient()
        replies = await collect(
            self.plugin.admin_private(
                FakeEvent(group_id=None, astrbot_admin=True),
                "排行 21",
            )
        )

        self.assertEqual(replies, ["排行数量必须是 1 到 20 的整数。"])

    async def test_private_admin_refuses_duplicate_exact_email(self):
        class Client:
            async def find_users_by_exact_email(_self, _email):
                return [
                    user(7, "alice", "same@example.com"),
                    user(8, "alice", "same@example.com"),
                ]

            async def adjust_balance(_self, **_values):
                raise AssertionError("重名用户不应调账")

        self.plugin.client = Client()
        replies = await collect(
            self.plugin.admin_private(
                FakeEvent(group_id=None, astrbot_admin=True),
                "加余额 same@example.com 1.25",
            )
        )

        self.assertEqual(
            replies,
            ["存在多个相同邮箱用户，为避免误操作，本次管理操作已取消。"],
        )

    async def test_private_admin_adds_balance_with_stable_idempotency_key(self):
        class Client:
            def __init__(_self):
                _self.adjustments = []

            async def find_users_by_exact_email(_self, email):
                self.assertEqual(email, "alice@example.com")
                return [user(7, "alice", "alice@example.com")]

            async def adjust_balance(_self, **values):
                _self.adjustments.append(values)
                return SimpleNamespace(balance=Decimal("13.75"))

        client = Client()
        self.plugin.client = client
        event = FakeEvent(
            group_id=None,
            astrbot_admin=True,
            message_id="admin-message-1",
        )

        first = await collect(
            self.plugin.admin_private(event, "加余额 alice@example.com 1.25")
        )
        second = await collect(
            self.plugin.admin_private(event, "加余额 alice@example.com 1.25")
        )

        self.assertIn("余额调整成功", first[0])
        self.assertIn("操作：增加", first[0])
        self.assertIn("调整后余额：USD 13.75", first[0])
        self.assertEqual(first, second)
        self.assertEqual(len(client.adjustments), 2)
        self.assertEqual(client.adjustments[0]["amount"], Decimal("1.25"))
        self.assertEqual(client.adjustments[0]["operation"], "add")
        self.assertEqual(
            client.adjustments[0]["idempotency_key"],
            client.adjustments[1]["idempotency_key"],
        )
        self.assertTrue(
            client.adjustments[0]["idempotency_key"].startswith(
                "fapi-admin-balance:v1:"
            )
        )

    async def test_private_admin_adjusts_balance_by_email(self):
        class Client:
            def __init__(_self):
                _self.adjustments = []

            async def find_users_by_exact_email(_self, email):
                self.assertEqual(email, "alice@example.com")
                return [user(7, "Alice Smith", "alice@example.com")]

            async def adjust_balance(_self, **values):
                _self.adjustments.append(values)
                return SimpleNamespace(balance=Decimal("13.75"))

        client = Client()
        self.plugin.client = client
        replies = await collect(
            self.plugin.admin_private(
                FakeEvent(
                    group_id=None,
                    astrbot_admin=True,
                    message_id="admin-email",
                ),
                "加余额 alice@example.com 1.25",
            )
        )

        self.assertIn("余额调整成功", replies[0])
        self.assertEqual(client.adjustments[0]["amount"], Decimal("1.25"))

    async def test_private_admin_rejects_malformed_trailing_arguments(self):
        class ForbiddenClient:
            def __getattr__(_self, name):
                async def forbidden(*_args, **_kwargs):
                    raise AssertionError(f"非法参数不应调用 {name}")

                return forbidden

        self.plugin.client = ForbiddenClient()
        event = FakeEvent(group_id=None, astrbot_admin=True)

        adjustment = await collect(
            self.plugin.admin_private(event, "加余额 alice@example.com 1.25 trailing")
        )
        overview = await collect(
            self.plugin.admin_private(event, "概览 trailing")
        )
        ranking = await collect(
            self.plugin.admin_private(event, "排行 5 trailing")
        )

        self.assertIn("金额不正确", adjustment[0])
        self.assertEqual(overview, ["用法：/fapi 管理 概览"])
        self.assertEqual(ranking, ["排行数量必须是 1 到 20 的整数。"])

    def test_safe_reply_field_neutralizes_structural_markup(self):
        unsafe = "alice\u2028fake\u2029line\u202e<br/>[x](url)$value$"
        safe = self.plugin._safe_reply_field(unsafe)

        self.assertEqual(len(safe.splitlines()), 1)
        for forbidden in ("\u2028", "\u2029", "\u202e", "<", ">", "[", "]", "$"):
            self.assertNotIn(forbidden, safe)

    async def test_private_admin_validates_write_before_user_lookup(self):
        class ForbiddenClient:
            async def find_users_by_exact_email(_self, _email):
                raise AssertionError("非法金额不应查询用户")

        self.plugin.client = ForbiddenClient()
        event = FakeEvent(group_id=None, astrbot_admin=True)

        too_large = await collect(
            self.plugin.admin_private(event, "加余额 alice@example.com 1000.00000001")
        )
        missing_message_id = await collect(
            self.plugin.admin_private(
                FakeEvent(
                    group_id=None,
                    astrbot_admin=True,
                    message_id=None,
                    raw_message={},
                ),
                "减余额 alice@example.com 1.00",
            )
        )

        self.assertIn("单次余额调整不能超过", too_large[0])
        self.assertIn("无法确认当前消息编号", missing_message_id[0])

    async def test_private_admin_reports_insufficient_and_unknown_outcomes(self):
        class Client:
            def __init__(_self):
                _self.unknown = False

            async def find_users_by_exact_email(_self, _email):
                return [user(7, "alice", "alice@example.com")]

            async def adjust_balance(_self, **_values):
                if _self.unknown:
                    raise self.module.TokenRouterError(
                        "timeout",
                        outcome_unknown=True,
                    )
                raise self.module.TokenRouterError(
                    "insufficient balance",
                    status_code=400,
                    api_code="INSUFFICIENT_BALANCE",
                )

        client = Client()
        self.plugin.client = client
        event = FakeEvent(group_id=None, astrbot_admin=True)

        insufficient = await collect(
            self.plugin.admin_private(event, "减余额 alice@example.com 1.00")
        )
        client.unknown = True
        unknown = await collect(
            self.plugin.admin_private(event, "加余额 alice@example.com 1.00")
        )

        self.assertEqual(insufficient, ["用户余额不足，本次未扣减。"])
        self.assertIn("不要重复发送调账命令", unknown[0])

    async def test_constructor_rejects_duplicate_table_names_case_insensitively(self):
        with self.assertRaisesRegex(ValueError, "MySQL 数据表名称必须互不相同"):
            self.module.FapiPlugin(
                object(),
                {
                    "mysql_table": "Shared_Table",
                    "mysql_game_operations_table": " shared_table ",
                },
            )

    async def test_constructor_includes_game_cooldown_table_in_uniqueness(self):
        with self.assertRaisesRegex(ValueError, "mysql_game_cooldowns_table"):
            self.module.FapiPlugin(
                object(),
                {
                    "mysql_table": "astrbot_fapi_game_cooldowns",
                },
            )

    async def test_constructor_includes_daily_broadcast_table_in_uniqueness(self):
        with self.assertRaisesRegex(ValueError, "mysql_daily_broadcasts_table"):
            self.module.FapiPlugin(
                object(),
                {
                    "mysql_table": "astrbot_fapi_daily_broadcasts",
                },
            )

    async def test_game_lease_covers_all_http_timeout_phases(self):
        plugin = self.module.FapiPlugin(
            object(),
            {"request_timeout": 60},
        )
        await plugin.client.close()

        self.assertEqual(plugin._game_claim_lease_seconds, 300)

    async def test_initialize_starts_supervisors_after_database_failure(self):
        started = []
        closed = []
        wait_forever = self.module.asyncio.Event()

        class FailingStore:
            def __init__(store_self, error_type, name):
                store_self.error_type = error_type
                store_self.name = name

            async def initialize(store_self):
                raise store_self.error_type("database unavailable")

            async def close(store_self):
                closed.append(store_self.name)

        class Client:
            async def close(_self):
                closed.append("client")

        async def reward_worker():
            started.append("reward")
            await wait_forever.wait()

        async def game_worker():
            started.append("game")
            await wait_forever.wait()

        self.plugin.bindings = FailingStore(
            self.module.BindingDatabaseError,
            "bindings",
        )
        self.plugin.rewards = FailingStore(
            self.module.RewardDatabaseError,
            "rewards",
        )
        self.plugin.games = FailingStore(
            self.module.GameDatabaseError,
            "games",
        )
        self.plugin.client = Client()
        self.plugin._game_config_error = "invalid new-game settings"
        self.plugin._reward_worker_loop = reward_worker
        self.plugin._game_worker_loop = game_worker

        await self.plugin.initialize()
        await self.module.asyncio.sleep(0)

        self.assertCountEqual(started, ["reward", "game"])
        self.assertIsNotNone(self.plugin._reward_worker_task)
        self.assertIsNotNone(self.plugin._game_worker_task)

        await self.plugin.terminate()
        self.assertCountEqual(closed, ["games", "rewards", "bindings", "client"])

    async def test_terminate_closes_remaining_resources_after_one_close_fails(self):
        closed = []

        class Resource:
            def __init__(resource_self, name, fail=False):
                resource_self.name = name
                resource_self.fail = fail

            async def close(resource_self):
                closed.append(resource_self.name)
                if resource_self.fail:
                    raise RuntimeError("close failed")

        self.plugin.games = Resource("games", fail=True)
        self.plugin.rewards = Resource("rewards")
        self.plugin.bindings = Resource("bindings")
        self.plugin.client = Resource("client")

        await self.plugin.terminate()

        self.assertEqual(closed, ["games", "rewards", "bindings", "client"])

    async def test_onebot_help_menu_lists_commands_and_permissions(self):
        replies = await collect(self.plugin.help_menu(FakeEvent()))

        self.assertEqual(len(replies), 1)
        self.assertIn("FAPI 功能菜单", replies[0])
        self.assertIn("/fapi 绑定\n", replies[0])
        self.assertIn("/fapi 绑定 <完整邮箱>", replies[0])
        self.assertNotIn("/fapi 解绑", replies[0])
        self.assertIn("/fapi 余额", replies[0])
        self.assertIn("/fapi 签到", replies[0])
        self.assertIn("/fapi 抢红包 [红包编号]", replies[0])
        self.assertIn("/fapi 打劫 @用户", replies[0])
        self.assertNotIn("老虎机", replies[0])
        self.assertFalse(hasattr(self.plugin, "slot_machine"))
        self.assertIn("群主、群管理员或 AstrBot 管理员", replies[0])

    async def test_official_help_menu_requires_email_and_astrbot_admin(self):
        self.plugin._allowed_group_id = "official-group-openid"
        replies = await collect(
            self.plugin.help_menu(
                FakeEvent(
                    sender_id="official-member-openid",
                    platform_name="qq_official",
                    group_id="official-group-openid",
                )
            )
        )

        self.assertEqual(len(replies), 1)
        self.assertIn("/fapi 绑定 <完整邮箱>", replies[0])
        self.assertNotIn("/fapi 绑定\n", replies[0])
        self.assertIn("红包创建：仅 AstrBot 管理员", replies[0])

    async def test_help_menu_is_rejected_outside_configured_group(self):
        replies = await collect(
            self.plugin.help_menu(FakeEvent(group_id="another-group"))
        )

        self.assertEqual(
            replies,
            ["该指令只能在管理员配置的指定群中使用。"],
        )

    async def test_rejects_private_binding_before_api_or_database_access(self):
        class Client:
            async def find_users_by_exact_email(_self, _email):
                raise AssertionError("私聊绑定不应查询中转站")

        class Store:
            async def bind(_self, **_values):
                raise AssertionError("私聊绑定不应写入数据库")

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(
            self.plugin.bind(FakeEvent(group_id=None), "alice@example.com")
        )

        self.assertEqual(replies, ["该指令只能在管理员配置的指定群中使用。"])

    async def test_unconfigured_group_rejects_every_command_before_external_access(self):
        class ForbiddenDependency:
            def __getattr__(_self, name):
                async def forbidden(*_args, **_kwargs):
                    raise AssertionError(f"群门禁前不应调用 {name}")

                return forbidden

        self.plugin._allowed_group_id = ""
        self.plugin.client = ForbiddenDependency()
        self.plugin.bindings = ForbiddenDependency()
        self.plugin.rewards = ForbiddenDependency()
        self.plugin.games = ForbiddenDependency()
        event = FakeEvent(role="admin", astrbot_admin=True)
        commands = (
            self.plugin.help_menu(event),
            self.plugin.bind(event, "alice@example.com"),
            self.plugin.binding_status(event),
            self.plugin.balance(event),
            self.plugin.checkin(event),
            self.plugin.create_red_packet(event, "1.00", 1),
            self.plugin.claim_red_packet(event),
            self.plugin.red_packet_status(event),
            self.plugin.robbery(event),
        )

        for command in commands:
            self.assertEqual(
                await collect(command),
                ["插件尚未配置指定群，请联系管理员。"],
            )

    async def test_other_group_rejects_every_command_before_external_access(self):
        class ForbiddenDependency:
            def __getattr__(_self, name):
                async def forbidden(*_args, **_kwargs):
                    raise AssertionError(f"跨群门禁前不应调用 {name}")

                return forbidden

        self.plugin.client = ForbiddenDependency()
        self.plugin.bindings = ForbiddenDependency()
        self.plugin.rewards = ForbiddenDependency()
        self.plugin.games = ForbiddenDependency()
        event = FakeEvent(
            group_id="another-group-openid",
            role="admin",
            astrbot_admin=True,
        )
        commands = (
            self.plugin.help_menu(event),
            self.plugin.bind(event, "alice@example.com"),
            self.plugin.binding_status(event),
            self.plugin.balance(event),
            self.plugin.checkin(event),
            self.plugin.create_red_packet(event, "1.00", 1),
            self.plugin.claim_red_packet(event),
            self.plugin.red_packet_status(event),
            self.plugin.robbery(event),
        )

        for command in commands:
            self.assertEqual(
                await collect(command),
                ["该指令只能在管理员配置的指定群中使用。"],
            )

    async def test_official_bot_requires_full_email_before_user_lookup(self):
        class Client:
            async def find_users_by_exact_email(_self, _email):
                raise AssertionError("缺少邮箱时不应查询中转站")

        self.plugin._allowed_group_id = "official-group-openid"
        self.plugin.client = Client()
        replies = await collect(
            self.plugin.bind(
                FakeEvent(
                    sender_id="official-member-openid",
                    platform_name="qq_official",
                    group_id="official-group-openid",
                ),
            )
        )

        self.assertEqual(
            replies,
            [
                "QQ 官方机器人无法取得真实 QQ 号，请使用："
                "/fapi 绑定 <完整邮箱>"
            ],
        )

    async def test_official_bot_binds_openid_with_exact_email(self):
        calls = []

        class Client:
            async def find_users_by_exact_email(_self, email):
                calls.append(("lookup", email))
                return [user(7, "alice", "alice@example.com")]

        class Store:
            async def bind(_self, **values):
                calls.append(("bind", values))
                return SimpleNamespace(status=self.module.BindStatus.CREATED)

        self.plugin._allowed_group_id = "official-group-openid"
        self.plugin.client = Client()
        self.plugin.bindings = Store()
        replies = await collect(
            self.plugin.bind(
                FakeEvent(
                    sender_id="official-member-openid",
                    platform_name="qq_official",
                    group_id="official-group-openid",
                ),
                "alice@example.com",
            )
        )

        self.assertEqual(replies, ["绑定成功。"])
        self.assertEqual(calls[0], ("lookup", "alice@example.com"))
        self.assertEqual(
            calls[1],
            (
                "bind",
                {
                    "qq_id": "official-member-openid",
                    "tokenrouter_user_id": 7,
                    "email": "alice@example.com",
                },
            ),
        )

    async def test_official_bot_explains_legacy_binding_conflict(self):
        class Client:
            async def find_users_by_exact_email(_self, _email):
                return [user(7, "alice", "alice@example.com")]

        class Store:
            async def bind(_self, **_values):
                return SimpleNamespace(status=self.module.BindStatus.USER_CONFLICT)

        self.plugin._allowed_group_id = "official-group-openid"
        self.plugin.client = Client()
        self.plugin.bindings = Store()
        replies = await collect(
            self.plugin.bind(
                FakeEvent(
                    sender_id="official-member-openid",
                    platform_name="qq_official",
                    group_id="official-group-openid",
                ),
                "alice@example.com",
            )
        )

        self.assertEqual(len(replies), 1)
        self.assertIn("个人机器人留下的旧 QQ 绑定", replies[0])
        self.assertIn("联系管理员迁移", replies[0])

    async def test_official_openids_are_accepted_in_configured_group(self):
        self.plugin._allowed_group_id = "official-group-openid"

        command_context, error = self.plugin._group_command_context(
            FakeEvent(
                sender_id="official-member-openid",
                platform_name="qq_official",
                group_id="official-group-openid",
            )
        )

        self.assertIsNone(error)
        self.assertEqual(command_context.platform, "qq_official")
        self.assertEqual(command_context.sender_id, "official-member-openid")
        self.assertEqual(command_context.group_id, "official-group-openid")

    async def test_unsupported_platform_is_rejected(self):
        command_context, error = self.plugin._group_command_context(
            FakeEvent(platform_name="telegram")
        )

        self.assertIsNone(command_context)
        self.assertEqual(error, "该指令仅支持 QQ 机器人平台。")

    async def test_onebot_mention_uses_structured_at_component(self):
        event = FakeEvent(
            messages=[SimpleNamespace(type="At", qq="67890")],
        )
        command_context, error = self.plugin._group_command_context(event)

        self.assertIsNone(error)
        self.assertEqual(
            self.plugin._mentioned_target_id(event, command_context),
            "67890",
        )

    async def test_onebot_mention_rejects_plain_text_and_multiple_targets(self):
        command_context, error = self.plugin._group_command_context(FakeEvent())
        self.assertIsNone(error)

        plain_text = FakeEvent(
            messages=[SimpleNamespace(type="Plain", text="@67890")],
        )
        multiple_targets = FakeEvent(
            messages=[
                SimpleNamespace(type="At", qq="67890"),
                SimpleNamespace(type="At", qq="54321"),
            ],
        )

        self.assertIsNone(
            self.plugin._mentioned_target_id(plain_text, command_context)
        )
        self.assertIsNone(
            self.plugin._mentioned_target_id(multiple_targets, command_context)
        )

    async def test_official_mention_prefers_member_openid(self):
        self.plugin._allowed_group_id = "official-group-openid"
        event = FakeEvent(
            sender_id="actor-member-openid",
            platform_name="qq_official",
            group_id="official-group-openid",
            raw_message={
                "mentions": [
                    {
                        "id": "target-member-openid",
                        "member_openid": "target-member-openid",
                        "is_you": False,
                    }
                ]
            },
        )
        command_context, error = self.plugin._group_command_context(event)

        self.assertIsNone(error)
        self.assertEqual(
            self.plugin._mentioned_target_id(event, command_context),
            "target-member-openid",
        )

    async def test_official_mention_rejects_conflicting_openids(self):
        self.plugin._allowed_group_id = "official-group-openid"
        event = FakeEvent(
            sender_id="actor-member-openid",
            platform_name="qq_official",
            group_id="official-group-openid",
            raw_message={
                "mentions": [
                    {
                        "id": "different-openid",
                        "member_openid": "target-member-openid",
                        "is_you": False,
                    }
                ]
            },
        )
        command_context, error = self.plugin._group_command_context(event)

        self.assertIsNone(error)
        self.assertIsNone(
            self.plugin._mentioned_target_id(event, command_context)
        )

    async def test_official_mention_id_fallback_requires_explicit_non_bot(self):
        self.plugin._allowed_group_id = "official-group-openid"
        base_values = {
            "sender_id": "actor-member-openid",
            "platform_name": "qq_official",
            "group_id": "official-group-openid",
        }
        explicit_target = FakeEvent(
            **base_values,
            raw_message={
                "mentions": [
                    {"id": "target-member-openid", "is_you": False},
                ]
            },
        )
        ambiguous_target = FakeEvent(
            **base_values,
            raw_message={"mentions": [{"id": "ambiguous-openid"}]},
        )
        bot_and_target = FakeEvent(
            **base_values,
            raw_message={
                "mentions": [
                    {"id": "bot-or-unknown-openid"},
                    {"id": "target-member-openid", "is_you": False},
                ]
            },
        )
        command_context, error = self.plugin._group_command_context(explicit_target)

        self.assertIsNone(error)
        self.assertEqual(
            self.plugin._mentioned_target_id(explicit_target, command_context),
            "target-member-openid",
        )
        self.assertIsNone(
            self.plugin._mentioned_target_id(ambiguous_target, command_context)
        )
        self.assertEqual(
            self.plugin._mentioned_target_id(bot_and_target, command_context),
            "target-member-openid",
        )

    async def test_official_mention_ignores_bot_and_rejects_multiple_targets(self):
        self.plugin._allowed_group_id = "official-group-openid"
        base_values = {
            "sender_id": "actor-member-openid",
            "platform_name": "qq_official",
            "group_id": "official-group-openid",
        }
        bot_only = FakeEvent(
            **base_values,
            raw_message={
                "mentions": [
                    {"member_openid": "bot-openid", "is_you": True},
                ]
            },
        )
        multiple_targets = FakeEvent(
            **base_values,
            raw_message={
                "mentions": [
                    {"member_openid": "target-one", "is_you": False},
                    {"member_openid": "target-two", "is_you": False},
                ]
            },
        )
        command_context, error = self.plugin._group_command_context(bot_only)

        self.assertIsNone(error)
        self.assertIsNone(
            self.plugin._mentioned_target_id(bot_only, command_context)
        )
        self.assertIsNone(
            self.plugin._mentioned_target_id(multiple_targets, command_context)
        )

    async def test_transfer_moves_balance_between_bound_users(self):
        event = FakeEvent(messages=[SimpleNamespace(type="At", qq="67890")])
        games = InMemoryGameStore(self.module)
        client = BalanceClient(self.module)
        self.plugin.bindings = GameBindings({"12345": 11, "67890": 22})
        self.plugin.games = games
        self.plugin.client = client

        replies = await collect(self.plugin.transfer(event, "@小羊 2.50"))

        self.assertEqual(len(replies), 1)
        self.assertIn("转账成功", replies[0])
        self.assertEqual(games.reserve_calls[0]["game_type"], "transfer")
        self.assertEqual(games.reserve_calls[0]["debit_units"], 250_000_000)
        self.assertEqual(
            [(call["user_id"], call["operation"]) for call in client.calls],
            [(11, "subtract"), (22, "add")],
        )

    async def test_transfer_rejects_invalid_target_before_balance_access(self):
        event = FakeEvent(messages=[SimpleNamespace(type="At", qq="12345")])
        games = InMemoryGameStore(self.module)
        self.plugin.games = games

        replies = await collect(self.plugin.transfer(event, "1"))

        self.assertEqual(replies, ["请只 @ 一名其他群成员：/fapi 转账 @用户 <金额>"])
        self.assertEqual(games.reserve_calls, [])

    async def test_robbery_success_transfers_between_bound_users_once(self):
        event = FakeEvent(
            messages=[SimpleNamespace(type="At", qq="67890")],
            message_id="robbery-message-1",
        )
        bindings = GameBindings({"12345": 11, "67890": 22})
        games = InMemoryGameStore(self.module)
        client = BalanceClient(self.module)
        self.plugin.bindings = bindings
        self.plugin.games = games
        self.plugin.client = client
        rates = []

        def successful_roll(rate):
            rates.append(rate)
            return True

        self.module.roll_success = successful_roll

        first_reply = await collect(self.plugin.robbery(event))
        second_reply = await collect(self.plugin.robbery(event))

        self.assertEqual(first_reply, second_reply)
        self.assertEqual(
            first_reply,
            [
                "打劫成功\n"
                "获得：USD 0.10\n"
                "金额已从对方账户转入你的账户。"
            ],
        )
        self.assertEqual(bindings.calls, ["12345", "67890"])
        self.assertEqual(rates, [self.plugin._robbery_success_rate])
        self.assertEqual(len(games.reserve_calls), 1)
        reservation = games.reserve_calls[0]
        self.assertEqual(reservation["debit_user_id"], 22)
        self.assertEqual(reservation["credit_user_id"], 11)
        self.assertEqual(reservation["debit_units"], 10_000_000)
        self.assertEqual(reservation["credit_units"], 10_000_000)
        self.assertEqual(
            [(call["user_id"], call["operation"]) for call in client.calls],
            [(22, "subtract"), (11, "add")],
        )
        self.assertEqual(
            [call["idempotency_key"] for call in client.calls],
            [
                "fapi-game:v1:game-op-1:debit",
                "fapi-game:v1:game-op-1:credit",
            ],
        )

    async def test_robbery_failed_roll_does_not_change_balance(self):
        event = FakeEvent(messages=[SimpleNamespace(type="At", qq="67890")])
        games = InMemoryGameStore(self.module)
        client = BalanceClient(self.module)
        self.plugin.bindings = GameBindings({"12345": 11, "67890": 22})
        self.plugin.games = games
        self.plugin.client = client
        self.module.roll_success = lambda _rate: False

        replies = await collect(self.plugin.robbery(event))

        self.assertEqual(replies, ["打劫失败，本次没有余额变动。"])
        self.assertEqual(client.calls, [])
        self.assertEqual(games.reserve_calls[0]["debit_units"], 0)
        self.assertEqual(games.reserve_calls[0]["credit_units"], 0)

    async def test_robbery_backfire_transfers_actor_balance_to_target(self):
        event = FakeEvent(
            messages=[SimpleNamespace(type="At", qq="67890")],
            message_id="robbery-backfire-message",
        )
        games = InMemoryGameStore(self.module)
        client = BalanceClient(self.module)
        self.plugin.bindings = GameBindings({"12345": 11, "67890": 22})
        self.plugin.games = games
        self.plugin.client = client
        rolls = iter((False, True))
        self.module.roll_success = lambda _rate: next(rolls)

        replies = await collect(self.plugin.robbery(event))

        self.assertEqual(
            replies,
            [
                "打劫失败，触发反噬\n"
                "损失：USD 0.05\n"
                "金额已转入对方账户。"
            ],
        )
        reservation = games.reserve_calls[0]
        self.assertFalse(reservation["won"])
        self.assertEqual(reservation["debit_user_id"], 11)
        self.assertEqual(reservation["credit_user_id"], 22)
        self.assertEqual(reservation["debit_units"], 5_000_000)
        self.assertEqual(reservation["credit_units"], 5_000_000)
        self.assertEqual(
            [(call["user_id"], call["operation"]) for call in client.calls],
            [(11, "subtract"), (22, "add")],
        )
        self.assertEqual(
            self.plugin._game_reply(
                replace(
                    games.operation,
                    status=self.module.GameStatus.REFUNDED,
                )
            ),
            "反噬结算失败，扣除的金额已经退回你的账户。",
        )
        self.assertEqual(
            self.plugin._game_reply(
                replace(
                    games.operation,
                    status=self.module.GameStatus.MANUAL_REVIEW,
                )
            ),
            "反噬结算异常，退款需要管理员人工处理。",
        )
        self.assertEqual(
            self.plugin._game_reply(
                replace(
                    games.operation,
                    status=self.module.GameStatus.PENDING_REFUND,
                )
            ),
            "打劫失败并触发反噬，余额正在结算，请稍后查询。",
        )
        self.assertEqual(
            self.plugin._game_reply(
                replace(
                    games.operation,
                    status=self.module.GameStatus.FAILED,
                )
            ),
            "反噬结算失败，本次未扣款。",
        )

    async def test_robbery_backfire_does_not_credit_when_actor_balance_is_low(self):
        event = FakeEvent(messages=[SimpleNamespace(type="At", qq="67890")])
        games = InMemoryGameStore(self.module)
        client = BalanceClient(self.module, fail_on=1)
        self.plugin.bindings = GameBindings({"12345": 11, "67890": 22})
        self.plugin.games = games
        self.plugin.client = client
        rolls = iter((False, True))
        self.module.roll_success = lambda _rate: next(rolls)

        replies = await collect(self.plugin.robbery(event))

        self.assertEqual(
            replies,
            ["打劫失败并触发反噬，但你的余额不足，本次没有余额变动。"],
        )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["user_id"], 11)
        self.assertEqual(client.calls[0]["operation"], "subtract")

    async def test_disabled_robbery_backfire_never_rolls_or_charges(self):
        event = FakeEvent(messages=[SimpleNamespace(type="At", qq="67890")])
        games = InMemoryGameStore(self.module)
        client = BalanceClient(self.module)
        self.plugin.bindings = GameBindings({"12345": 11, "67890": 22})
        self.plugin.games = games
        self.plugin.client = client
        self.plugin._robbery_backfire_enabled = False
        rates = []

        def failed_roll(rate):
            rates.append(rate)
            return False

        self.module.roll_success = failed_roll

        replies = await collect(self.plugin.robbery(event))

        self.assertEqual(replies, ["打劫失败，本次没有余额变动。"])
        self.assertEqual(rates, [self.plugin._robbery_success_rate])
        self.assertEqual(client.calls, [])

    async def test_robbery_insufficient_target_balance_never_credits_actor(self):
        event = FakeEvent(messages=[SimpleNamespace(type="At", qq="67890")])
        games = InMemoryGameStore(self.module)
        client = BalanceClient(self.module, fail_on=1)
        self.plugin.bindings = GameBindings({"12345": 11, "67890": 22})
        self.plugin.games = games
        self.plugin.client = client
        self.module.roll_success = lambda _rate: True

        replies = await collect(self.plugin.robbery(event))

        self.assertEqual(replies, ["对方余额不足，本次打劫未得手。"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["user_id"], 22)
        self.assertEqual(client.calls[0]["operation"], "subtract")
        self.assertIn(("insufficient", "game-op-1"), games.transition_calls)

    async def test_robbery_requires_both_users_to_be_bound(self):
        event = FakeEvent(messages=[SimpleNamespace(type="At", qq="67890")])
        games = InMemoryGameStore(self.module)
        bindings = GameBindings({"12345": 11})
        self.plugin.bindings = bindings
        self.plugin.games = games

        replies = await collect(self.plugin.robbery(event))

        self.assertEqual(replies, ["被 @ 用户尚未绑定中转站账号，无法打劫。"])
        self.assertCountEqual(bindings.calls, ["12345", "67890"])
        self.assertEqual(games.reserve_calls, [])

    async def test_robbery_reports_cooldown_without_settlement(self):
        event = FakeEvent(messages=[SimpleNamespace(type="At", qq="67890")])
        games = InMemoryGameStore(self.module, cooldown_seconds=37)
        client = BalanceClient(self.module)
        self.plugin.bindings = GameBindings({"12345": 11, "67890": 22})
        self.plugin.games = games
        self.plugin.client = client

        replies = await collect(self.plugin.robbery(event))

        self.assertEqual(replies, ["打劫冷却中，请在 37 秒后再试。"])
        self.assertEqual(client.calls, [])

    async def test_game_credit_failure_refunds_original_debit_user(self):
        operation = game_operation(
            self.module,
            status=self.module.GameStatus.PENDING_CREDIT,
        )
        games = InMemoryGameStore(self.module, operation=operation)

        class Client:
            def __init__(_self):
                _self.calls = []

            async def adjust_balance(_self, **values):
                _self.calls.append(values)
                if len(_self.calls) == 1:
                    raise self.module.TokenRouterError(
                        "credit rejected",
                        status_code=400,
                        api_code="INVALID_BALANCE_ADJUSTMENT",
                    )

        client = Client()
        self.plugin.games = games
        self.plugin.client = client

        settled = await self.plugin._settle_game_operation(operation)

        self.assertEqual(settled.status, self.module.GameStatus.REFUNDED)
        self.assertEqual(
            [(call["user_id"], call["operation"]) for call in client.calls],
            [(11, "add"), (22, "add")],
        )
        self.assertEqual(
            [call["idempotency_key"] for call in client.calls],
            [
                "fapi-game:v1:game-op-1:credit",
                "fapi-game:v1:game-op-1:refund",
            ],
        )
        self.assertIn(("pending_refund", "game-op-1"), games.transition_calls)
        self.assertIn(("refunded", "game-op-1"), games.transition_calls)

    async def test_retryable_game_error_keeps_pending_state(self):
        operation = game_operation(
            self.module,
            game_type="legacy_game",
            status=self.module.GameStatus.PENDING_DEBIT,
            target_qq_id=None,
            target_user_id=None,
            debit_user_id=11,
            credit_user_id=11,
            debit_units=10_000_000,
            credit_units=20_000_000,
        )
        games = InMemoryGameStore(self.module, operation=operation)

        class Client:
            def __init__(_self):
                _self.calls = []

            async def adjust_balance(_self, **values):
                _self.calls.append(values)
                raise self.module.TokenRouterError(
                    "rate limited",
                    status_code=429,
                    retry_after=12.2,
                )

        client = Client()
        self.plugin.games = games
        self.plugin.client = client

        settled = await self.plugin._settle_game_operation(operation)

        self.assertEqual(settled.status, self.module.GameStatus.PENDING_DEBIT)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            client.calls[0]["idempotency_key"],
            "fapi-game:v1:game-op-1:debit",
        )
        self.assertEqual(
            games.transition_calls,
            [
                (
                    "error",
                    "game-op-1",
                    "rate limited",
                    {
                        "base_delay_seconds": 13,
                        "claim_token": "test-game-lease",
                    },
                )
            ],
        )

    async def test_game_settlement_does_not_run_without_database_claim(self):
        operation = game_operation(
            self.module,
            status=self.module.GameStatus.PENDING_DEBIT,
        )
        games = InMemoryGameStore(self.module, operation=operation)

        async def claim_unavailable(_operation_id, **_values):
            return None

        games.claim = claim_unavailable
        client = BalanceClient(self.module)
        self.plugin.games = games
        self.plugin.client = client

        settled = await self.plugin._settle_game_operation(operation)

        self.assertEqual(settled.status, self.module.GameStatus.PENDING_DEBIT)
        self.assertEqual(client.calls, [])

    async def test_permanent_refund_error_uses_bounded_manual_review_path(self):
        operation = game_operation(
            self.module,
            status=self.module.GameStatus.PENDING_REFUND,
        )
        games = InMemoryGameStore(self.module, operation=operation)

        class Client:
            async def adjust_balance(_self, **_values):
                raise self.module.TokenRouterError(
                    "refund rejected",
                    status_code=403,
                    api_code="FORBIDDEN",
                )

        self.plugin.games = games
        self.plugin.client = Client()

        await self.plugin._settle_game_operation(operation)

        self.assertEqual(games.transition_calls[0][0], "refund_error")
        self.assertEqual(
            games.transition_calls[0][3],
            {
                "max_attempts": 5,
                "base_delay_seconds": 5,
                "claim_token": "test-game-lease",
            },
        )
        self.assertEqual(
            self.plugin._game_reply(
                replace(operation, status=self.module.GameStatus.MANUAL_REVIEW)
            ),
            "打劫结算异常，退款需要管理员人工处理。",
        )

    async def test_game_worker_settles_only_claimed_operations(self):
        operation = replace(
            game_operation(
                self.module,
                status=self.module.GameStatus.PENDING_DEBIT,
            ),
            lease_token="worker-lease",
        )
        calls = []

        class Store:
            async def claim_pending(_self, **values):
                calls.append(("claim_pending", values))
                return [operation]

        async def settle(_operation, *, already_claimed=False):
            calls.append(("settle", already_claimed))
            raise asyncio.CancelledError

        self.plugin.games = Store()
        self.plugin._settle_game_operation = settle

        with self.assertRaises(asyncio.CancelledError):
            await self.plugin._game_worker_loop()

        self.assertEqual(calls[0][0], "claim_pending")
        self.assertEqual(calls[1], ("settle", True))

    async def test_binds_sender_qq_email_without_argument(self):
        calls = []

        class Client:
            async def find_users_by_exact_email(_self, email):
                calls.append(("lookup", email))
                return [user(7, "alice", "12345@qq.com")]

        class Store:
            async def bind(_self, **values):
                calls.append(("bind", values))
                return SimpleNamespace(status=self.module.BindStatus.CREATED)

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(self.plugin.bind(FakeEvent()))

        self.assertEqual(replies, ["绑定成功。"])
        self.assertEqual(calls[0], ("lookup", "12345@qq.com"))
        self.assertEqual(
            calls[1],
            (
                "bind",
                {
                    "qq_id": "12345",
                    "tokenrouter_user_id": 7,
                    "email": "12345@qq.com",
                },
            ),
        )

    async def test_duplicate_usernames_do_not_affect_email_binding(self):
        class Client:
            async def find_users_by_exact_email(_self, email):
                self.assertEqual(email, "12345@qq.com")
                return [user(7, "alice", "12345@qq.com")]

        class Store:
            async def bind(_self, **values):
                self.assertEqual(values["tokenrouter_user_id"], 7)
                return SimpleNamespace(status=self.module.BindStatus.CREATED)

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(self.plugin.bind(FakeEvent()))

        self.assertEqual(replies, ["绑定成功。"])

    async def test_reports_missing_automatic_qq_email(self):
        class Client:
            async def find_users_by_exact_email(_self, email):
                self.assertEqual(email, "12345@qq.com")
                return []

        class Store:
            async def bind(_self, **_values):
                raise AssertionError("邮箱不匹配时不应写入数据库")

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(self.plugin.bind(FakeEvent()))

        self.assertEqual(replies, ["未找到邮箱完全匹配的中转站用户，绑定已暂停。"])

    async def test_binds_exact_non_qq_email(self):
        calls = []

        class Client:
            async def find_users_by_exact_email(_self, email):
                calls.append(("lookup", email))
                return [user(7, "alice", "alice@example.com")]

        class Store:
            async def bind(_self, **values):
                calls.append(("bind", values))
                return SimpleNamespace(status=self.module.BindStatus.CREATED)

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(
            self.plugin.bind(FakeEvent(), "alice@example.com")
        )

        self.assertEqual(replies, ["绑定成功。"])
        self.assertEqual(calls[0], ("lookup", "alice@example.com"))
        self.assertEqual(calls[1][1]["tokenrouter_user_id"], 7)
        self.assertEqual(calls[1][1]["email"], "alice@example.com")

    async def test_rejects_legacy_username_and_email_syntax(self):
        class Client:
            async def find_users_by_exact_email(_self, _email):
                raise AssertionError("旧语法不应查询用户")

        class Store:
            async def bind(_self, **_values):
                raise AssertionError("旧语法不应写入数据库")

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(
            self.plugin.bind(FakeEvent(), "alice", "alice@example.com")
        )

        self.assertEqual(replies, ["绑定不再使用用户名，请使用：/fapi 绑定 <完整邮箱>"])

    async def test_email_match_is_case_sensitive(self):
        class Client:
            async def find_users_by_exact_email(_self, email):
                self.assertEqual(email, "alice@example.com")
                return []

        class Store:
            async def bind(_self, **_values):
                raise AssertionError("邮箱大小写不一致时不应写入数据库")

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(self.plugin.bind(FakeEvent(), "alice@example.com"))

        self.assertIn("未找到邮箱完全匹配", replies[0])

    async def test_rejects_invalid_email_before_user_lookup(self):
        class Client:
            async def find_users_by_exact_email(_self, _email):
                raise AssertionError("非法邮箱不应查询用户")

        class Store:
            async def bind(_self, **_values):
                raise AssertionError("非法邮箱不应写入数据库")

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(self.plugin.bind(FakeEvent(), "not-an-email"))

        self.assertIn("邮箱格式不完整", replies[0])

    async def test_rejects_ambiguous_same_email(self):
        class Client:
            async def find_users_by_exact_email(_self, _email):
                return [
                    user(7, "alice", "alice@example.com"),
                    user(8, "duplicate-name", "alice@example.com"),
                ]

        class Store:
            async def bind(_self, **_values):
                raise AssertionError("候选不唯一时不应写入数据库")

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(
            self.plugin.bind(FakeEvent(), "alice@example.com")
        )

        self.assertIn("存在多个", replies[0])

    async def test_allows_group_balance_for_bound_sender(self):
        calls = []

        class Client:
            async def get_balance_summary(_self, user_id):
                calls.append(("balance", user_id))
                return SimpleNamespace(
                    username="RYAN",
                    balance=10,
                    used_last_30_days=1.25,
                    concurrency=5,
                )

        class Store:
            async def get_by_qq(_self, qq_id):
                calls.append(("binding", qq_id))
                return SimpleNamespace(
                    tokenrouter_user_id=73,
                    email="ryan@example.com",
                )

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(
            self.plugin.balance(FakeEvent(group_id="987654321"))
        )

        self.assertEqual(calls, [("binding", "12345"), ("balance", 73)])
        self.assertEqual(
            replies,
            [
                "邮箱：ry...@example.com\n"
                "余额：USD 10.00\n"
                "已使用（近30天）：USD 1.25\n"
                "并发上限：5"
            ],
        )

    async def test_unbound_user_cannot_query_balance(self):
        class Client:
            async def get_balance_summary(_self, _user_id):
                raise AssertionError("未绑定用户不应访问中转站")

        class Store:
            async def get_by_qq(_self, qq_id):
                self.assertEqual(qq_id, "12345")
                return None

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(self.plugin.balance(FakeEvent()))

        self.assertEqual(
            replies,
            ["当前账号尚未绑定中转站账号，请先使用 /fapi 绑定。"],
        )

    async def test_balance_uses_bound_user_id_and_displays_all_fields(self):
        calls = []

        class Client:
            async def get_balance_summary(_self, user_id):
                calls.append(("balance", user_id))
                return SimpleNamespace(
                    user_id=user_id,
                    username="RYAN",
                    balance=10,
                    used_last_30_days=1.2345,
                    concurrency=5,
                )

        class Store:
            async def get_by_qq(_self, qq_id):
                calls.append(("binding", qq_id))
                return SimpleNamespace(
                    tokenrouter_user_id=73,
                    email="r@example.com",
                )

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(self.plugin.balance(FakeEvent()))

        self.assertEqual(calls, [("binding", "12345"), ("balance", 73)])
        self.assertEqual(
            replies,
            [
                "邮箱：r...@example.com\n"
                "余额：USD 10.00\n"
                "已使用（近30天）：USD 1.2345\n"
                "并发上限：5"
            ],
        )

    async def test_checkin_requires_binding_before_reward_reservation(self):
        class Store:
            async def get_by_qq(_self, _qq_id):
                return None

        class Rewards:
            async def reserve_checkin(_self, **_values):
                raise AssertionError("未绑定用户不应预留签到奖励")

        self.plugin.bindings = Store()
        self.plugin.rewards = Rewards()

        replies = await collect(self.plugin.checkin(FakeEvent()))

        self.assertEqual(
            replies,
            ["当前账号尚未绑定中转站账号，请先使用 /fapi 绑定。"],
        )

    async def test_checkin_credits_bound_user_once(self):
        job = self.module.RewardJob(
            operation_id="op-checkin",
            source_type="checkin",
            qq_id="12345",
            tokenrouter_user_id=73,
            amount_units=5_000_000,
            redeem_code="FAPI-OP-CHECKIN",
            status=self.module.RewardJobStatus.PENDING,
            attempt_count=0,
        )

        class Store:
            async def get_by_qq(_self, _qq_id):
                return SimpleNamespace(tokenrouter_user_id=73)

        class Rewards:
            async def reserve_checkin(_self, **values):
                self.assertEqual(values["tokenrouter_user_id"], 73)
                self.assertEqual(values["minimum_amount_units"], 1_000_000)
                self.assertEqual(values["maximum_amount_units"], 10_000_000)
                return self.module.CheckinReservation(
                    job=job,
                    reward_date=values["reward_date"],
                    streak=3,
                    total_days=8,
                    created=True,
                )

        delivered = []

        async def deliver(reward_job):
            delivered.append(reward_job.operation_id)
            return True

        self.plugin.bindings = Store()
        self.plugin.rewards = Rewards()
        self.plugin._deliver_reward = deliver

        replies = await collect(self.plugin.checkin(FakeEvent()))

        self.assertEqual(delivered, ["op-checkin"])
        self.assertEqual(
            replies,
            ["签到成功\n奖励：USD 0.05\n连续签到：3 天\n累计签到：8 天"],
        )

    async def test_completed_checkin_does_not_credit_again(self):
        job = self.module.RewardJob(
            operation_id="op-done",
            source_type="checkin",
            qq_id="12345",
            tokenrouter_user_id=73,
            amount_units=5_000_000,
            redeem_code="FAPI-OP-DONE",
            status=self.module.RewardJobStatus.SUCCEEDED,
            attempt_count=1,
        )

        class Store:
            async def get_by_qq(_self, _qq_id):
                return SimpleNamespace(tokenrouter_user_id=73)

        class Rewards:
            async def reserve_checkin(_self, **values):
                return self.module.CheckinReservation(
                    job=job,
                    reward_date=values["reward_date"],
                    streak=2,
                    total_days=2,
                    created=False,
                )

        async def deliver(_job):
            raise AssertionError("已完成签到不能再次发款")

        self.plugin.bindings = Store()
        self.plugin.rewards = Rewards()
        self.plugin._deliver_reward = deliver

        replies = await collect(self.plugin.checkin(FakeEvent()))

        self.assertEqual(
            replies,
            ["今天已经签到过了。\n连续签到：2 天\n累计签到：2 天"],
        )

    async def test_reward_delivery_keeps_decimal_and_respects_retry_after(self):
        job = self.module.RewardJob(
            operation_id="op-retry",
            source_type="checkin",
            qq_id="12345",
            tokenrouter_user_id=73,
            amount_units=5_000_000,
            redeem_code="FAPIR_FIXED",
            status=self.module.RewardJobStatus.PENDING,
            attempt_count=0,
        )
        calls = []

        class Rewards:
            async def get_reward_job(_self, operation_id):
                self.assertEqual(operation_id, "op-retry")
                return job

            async def record_reward_attempt(_self, operation_id):
                calls.append(("attempt", operation_id))

            async def record_reward_error(
                _self,
                operation_id,
                error,
                *,
                retry_seconds,
            ):
                calls.append(("error", operation_id, error, retry_seconds))

        class Client:
            async def create_and_redeem_reward(_self, **values):
                self.assertEqual(values["amount"], Decimal("0.05"))
                raise self.module.TokenRouterError(
                    "处理中",
                    status_code=409,
                    retry_after=30.2,
                )

        self.plugin.rewards = Rewards()
        self.plugin.client = Client()

        delivered = await self.plugin._deliver_reward(job)

        self.assertFalse(delivered)
        self.assertEqual(calls[0], ("attempt", "op-retry"))
        self.assertEqual(calls[1], ("error", "op-retry", "处理中", 31))

    async def test_regular_group_member_cannot_create_red_packet(self):
        class Rewards:
            async def create_red_packet(_self, **_values):
                raise AssertionError("普通群员不能创建平台红包")

        self.plugin.rewards = Rewards()
        replies = await collect(
            self.plugin.create_red_packet(
                FakeEvent(group_id="987654321", role="member"),
                "1.00",
                2,
            )
        )

        self.assertEqual(
            replies,
            ["只有群主、群管理员或机器人管理员可以创建红包。"],
        )

    async def test_group_admin_can_create_red_packet(self):
        class Rewards:
            async def create_red_packet(_self, **values):
                self.assertEqual(values["group_id"], "987654321")
                self.assertEqual(values["total_units"], 100_000_000)
                self.assertEqual(values["total_count"], 2)
                return SimpleNamespace(
                    packet_id=9,
                    total_units=100_000_000,
                    total_count=2,
                )

        self.plugin.rewards = Rewards()
        replies = await collect(
            self.plugin.create_red_packet(
                FakeEvent(group_id="987654321", role="admin"),
                "1.00",
                2,
            )
        )

        self.assertIn("群红包 #9 已创建", replies[0])
        self.assertIn("总金额：USD 1.00", replies[0])
        self.assertIn("领取指令：/fapi 抢红包 9", replies[0])
        self.assertNotIn("抢红包 领取", replies[0])

    async def test_official_group_role_does_not_bypass_astrbot_admin_check(self):
        class Rewards:
            async def create_red_packet(_self, **_values):
                raise AssertionError("官机群角色不可作为红包创建权限")

        self.plugin._allowed_group_id = "official-group-openid"
        self.plugin.rewards = Rewards()
        replies = await collect(
            self.plugin.create_red_packet(
                FakeEvent(
                    sender_id="official-member-openid",
                    platform_name="qq_official",
                    group_id="official-group-openid",
                    role="admin",
                ),
                "1.00",
                2,
            )
        )

        self.assertEqual(
            replies,
            ["QQ 官方机器人仅允许 AstrBot 管理员创建红包。"],
        )

    async def test_astrbot_admin_can_create_red_packet_in_official_group(self):
        class Rewards:
            async def create_red_packet(_self, **values):
                self.assertEqual(values["group_id"], "official-group-openid")
                self.assertEqual(
                    values["creator_qq_id"],
                    "official-member-openid",
                )
                return SimpleNamespace(
                    packet_id=11,
                    total_units=100_000_000,
                    total_count=2,
                )

        self.plugin._allowed_group_id = "official-group-openid"
        self.plugin.rewards = Rewards()
        replies = await collect(
            self.plugin.create_red_packet(
                FakeEvent(
                    sender_id="official-member-openid",
                    platform_name="qq_official",
                    group_id="official-group-openid",
                    role=None,
                    astrbot_admin=True,
                ),
                "1.00",
                2,
            )
        )

        self.assertIn("群红包 #11 已创建", replies[0])

    async def test_red_packet_creation_key_is_stable_and_collision_resistant(self):
        event = FakeEvent(message_id="message-1")
        first = self.plugin._red_packet_creation_key(
            event,
            "qq_official",
            "group-openid",
        )
        second = self.plugin._red_packet_creation_key(
            event,
            "qq_official",
            "group-openid",
        )
        same_prefix_group = "g" * 128
        another_group = "g" * 127 + "h"
        long_message = "m" * 128

        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 191)
        self.assertNotEqual(
            first,
            self.plugin._red_packet_creation_key(
                event,
                "aiocqhttp",
                "group-openid",
            ),
        )
        self.assertNotEqual(
            self.plugin._red_packet_creation_key(
                FakeEvent(message_id=long_message),
                "qq_official",
                same_prefix_group,
            ),
            self.plugin._red_packet_creation_key(
                FakeEvent(message_id=long_message),
                "qq_official",
                another_group,
            ),
        )

    async def test_red_packet_requires_stable_message_id(self):
        class Rewards:
            async def create_red_packet(_self, **_values):
                raise AssertionError("缺少消息编号时不得创建红包")

        self.plugin.rewards = Rewards()
        replies = await collect(
            self.plugin.create_red_packet(
                FakeEvent(
                    group_id="987654321",
                    role="admin",
                    message_id=None,
                ),
                "1.00",
                2,
            )
        )

        self.assertEqual(
            replies,
            ["无法确认当前消息编号，为避免重复创建红包，本次操作已取消。"],
        )

    async def test_private_chat_cannot_create_red_packet(self):
        replies = await collect(
            self.plugin.create_red_packet(
                FakeEvent(group_id=None, role="owner", astrbot_admin=True),
                "1.00",
                2,
            )
        )

        self.assertEqual(replies, ["该指令只能在管理员配置的指定群中使用。"])

    async def test_claim_red_packet_uses_current_group_and_bound_user(self):
        job = self.module.RewardJob(
            operation_id="op-claim",
            source_type="red_packet",
            qq_id="12345",
            tokenrouter_user_id=73,
            amount_units=12_000_000,
            redeem_code="FAPI-OP-CLAIM",
            status=self.module.RewardJobStatus.PENDING,
            attempt_count=0,
        )

        class Store:
            async def get_by_qq(_self, _qq_id):
                return SimpleNamespace(tokenrouter_user_id=73)

        class Rewards:
            async def reserve_red_packet_claim(_self, **values):
                self.assertEqual(values["group_id"], "987654321")
                self.assertEqual(values["tokenrouter_user_id"], 73)
                self.assertEqual(values["packet_id"], 9)
                return self.module.RedPacketClaimReservation(
                    packet_id=9,
                    job=job,
                    created=True,
                )

        self.plugin.bindings = Store()
        self.plugin.rewards = Rewards()
        self.plugin._deliver_reward = lambda _job: _async_value(True)

        replies = await collect(
            self.plugin.claim_red_packet(
                FakeEvent(group_id="987654321"),
                9,
            )
        )

        self.assertEqual(
            replies,
            ["抢到红包 #9\n金额：USD 0.12\n奖励已加入中转站余额。"],
        )

    async def test_claim_red_packet_rejects_non_numeric_id_before_storage(self):
        class Store:
            async def get_by_qq(_self, _qq_id):
                raise AssertionError("非法红包编号不得访问绑定数据库")

        self.plugin.bindings = Store()

        replies = await collect(
            self.plugin.claim_red_packet(FakeEvent(group_id="987654321"), "领取")
        )

        self.assertEqual(replies, ["红包编号不正确。"])

    async def test_red_packet_status_rejects_non_numeric_id_before_storage(self):
        class Rewards:
            async def get_red_packet(_self, **_values):
                raise AssertionError("非法红包编号不得访问红包数据库")

        self.plugin.rewards = Rewards()

        replies = await collect(
            self.plugin.red_packet_status(FakeEvent(group_id="987654321"), "领取")
        )

        self.assertEqual(replies, ["红包编号不正确。"])


    async def test_bind_reports_persistent_cooldown(self):
        class Client:
            async def find_users_by_exact_email(_self, _email):
                return [user(7, "alice", "alice@example.com")]

        class Store:
            async def bind(_self, **_values):
                raise self.module.BindCooldownActive(90)

        self.plugin.client = Client()
        self.plugin.bindings = Store()
        replies = await collect(self.plugin.bind(FakeEvent(), "alice@example.com"))

        self.assertEqual(replies, ["绑定冷却中，请在 1 分钟 30 秒 后再绑定。"])

    async def test_reward_ranking_masks_email_and_uses_settled_rows(self):
        class Rewards:
            async def get_ranking(_self, **values):
                self.assertEqual(values, {"source_type": "checkin", "limit": 10})
                return [
                    SimpleNamespace(
                        tokenrouter_user_id=7,
                        count=12,
                        amount_units=150_000_000,
                    )
                ]

        class Bindings:
            async def get_by_user_ids(_self, user_ids):
                self.assertEqual(user_ids, {7})
                return {7: SimpleNamespace(email="alice@example.com")}

        self.plugin.rewards = Rewards()
        self.plugin.bindings = Bindings()
        replies = await collect(self.plugin.checkin_ranking(FakeEvent()))

        self.assertIn("al...@example.com", replies[0])
        self.assertNotIn("alice@example.com", replies[0])
        self.assertIn("12 天", replies[0])

    async def test_user_ledger_marks_pending_reward_without_claiming_arrival(self):
        created_at = self.module.datetime(2026, 10, 12, 1, 2, 3)

        class Bindings:
            async def get_by_qq(_self, _qq_id):
                return SimpleNamespace(
                    tokenrouter_user_id=7,
                    email="alice@example.com",
                )

        class Rewards:
            async def get_user_ledger(_self, _user_id, **_values):
                return [
                    SimpleNamespace(
                        operation_id="reward-1",
                        source_type="checkin",
                        amount_units=5_000_000,
                        status=self.module.RewardJobStatus.PENDING,
                        created_at=created_at,
                    )
                ]

        class Games:
            async def get_user_operations(_self, _user_id, **_values):
                return []

        self.plugin.bindings = Bindings()
        self.plugin.rewards = Rewards()
        self.plugin.games = Games()
        replies = await collect(self.plugin.ledger(FakeEvent()))

        self.assertIn("签到 +USD 0.05（待补发）", replies[0])
        self.assertIn("al...@example.com", replies[0])

    async def test_admin_can_list_and_retry_existing_reward_task(self):
        job = self.module.RewardJob(
            operation_id="reward-task-1",
            source_type="checkin",
            qq_id="12345",
            tokenrouter_user_id=7,
            amount_units=5_000_000,
            redeem_code="FAPIR_TEST",
            status=self.module.RewardJobStatus.PENDING,
            attempt_count=2,
            created_at=self.module.datetime(2026, 10, 12, 1, 0, 0),
        )
        retried = []

        class Rewards:
            async def list_incomplete_jobs(_self, **_values):
                return [job]

            async def get_reward_job(_self, operation_id):
                return job if operation_id == job.operation_id else None

            async def retry_reward_job(_self, operation_id):
                retried.append(operation_id)
                return True

        class Games:
            async def list_incomplete(_self, **_values):
                return []

            async def get(_self, _operation_id):
                raise AssertionError("奖励任务不应查询游戏表")

        self.plugin.rewards = Rewards()
        self.plugin.games = Games()
        event = FakeEvent(group_id=None, astrbot_admin=True)
        listed = await collect(self.plugin.admin_private(event, "未完成任务"))
        retried_reply = await collect(
            self.plugin.admin_private(event, "重试 reward-task-1")
        )

        self.assertIn("reward-task-1", listed[0])
        self.assertIn("固定兑换码不变", retried_reply[0])
        self.assertEqual(retried, ["reward-task-1"])

    async def test_daily_broadcast_reply_contains_site_and_reward_stats(self):
        overview = SimpleNamespace(
            today_requests=12,
            today_tokens=345,
            today_actual_cost=Decimal("1.25"),
            rpm=2,
            tpm=30,
            stats_stale=False,
        )
        stats = self.module.DailyRewardStats(
            reward_date=self.module.date(2026, 10, 12),
            checkin_count=3,
            checkin_units=15_000_000,
            red_packet_count=1,
            red_packet_units=100_000_000,
            claim_count=2,
            claim_units=50_000_000,
        )

        reply = self.plugin._daily_broadcast_reply(overview, stats)

        self.assertIn("FAPI 每日播报 2026-10-12", reply)
        self.assertIn("签到：3 人", reply)
        self.assertIn("红包到账：2 份", reply)

    async def test_daily_broadcast_sends_only_after_database_claim(self):
        sent = []

        class MessageChain:
            def message(chain_self, text):
                chain_self.text = text
                return chain_self

        class Context:
            async def send_message(_self, umo, chain):
                sent.append((umo, chain.text))

        class Client:
            calls = 0

            async def get_dashboard_overview(client_self):
                client_self.calls += 1
                return SimpleNamespace(
                    today_requests=12,
                    today_tokens=345,
                    today_actual_cost=Decimal("1.25"),
                    rpm=2,
                    tpm=30,
                    stats_stale=False,
                )

        class Rewards:
            claims = 0
            completed = []

            async def claim_daily_broadcast(rewards_self, **_values):
                rewards_self.claims += 1
                return "lease-1" if rewards_self.claims == 1 else None

            async def get_daily_stats(_self, **values):
                return self.module.DailyRewardStats(
                    reward_date=values["reward_date"],
                    checkin_count=3,
                    checkin_units=15_000_000,
                    red_packet_count=1,
                    red_packet_units=100_000_000,
                    claim_count=2,
                    claim_units=50_000_000,
                )

            async def mark_daily_broadcast_sent(rewards_self, **values):
                rewards_self.completed.append(values)

        event_module = sys.modules["astrbot.api.event"]
        event_module.MessageChain = MessageChain
        self.plugin.context = Context()
        self.plugin.client = Client()
        self.plugin.rewards = Rewards()
        self.plugin._daily_broadcast_umo = "FapiHub:GroupMessage:987654321"

        await self.plugin._send_daily_broadcast()
        await self.plugin._send_daily_broadcast()

        self.assertEqual(len(sent), 1)
        self.assertEqual(self.plugin.client.calls, 1)
        self.assertEqual(len(self.plugin.rewards.completed), 1)
        self.assertEqual(self.plugin.rewards.completed[0]["lease_token"], "lease-1")

    async def test_refund_ledger_never_shows_unreceived_credit(self):
        for status, expected_status in (
            (self.module.GameStatus.PENDING_REFUND, "入账失败，付款方退款中"),
            (
                self.module.GameStatus.MANUAL_REVIEW,
                "入账失败，付款方退款待人工处理",
            ),
        ):
            with self.subTest(status=status):
                operation = game_operation(
                    self.module,
                    status=status,
                    debit_user_id=22,
                    credit_user_id=11,
                    debit_units=10_000_000,
                    credit_units=10_000_000,
                )

                receiver_line = self.plugin._game_ledger_line(operation, 11)
                payer_line = self.plugin._game_ledger_line(operation, 22)

                self.assertIn(f"收入 USD 0.00（{expected_status}）", receiver_line)
                self.assertNotIn("收入 +USD 0.10", receiver_line)
                self.assertIn("支出 -USD 0.10", payer_line)


async def _async_value(value):
    return value


if __name__ == "__main__":
    unittest.main()
