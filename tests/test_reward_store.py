import math
import unittest
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from reward_store import (
    CHECKIN_REWARD_STEP_UNITS,
    MIN_RED_PACKET_UNITS,
    MONEY_SCALE,
    MySQLRewardStore,
    _redeem_code,
    money_to_units,
    random_checkin_reward_units,
    split_red_packet_share,
    units_to_decimal,
)


class RedeemCodeTest(unittest.TestCase):
    def test_is_deterministic_ascii_and_fits_tokenrouter_database(self):
        operation_id = "12345678-1234-5678-1234-567812345678"

        first = _redeem_code(operation_id)
        second = _redeem_code(operation_id)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)
        self.assertTrue(first.isascii())
        self.assertTrue(first.startswith("FAPIR_"))

    def test_different_operations_get_different_codes(self):
        self.assertNotEqual(_redeem_code("operation-a"), _redeem_code("operation-b"))


class MoneyUnitsTest(unittest.TestCase):
    def test_round_trips_eight_decimal_places_exactly(self):
        units = money_to_units("12.12345678")

        self.assertEqual(units, 1_212_345_678)
        self.assertEqual(units_to_decimal(units), Decimal("12.12345678"))

    def test_rejects_invalid_or_overprecise_amounts(self):
        for value in ["0", "-1", "NaN", "Infinity", "0.000000001", "abc"]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    money_to_units(value)

    def test_accepts_zero_only_when_explicitly_allowed(self):
        self.assertEqual(money_to_units("0", allow_zero=True), 0)


class CheckinRewardTest(unittest.TestCase):
    def test_includes_both_ends_of_configured_range(self):
        minimum = money_to_units("0.01")
        maximum = money_to_units("0.10")

        self.assertEqual(
            random_checkin_reward_units(minimum, maximum, randbelow=lambda _n: 0),
            minimum,
        )
        self.assertEqual(
            random_checkin_reward_units(
                minimum,
                maximum,
                randbelow=lambda choices: choices - 1,
            ),
            maximum,
        )

    def test_uses_one_cent_steps(self):
        minimum = money_to_units("0.03")
        reward = random_checkin_reward_units(
            minimum,
            money_to_units("0.08"),
            randbelow=lambda _n: 4,
        )

        self.assertEqual(reward, money_to_units("0.07"))
        self.assertEqual(reward % CHECKIN_REWARD_STEP_UNITS, 0)

    def test_budget_tail_is_rounded_down_to_a_cent(self):
        reward = random_checkin_reward_units(
            money_to_units("0.01"),
            money_to_units("0.075"),
            randbelow=lambda choices: choices - 1,
        )

        self.assertEqual(reward, money_to_units("0.07"))

    def test_rejects_invalid_range_or_sub_cent_minimum(self):
        with self.assertRaises(ValueError):
            random_checkin_reward_units(
                money_to_units("0.10"),
                money_to_units("0.01"),
            )
        with self.assertRaises(ValueError):
            random_checkin_reward_units(
                money_to_units("0.001"),
                money_to_units("0.01"),
            )


class RewardStoreDatabaseBehaviorTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def store():
        return MySQLRewardStore(
            host="db.example.com",
            port=3306,
            user="fapi",
            password="secret",
            database="astrbot",
        )

    async def test_initialize_sets_mysql_session_timezone_to_utc(self):
        pool = SimpleNamespace()
        create_pool = AsyncMock(return_value=pool)
        store = self.store()
        store._create_tables = AsyncMock()

        with patch(
            "reward_store.aiomysql",
            SimpleNamespace(create_pool=create_pool, DictCursor=object()),
        ):
            await store.initialize()

        self.assertEqual(
            create_pool.await_args.kwargs["init_command"],
            "SET time_zone = '+00:00'",
        )
        store._create_tables.assert_awaited_once_with()

    async def test_pending_previous_checkin_preserves_streak(self):
        class Cursor:
            def __init__(self):
                self.sql = ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def execute(self, sql, _params=None):
                self.sql = sql

            async def fetchone(self):
                return {"streak": 6}

        cursor = Cursor()
        connection = SimpleNamespace(cursor=lambda: cursor)

        streak = await self.store()._previous_checkin_streak(
            connection,
            "member-openid",
            date(2026, 10, 10),
        )

        self.assertEqual(streak, 6)
        self.assertIn("status NOT IN ('failed', 'cancelled')", cursor.sql)
        self.assertNotIn("status = 'succeeded'", cursor.sql)

    async def test_existing_pending_checkin_counts_as_valid_day(self):
        class Cursor:
            def __init__(self):
                self.sql = ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def execute(self, sql, _params=None):
                self.sql = sql

            async def fetchone(self):
                return {
                    "operation_id": "operation-1",
                    "reward_date": date(2026, 10, 11),
                    "streak": 7,
                    "source_type": "checkin",
                    "qq_id": "member-openid",
                    "tokenrouter_user_id": 42,
                    "amount_units": MONEY_SCALE,
                    "redeem_code": "FAPIR_ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                    "status": "pending",
                    "attempt_count": 1,
                    "first_attempt_at": datetime(2026, 10, 11, 0, 0, 0),
                    "valid_days": 7,
                }

        cursor = Cursor()
        connection = SimpleNamespace(cursor=lambda: cursor)

        reservation = await self.store()._get_checkin(
            connection,
            "member-openid",
            date(2026, 10, 11),
            for_update=True,
        )

        self.assertIsNotNone(reservation)
        self.assertEqual(reservation.streak, 7)
        self.assertEqual(reservation.total_days, 7)
        self.assertEqual(reservation.job.status.value, "pending")
        self.assertIn("status NOT IN ('failed', 'cancelled')", cursor.sql)
        self.assertTrue(cursor.sql.rstrip().endswith("FOR UPDATE"))

    async def test_valid_checkin_count_excludes_terminal_failures(self):
        class Cursor:
            def __init__(self):
                self.sql = ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def execute(self, sql, _params=None):
                self.sql = sql

            async def fetchone(self):
                return {"total": 4}

        cursor = Cursor()
        connection = SimpleNamespace(cursor=lambda: cursor)

        total = await self.store()._valid_checkin_count(connection, "member-openid")

        self.assertEqual(total, 4)
        self.assertIn("status NOT IN ('failed', 'cancelled')", cursor.sql)


class RedPacketSplitTest(unittest.TestCase):
    def test_last_share_receives_all_remaining_units(self):
        self.assertEqual(split_red_packet_share(123_456_789, 1), 123_456_789)

    def test_every_share_keeps_minimum_for_remaining_people(self):
        remaining = money_to_units("1.00")
        count = 10
        shares = []
        while count:
            share = split_red_packet_share(
                remaining,
                count,
                randbelow=lambda choices: choices - 1,
            )
            shares.append(share)
            remaining -= share
            count -= 1

        self.assertEqual(sum(shares), MONEY_SCALE)
        self.assertTrue(all(share >= MIN_RED_PACKET_UNITS for share in shares))
        self.assertEqual(remaining, 0)

    def test_rejects_impossible_distribution(self):
        with self.assertRaises(ValueError):
            split_red_packet_share(MIN_RED_PACKET_UNITS, 2)


class PlatformIdMigrationTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def store():
        return MySQLRewardStore(
            host="db.example.com",
            port=3306,
            user="fapi",
            password="secret",
            database="astrbot",
        )

    async def test_migrates_all_legacy_identity_columns(self):
        class Cursor:
            def __init__(self):
                self.executions = []

            async def execute(self, sql, params=None):
                self.executions.append((sql, params))

            async def fetchone(self):
                return {
                    "max_length": 20,
                    "collation_name": "utf8mb4_unicode_ci",
                }

        cursor = Cursor()
        await self.store()._ensure_platform_id_columns(cursor)

        alters = [
            sql for sql, _params in cursor.executions if "ALTER TABLE" in sql
        ]
        self.assertEqual(len(alters), 5)
        self.assertTrue(all("VARCHAR(128)" in sql for sql in alters))
        self.assertTrue(all("utf8mb4_bin" in sql for sql in alters))

    async def test_skips_current_identity_columns(self):
        class Cursor:
            def __init__(self):
                self.executions = []

            async def execute(self, sql, params=None):
                self.executions.append((sql, params))

            async def fetchone(self):
                return {
                    "max_length": 128,
                    "collation_name": "utf8mb4_bin",
                }

        cursor = Cursor()
        await self.store()._ensure_platform_id_columns(cursor)

        self.assertFalse(any("ALTER TABLE" in sql for sql, _ in cursor.executions))


class RedPacketIdempotencyTest(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_insert_recovers_existing_packet(self):
        expires_at = datetime(2026, 10, 10, 12, 0, 0)
        existing = {
            "packet_id": 9,
            "group_id": "official-group-openid",
            "creator_qq_id": "official-member-openid",
            "total_units": MONEY_SCALE,
            "remaining_units": MONEY_SCALE,
            "total_count": 2,
            "remaining_count": 2,
            "expires_at": expires_at,
            "credited_count": 0,
        }

        class Cursor:
            lastrowid = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def execute(self, _sql, _params=None):
                raise Exception(1062, "duplicate creation key")

        class Connection:
            def __init__(self):
                self.rollback_count = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def begin(self):
                return None

            async def rollback(self):
                self.rollback_count += 1

            def cursor(self):
                return Cursor()

        class Pool:
            def __init__(self, connection):
                self.connection = connection

            def acquire(self):
                return self.connection

        store = MySQLRewardStore(
            host="db.example.com",
            port=3306,
            user="fapi",
            password="secret",
            database="astrbot",
        )
        connection = Connection()
        store._pool = Pool(connection)

        async def lock_budget(*_args):
            return None

        async def get_budget(*_args):
            return {"red_packet_reserved_units": 0}

        lookups = iter((None, existing))

        async def get_existing(*_args):
            return next(lookups)

        store._lock_budget_row = lock_budget
        store._get_budget = get_budget
        store._get_packet_by_creation_key = get_existing

        packet = await store.create_red_packet(
            creation_key="fapi-red-packet:v1:test",
            group_id="official-group-openid",
            creator_qq_id="official-member-openid",
            total_units=MONEY_SCALE,
            total_count=2,
            reward_date=date(2026, 10, 10),
            daily_budget_units=10 * MONEY_SCALE,
            expires_at=expires_at,
        )

        self.assertFalse(packet.created)
        self.assertEqual(packet.packet_id, 9)
        self.assertEqual(connection.rollback_count, 1)

    def test_existing_packet_rejects_mismatched_parameters(self):
        existing = {
            "packet_id": 9,
            "group_id": "official-group-openid",
            "creator_qq_id": "official-member-openid",
            "total_units": MONEY_SCALE,
            "remaining_units": MONEY_SCALE,
            "total_count": 2,
            "remaining_count": 2,
            "expires_at": datetime(2026, 10, 10, 12, 0, 0),
            "credited_count": 0,
        }

        with self.assertRaisesRegex(Exception, "参数不一致"):
            MySQLRewardStore._existing_red_packet_record(
                existing,
                group_id="another-group",
                creator_qq_id="official-member-openid",
                total_units=MONEY_SCALE,
                total_count=2,
            )


if __name__ == "__main__":
    unittest.main()
