import unittest
from datetime import datetime

from game_store import (
    GameCooldownActive,
    GameOperation,
    GameReservation,
    GameStatus,
    MySQLGameStore,
    roll_success,
)


NOW = datetime(2026, 10, 11, 12, 0, 0)


def operation_row(**overrides):
    row = {
        "operation_id": "operation-1",
        "operation_key": "message-1:robbery",
        "game_type": "robbery",
        "group_id": "group-openid",
        "actor_qq_id": "actor-openid",
        "actor_user_id": 11,
        "target_qq_id": "target-openid",
        "target_user_id": 22,
        "debit_user_id": 11,
        "credit_user_id": 22,
        "debit_units": 1_000_000,
        "credit_units": 1_000_000,
        "won": 1,
        "status": "pending_debit",
        "attempt_count": 0,
        "last_error": None,
        "next_attempt_at": NOW,
        "created_at": NOW,
    }
    row.update(overrides)
    return row


class RollSuccessTest(unittest.TestCase):
    def test_zero_and_one_hundred_do_not_call_rng(self):
        def fail(_bound):
            raise AssertionError("rng should not be called at exact boundaries")

        self.assertFalse(roll_success(0, randbelow=fail))
        self.assertTrue(roll_success(100, randbelow=fail))

    def test_percentage_uses_zero_based_roll(self):
        self.assertTrue(roll_success(30, randbelow=lambda bound: 29))
        self.assertFalse(roll_success(30, randbelow=lambda bound: 30))

    def test_rejects_out_of_range_and_non_integer_values(self):
        for value in (-1, 101):
            with self.assertRaises(ValueError):
                roll_success(value)
        with self.assertRaises(TypeError):
            roll_success(1.5)
        with self.assertRaises(TypeError):
            roll_success(True)


class FakeCursor:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.executions = []
        self.rowcount = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, sql, params=None):
        self.executions.append((sql, params))
        self.rowcount = 1

    async def fetchone(self):
        return self.responses.pop(0) if self.responses else None

    async def fetchall(self):
        return self.responses.pop(0) if self.responses else []


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.begin_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def begin(self):
        self.begin_count += 1

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1

    def cursor(self):
        return self._cursor


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return self.connection


def make_store(cursor=None):
    store = MySQLGameStore(
        host="db.example.com",
        port=3306,
        user="fapi",
        password="secret",
        database="astrbot",
    )
    cursor = cursor or FakeCursor()
    connection = FakeConnection(cursor)
    store._pool = FakePool(connection)
    return store, cursor, connection


class ReservationValidationTest(unittest.TestCase):
    def test_money_units_must_be_non_negative_integers(self):
        values = {
            "operation_key": "key",
            "game_type": "slots",
            "group_id": "group",
            "actor_qq_id": "actor",
            "actor_user_id": 1,
            "target_qq_id": None,
            "target_user_id": None,
            "debit_user_id": 1,
            "credit_user_id": None,
            "debit_units": 1,
            "credit_units": 0,
            "won": False,
            "cooldown_seconds": 0,
        }
        for invalid in (1.0, True, -1):
            with self.subTest(value=invalid):
                changed = dict(values, debit_units=invalid)
                with self.assertRaises((TypeError, ValueError)):
                    MySQLGameStore._validate_reservation(**changed)

    def test_credit_requires_recipient(self):
        values = {
            "operation_key": "key",
            "game_type": "slots",
            "group_id": "group",
            "actor_qq_id": "actor",
            "actor_user_id": 1,
            "target_qq_id": None,
            "target_user_id": None,
            "debit_user_id": 1,
            "credit_user_id": None,
            "debit_units": 1,
            "credit_units": 1,
            "won": True,
            "cooldown_seconds": 0,
        }
        with self.assertRaisesRegex(ValueError, "credit_user_id"):
            MySQLGameStore._validate_reservation(**values)


class GameSchemaTest(unittest.IsolatedAsyncioTestCase):
    def test_rejects_unsafe_table_name(self):
        store = MySQLGameStore(
            host="db.example.com",
            port=3306,
            user="fapi",
            password="secret",
            database="astrbot",
            table="games; DROP TABLE users",
        )
        with self.assertRaisesRegex(Exception, "table name"):
            store._validate_config()

    async def test_schema_uses_integer_units_and_binary_openids(self):
        rows = [
            {"max_length": 128, "collation_name": "utf8mb4_bin"},
            {"max_length": 128, "collation_name": "utf8mb4_bin"},
            {"max_length": 128, "collation_name": "utf8mb4_bin"},
        ]
        store, cursor, connection = make_store(FakeCursor(rows))

        await store._create_table()

        ddl = cursor.executions[0][0]
        self.assertIn("debit_units BIGINT UNSIGNED", ddl)
        self.assertIn("credit_units BIGINT UNSIGNED", ddl)
        self.assertIn("VARCHAR(128) CHARACTER SET utf8mb4", ddl)
        self.assertIn("COLLATE utf8mb4_bin", ddl)
        self.assertIn("UNIQUE KEY uq_fapi_game_operation_key", ddl)
        self.assertEqual(connection.commit_count, 1)

    async def test_legacy_platform_columns_are_widened(self):
        rows = [
            {"max_length": 20, "collation_name": "utf8mb4_unicode_ci"},
            {"max_length": 128, "collation_name": "utf8mb4_bin"},
            {"max_length": 128, "collation_name": "utf8mb4_bin"},
        ]
        cursor = FakeCursor(rows)
        store, _, _ = make_store(cursor)

        await store._ensure_platform_id_columns(cursor)

        alters = [sql for sql, _ in cursor.executions if "ALTER TABLE" in sql]
        self.assertEqual(len(alters), 1)
        self.assertIn("`group_id`", alters[0])
        self.assertIn("VARCHAR(128)", alters[0])
        self.assertIn("utf8mb4_bin", alters[0])


class ReserveGameTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def kwargs():
        return {
            "operation_key": "message-1:robbery",
            "game_type": "robbery",
            "group_id": "group-openid",
            "actor_qq_id": "actor-openid",
            "actor_user_id": 11,
            "target_qq_id": "target-openid",
            "target_user_id": 22,
            "debit_user_id": 11,
            "credit_user_id": 22,
            "debit_units": 1_000_000,
            "credit_units": 1_000_000,
            "won": True,
            "cooldown_seconds": 60,
        }

    async def test_existing_operation_key_is_returned_idempotently(self):
        cursor = FakeCursor([operation_row()])
        store, _, connection = make_store(cursor)

        result = await store.reserve_game(**self.kwargs())

        self.assertTrue(result.existing)
        self.assertFalse(result.created)
        self.assertEqual(result.operation.operation_id, "operation-1")
        self.assertEqual(connection.commit_count, 1)
        self.assertEqual(connection.rollback_count, 0)
        self.assertIn("FOR UPDATE", cursor.executions[0][0])

    async def test_cooldown_blocks_new_actor_type_operation(self):
        cursor = FakeCursor([None, {"retry_after_seconds": 42}])
        store, _, connection = make_store(cursor)

        with self.assertRaises(GameCooldownActive) as raised:
            await store.reserve_game(**self.kwargs())

        self.assertEqual(raised.exception.retry_after_seconds, 42)
        self.assertEqual(connection.rollback_count, 1)
        cooldown_sql, cooldown_params = cursor.executions[1]
        self.assertIn("group_id = %s", cooldown_sql)
        self.assertIn("game_type = %s", cooldown_sql)
        self.assertIn("actor_qq_id = %s", cooldown_sql)
        self.assertEqual(cooldown_params, (60, "group-openid", "robbery", "actor-openid", 60))

    async def test_new_operation_starts_at_pending_debit(self):
        row = operation_row()
        cursor = FakeCursor([None, None, row])
        store, _, connection = make_store(cursor)

        result = await store.reserve_game(**self.kwargs())

        self.assertFalse(result.existing)
        self.assertEqual(result.operation.status, GameStatus.PENDING_DEBIT)
        insert = next(item for item in cursor.executions if "INSERT INTO" in item[0])
        self.assertEqual(insert[1][13], "pending_debit")
        self.assertEqual(connection.commit_count, 1)


class StateTransitionTest(unittest.IsolatedAsyncioTestCase):
    async def test_mark_debited_selects_credit_or_success_in_sql(self):
        store, cursor, _ = make_store()

        await store.mark_debited("operation-1")

        sql, params = cursor.executions[0]
        self.assertIn("WHEN credit_units > 0 THEN 'pending_credit'", sql)
        self.assertIn("ELSE 'succeeded'", sql)
        self.assertIn("status = 'pending_debit'", sql)
        self.assertEqual(params, ("operation-1",))

    async def test_credit_failure_can_only_enter_refund_path(self):
        store, cursor, _ = make_store()

        await store.mark_pending_refund("operation-1")
        await store.mark_refunded("operation-1")

        first_sql, first_params = cursor.executions[0]
        second_sql, second_params = cursor.executions[1]
        self.assertIn("status IN (%s)", first_sql)
        self.assertEqual(
            first_params,
            ("pending_refund", "operation-1", "pending_credit"),
        )
        self.assertEqual(
            second_params,
            ("refunded", "operation-1", "pending_refund"),
        )

    async def test_record_error_increments_attempt_and_uses_capped_backoff(self):
        store, cursor, _ = make_store()

        await store.record_error(
            "operation-1",
            "network error",
            base_delay_seconds=3,
            max_delay_seconds=90,
        )

        sql, params = cursor.executions[0]
        self.assertIn("attempt_count = attempt_count + 1", sql)
        self.assertIn("POW(2", sql)
        self.assertIn("LEAST(%s", sql)
        self.assertEqual(params[:4], ("network error", 90, 3, "operation-1"))
        self.assertEqual(params[4:], (
            "pending_debit",
            "pending_credit",
            "pending_refund",
        ))

    async def test_list_pending_only_returns_due_transient_states(self):
        rows = [operation_row(status="pending_credit")]
        store, cursor, _ = make_store(FakeCursor([rows]))

        operations = await store.list_pending(limit=999)

        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0].status, GameStatus.PENDING_CREDIT)
        sql, params = cursor.executions[0]
        self.assertIn("next_attempt_at <= UTC_TIMESTAMP(6)", sql)
        self.assertIn("LIMIT 100", sql)
        self.assertEqual(params, (
            "pending_debit",
            "pending_credit",
            "pending_refund",
        ))


if __name__ == "__main__":
    unittest.main()
