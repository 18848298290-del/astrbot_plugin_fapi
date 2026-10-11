import importlib.util
import logging
import sys
import types
import unittest
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

    def register(*_args, **_kwargs):
        return lambda plugin_class: plugin_class

    api.AstrBotConfig = AstrBotConfig
    api.logger = logging.getLogger("fapi-plugin-test")
    event_module.AstrMessageEvent = object
    event_module.filter = Filter()
    star_module.Context = object
    star_module.Star = Star
    star_module.register = register

    modules = {
        package_name: package,
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_module,
        "astrbot.api.star": star_module,
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
    ):
        self.sender_id = sender_id
        self.platform_name = platform_name
        self.group_id = group_id
        self.astrbot_admin = astrbot_admin
        self.group = group
        self.message_obj = SimpleNamespace(
            raw_message={"sender": {"role": role}} if role is not None else {},
            message_id=message_id,
        )

    def get_sender_id(self):
        return self.sender_id

    def get_platform_name(self):
        return self.platform_name

    def get_group_id(self):
        return self.group_id

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

    async def test_requires_username(self):
        replies = await collect(self.plugin.bind(FakeEvent()))

        self.assertEqual(len(replies), 1)
        self.assertIn("/fapi 绑定 <用户名>", replies[0])

    async def test_onebot_help_menu_lists_commands_and_permissions(self):
        replies = await collect(self.plugin.help_menu(FakeEvent()))

        self.assertEqual(len(replies), 1)
        self.assertIn("FAPI 功能菜单", replies[0])
        self.assertIn("/fapi 绑定 <用户名>", replies[0])
        self.assertIn("/fapi 余额", replies[0])
        self.assertIn("/fapi 签到", replies[0])
        self.assertIn("/fapi 抢红包 [红包编号]", replies[0])
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
        self.assertIn("/fapi 绑定 <用户名> <完整邮箱>", replies[0])
        self.assertNotIn("/fapi 绑定 <用户名>\n", replies[0])
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
            async def find_users_by_exact_username(_self, _username):
                raise AssertionError("私聊绑定不应查询中转站")

        class Store:
            async def bind(_self, **_values):
                raise AssertionError("私聊绑定不应写入数据库")

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(
            self.plugin.bind(FakeEvent(group_id=None), "alice")
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
        event = FakeEvent(role="admin", astrbot_admin=True)
        commands = (
            self.plugin.help_menu(event),
            self.plugin.bind(event, "alice", "alice@example.com"),
            self.plugin.binding_status(event),
            self.plugin.balance(event),
            self.plugin.checkin(event),
            self.plugin.create_red_packet(event, "1.00", 1),
            self.plugin.claim_red_packet(event),
            self.plugin.red_packet_status(event),
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
        event = FakeEvent(
            group_id="another-group-openid",
            role="admin",
            astrbot_admin=True,
        )
        commands = (
            self.plugin.help_menu(event),
            self.plugin.bind(event, "alice", "alice@example.com"),
            self.plugin.binding_status(event),
            self.plugin.balance(event),
            self.plugin.checkin(event),
            self.plugin.create_red_packet(event, "1.00", 1),
            self.plugin.claim_red_packet(event),
            self.plugin.red_packet_status(event),
        )

        for command in commands:
            self.assertEqual(
                await collect(command),
                ["该指令只能在管理员配置的指定群中使用。"],
            )

    async def test_official_bot_requires_full_email_before_user_lookup(self):
        class Client:
            async def find_users_by_exact_username(_self, _username):
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
                "alice",
            )
        )

        self.assertEqual(
            replies,
            [
                "QQ 官方机器人无法取得真实 QQ 号，请使用："
                "/fapi 绑定 <用户名> <完整邮箱>"
            ],
        )

    async def test_official_bot_binds_openid_with_exact_email(self):
        calls = []

        class Client:
            async def find_users_by_exact_username(_self, username):
                calls.append(("lookup", username))
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
                "alice",
                "alice@example.com",
            )
        )

        self.assertEqual(replies, ["绑定成功。"])
        self.assertEqual(calls[0], ("lookup", "alice"))
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
            async def find_users_by_exact_username(_self, _username):
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
                "alice",
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

    async def test_binds_exact_username_with_sender_qq_email(self):
        calls = []

        class Client:
            async def find_users_by_exact_username(_self, username):
                calls.append(("lookup", username))
                return [user(7, "alice", "12345@qq.com")]

        class Store:
            async def bind(_self, **values):
                calls.append(("bind", values))
                return SimpleNamespace(status=self.module.BindStatus.CREATED)

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(self.plugin.bind(FakeEvent(), "alice"))

        self.assertEqual(replies, ["绑定成功。"])
        self.assertEqual(calls[0], ("lookup", "alice"))
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

    async def test_selects_qq_email_user_when_exact_username_is_duplicated(self):
        class Client:
            async def find_users_by_exact_username(_self, _username):
                return [
                    user(6, "alice", "alice@example.com"),
                    user(7, "alice", "12345@qq.com"),
                ]

        class Store:
            async def bind(_self, **values):
                self.assertEqual(values["tokenrouter_user_id"], 7)
                return SimpleNamespace(status=self.module.BindStatus.CREATED)

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(self.plugin.bind(FakeEvent(), "alice"))

        self.assertEqual(replies, ["绑定成功。"])

    async def test_prompts_for_email_when_username_has_no_qq_email(self):
        class Client:
            async def find_users_by_exact_username(_self, _username):
                return [user(7, "alice", "alice@example.com")]

        class Store:
            async def bind(_self, **_values):
                raise AssertionError("邮箱不匹配时不应写入数据库")

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(self.plugin.bind(FakeEvent(), "alice"))

        self.assertEqual(len(replies), 1)
        self.assertIn("/fapi 绑定 alice <完整邮箱>", replies[0])

    async def test_binds_exact_username_and_non_qq_email(self):
        calls = []

        class Client:
            async def find_users_by_exact_username(_self, username):
                calls.append(("lookup", username))
                return [user(7, "alice", "alice@example.com")]

        class Store:
            async def bind(_self, **values):
                calls.append(("bind", values))
                return SimpleNamespace(status=self.module.BindStatus.CREATED)

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(
            self.plugin.bind(FakeEvent(), "alice", "alice@example.com")
        )

        self.assertEqual(replies, ["绑定成功。"])
        self.assertEqual(calls[0], ("lookup", "alice"))
        self.assertEqual(calls[1][1]["tokenrouter_user_id"], 7)
        self.assertEqual(calls[1][1]["email"], "alice@example.com")

    async def test_rejects_email_that_does_not_match_exact_username(self):
        class Client:
            async def find_users_by_exact_username(_self, _username):
                return [user(7, "alice", "alice@example.com")]

        class Store:
            async def bind(_self, **_values):
                raise AssertionError("用户名和邮箱错配时不应写入数据库")

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(
            self.plugin.bind(FakeEvent(), "alice", "other@example.com")
        )

        self.assertEqual(len(replies), 1)
        self.assertIn("未同时完全匹配", replies[0])

    async def test_email_match_is_case_sensitive(self):
        class Client:
            async def find_users_by_exact_username(_self, _username):
                return [user(7, "alice", "Alice@Example.com")]

        class Store:
            async def bind(_self, **_values):
                raise AssertionError("邮箱大小写不一致时不应写入数据库")

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(
            self.plugin.bind(FakeEvent(), "alice", "alice@example.com")
        )

        self.assertIn("未同时完全匹配", replies[0])

    async def test_rejects_email_belonging_to_different_user(self):
        class Client:
            async def find_users_by_exact_username(_self, _username):
                return [user(7, "alice", "alice@example.com")]

        class Store:
            async def bind(_self, **_values):
                raise AssertionError("不得跨用户组合用户名和邮箱")

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(
            self.plugin.bind(FakeEvent(), "alice", "bob@example.com")
        )

        self.assertIn("同一个中转站用户", replies[0])

    async def test_rejects_ambiguous_same_username_and_email(self):
        class Client:
            async def find_users_by_exact_username(_self, _username):
                return [
                    user(7, "alice", "alice@example.com"),
                    user(8, "alice", "alice@example.com"),
                ]

        class Store:
            async def bind(_self, **_values):
                raise AssertionError("候选不唯一时不应写入数据库")

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(
            self.plugin.bind(FakeEvent(), "alice", "alice@example.com")
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
                return SimpleNamespace(tokenrouter_user_id=73)

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(
            self.plugin.balance(FakeEvent(group_id="987654321"))
        )

        self.assertEqual(calls, [("binding", "12345"), ("balance", 73)])
        self.assertEqual(
            replies,
            [
                "用户名：RYAN\n"
                "余额：$10.00\n"
                "已使用（近30天）：$1.25\n"
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
                return SimpleNamespace(tokenrouter_user_id=73)

        self.plugin.client = Client()
        self.plugin.bindings = Store()

        replies = await collect(self.plugin.balance(FakeEvent()))

        self.assertEqual(calls, [("binding", "12345"), ("balance", 73)])
        self.assertEqual(
            replies,
            [
                "用户名：RYAN\n"
                "余额：$10.00\n"
                "已使用（近30天）：$1.2345\n"
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
            ["签到成功\n奖励：$0.05\n连续签到：3 天\n累计签到：8 天"],
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
        self.assertIn("总金额：$1.00", replies[0])

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
            ["抢到红包 #9\n金额：$0.12\n奖励已加入中转站余额。"],
        )


async def _async_value(value):
    return value


if __name__ == "__main__":
    unittest.main()
