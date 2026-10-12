import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from game_store import (
    GameCooldownActive,
    GameOperationConflict,
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
        "refund_attempt_count": 0,
        "last_error": None,
        "next_attempt_at": NOW,
        "created_at": NOW,
        "lease_token": None,
        "lease_expires_at": None,
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
    def __init__(self, responses=None, rowcounts=None):
        self.responses = list(responses or [])
        self.rowcounts = list(rowcounts or [])
        self.executions = []
        self.rowcount = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, sql, params=None):
        self.executions.append((sql, params))
        self.rowcount = self.rowcounts.pop(0) if self.rowcounts else 1

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
            "game_type": "robbery",
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
            "game_type": "robbery",
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
        self.assertIn("lease_token CHAR(36)", ddl)
        self.assertIn("lease_expires_at DATETIME(6)", ddl)
        self.assertIn("refund_attempt_count INT UNSIGNED", ddl)
        cooldown_ddl = next(
            sql for sql, _ in cursor.executions
            if f"CREATE TABLE IF NOT EXISTS `{store.cooldown_table}`" in sql
        )
        self.assertIn("PRIMARY KEY (group_id, game_type, actor_qq_id)", cooldown_ddl)
        self.assertEqual(connection.commit_count, 1)

    def test_long_operation_table_gets_stable_valid_cooldown_table(self):
        table = "a" * 64
        first = MySQLGameStore._default_cooldown_table(table)
        second = MySQLGameStore._default_cooldown_table(table)

        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 64)
        self.assertRegex(first, r"^[A-Za-z][A-Za-z0-9_]+$")

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

    async def test_initialize_sets_every_mysql_session_to_utc(self):
        store = MySQLGameStore(
            host="db.example.com",
            port=3306,
            user="fapi",
            password="secret",
            database="astrbot",
        )
        pool = SimpleNamespace()
        create_pool = AsyncMock(return_value=pool)
        fake_aiomysql = SimpleNamespace(
            DictCursor=object(),
            create_pool=create_pool,
        )
        with (
            patch("game_store.aiomysql", fake_aiomysql),
            patch.object(store, "_create_table", AsyncMock()),
        ):
            await store.initialize()

        self.assertEqual(
            create_pool.await_args.kwargs["init_command"],
            "SET time_zone = '+00:00'",
        )

    async def test_missing_settlement_columns_are_added_for_legacy_table(self):
        store, cursor, _ = make_store(FakeCursor([[]]))

        await store._ensure_settlement_columns(cursor)

        alters = [sql for sql, _ in cursor.executions if "ALTER TABLE" in sql]
        self.assertEqual(len(alters), 3)
        self.assertIn("refund_attempt_count", alters[0])
        self.assertIn("lease_token", alters[1])
        self.assertIn("lease_expires_at", alters[2])


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

    async def test_existing_operation_keeps_first_random_financial_outcome(self):
        persisted = operation_row(
            debit_user_id=22,
            credit_user_id=11,
            debit_units=10_000_000,
            credit_units=10_000_000,
            won=True,
        )
        cursor = FakeCursor([persisted])
        store, _, _ = make_store(cursor)
        competing_values = self.kwargs()
        competing_values.update(
            debit_user_id=11,
            credit_user_id=22,
            debit_units=5_000_000,
            credit_units=5_000_000,
            won=False,
        )

        result = await store.reserve_game(**competing_values)

        self.assertTrue(result.existing)
        self.assertTrue(result.operation.won)
        self.assertEqual(result.operation.debit_user_id, 22)
        self.assertEqual(result.operation.credit_user_id, 11)

    async def test_existing_operation_still_rejects_changed_target_identity(self):
        cursor = FakeCursor([operation_row(target_user_id=999)])
        store, _, connection = make_store(cursor)

        with self.assertRaises(GameOperationConflict):
            await store.reserve_game(**self.kwargs())

        self.assertEqual(connection.rollback_count, 1)

    async def test_cooldown_blocks_new_actor_type_operation(self):
        cursor = FakeCursor([None, {"retry_after_seconds": 42}])
        store, _, connection = make_store(cursor)

        with self.assertRaises(GameCooldownActive) as raised:
            await store.reserve_game(**self.kwargs())

        self.assertEqual(raised.exception.retry_after_seconds, 42)
        self.assertEqual(connection.rollback_count, 1)
        seed_sql, seed_params = cursor.executions[1]
        cooldown_sql, cooldown_params = cursor.executions[2]
        self.assertIn(f"INSERT INTO `{store.cooldown_table}`", seed_sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", seed_sql)
        self.assertEqual(
            seed_params,
            (
                "group-openid", "robbery", "actor-openid",
                "group-openid", "robbery", "actor-openid", 61,
            ),
        )
        self.assertIn("group_id = %s", cooldown_sql)
        self.assertIn("game_type = %s", cooldown_sql)
        self.assertIn("actor_qq_id = %s", cooldown_sql)
        self.assertEqual(cooldown_params, (60, "group-openid", "robbery", "actor-openid", 60))

    async def test_concurrent_redelivery_wins_over_cooldown_result(self):
        cursor = FakeCursor(
            [None, {"retry_after_seconds": 60}, operation_row()]
        )
        store, _, connection = make_store(cursor)

        result = await store.reserve_game(**self.kwargs())

        self.assertTrue(result.existing)
        self.assertEqual(result.operation.operation_id, "operation-1")
        self.assertEqual(connection.rollback_count, 1)

    async def test_new_operation_starts_at_pending_debit(self):
        row = operation_row()
        cursor = FakeCursor([None, None, row])
        store, _, connection = make_store(cursor)

        result = await store.reserve_game(**self.kwargs())

        self.assertFalse(result.existing)
        self.assertEqual(result.operation.status, GameStatus.PENDING_DEBIT)
        insert = next(
            item for item in cursor.executions
            if "operation_id, operation_key" in item[0]
        )
        self.assertEqual(insert[1][13], "pending_debit")
        self.assertEqual(connection.commit_count, 1)


class StateTransitionTest(unittest.IsolatedAsyncioTestCase):
    async def test_claim_returns_database_lease_token(self):
        token = "11111111-1111-1111-1111-111111111111"
        row = operation_row(lease_token=token, lease_expires_at=NOW)
        store, cursor, _ = make_store(FakeCursor([row]))

        with patch("game_store.uuid.uuid4", return_value=token):
            operation = await store.claim("operation-1", lease_seconds=90)

        self.assertIsNotNone(operation)
        self.assertEqual(operation.lease_token, token)
        claim_sql, claim_params = cursor.executions[0]
        self.assertIn("lease_token IS NULL OR lease_expires_at", claim_sql)
        self.assertEqual(claim_params[:3], (token, 90, "operation-1"))

    async def test_claim_returns_none_when_another_worker_holds_lease(self):
        store, _, _ = make_store(FakeCursor(rowcounts=[0]))

        operation = await store.claim("operation-1")

        self.assertIsNone(operation)

    async def test_stale_claim_cannot_advance_state(self):
        store, _, _ = make_store(FakeCursor(rowcounts=[0]))

        with self.assertRaises(GameOperationConflict):
            await store.mark_debited(
                "operation-1",
                claim_token="stale-token",
            )

    async def test_claimed_transition_requires_live_token_and_renews_lease(self):
        store, cursor, _ = make_store()

        await store.mark_pending_refund(
            "operation-1",
            claim_token="live-token",
            lease_seconds=75,
        )

        sql, params = cursor.executions[0]
        self.assertIn("lease_expires_at > UTC_TIMESTAMP(6)", sql)
        self.assertIn("TIMESTAMPADD(SECOND, %s", sql)
        self.assertEqual(
            params,
            ("pending_refund", 75, "operation-1", "pending_credit", "live-token"),
        )

    async def test_refund_reaches_manual_review_at_attempt_limit(self):
        row = operation_row(
            status="manual_review",
            attempt_count=12,
            refund_attempt_count=3,
            last_error="recipient missing",
        )
        store, cursor, _ = make_store(FakeCursor([row]))

        status = await store.record_refund_error(
            "operation-1",
            "recipient missing",
            max_attempts=3,
            claim_token="live-token",
        )

        self.assertEqual(status, GameStatus.MANUAL_REVIEW)
        sql, params = cursor.executions[0]
        self.assertIn("refund_attempt_count + 1 >= %s", sql)
        self.assertIn("attempt_count = attempt_count + 1", sql)
        self.assertIn("refund_attempt_count = refund_attempt_count + 1", sql)
        self.assertIn("'manual_review'", sql)
        self.assertIn("lease_token = NULL", sql)
        self.assertEqual(params[-2:], ("operation-1", "live-token"))

    async def test_prior_phase_errors_do_not_consume_refund_attempts(self):
        row = operation_row(
            status="pending_refund",
            attempt_count=100,
            refund_attempt_count=1,
            last_error="recipient missing",
        )
        store, _, _ = make_store(FakeCursor([row]))

        status = await store.record_refund_error(
            "operation-1",
            "recipient missing",
            max_attempts=5,
            claim_token="live-token",
        )

        self.assertEqual(status, GameStatus.PENDING_REFUND)

    async def test_manual_review_can_be_explicitly_requeued(self):
        store, cursor, _ = make_store()

        await store.retry_manual_review("operation-1")

        sql, params = cursor.executions[0]
        self.assertIn("status = 'pending_refund'", sql)
        self.assertIn("status = 'manual_review'", sql)
        self.assertIn("lease_token = NULL", sql)
        self.assertIn("refund_attempt_count = 0", sql)
        self.assertEqual(params, ("operation-1",))

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
        self.assertIn("lease_token IS NULL OR lease_expires_at", sql)
        self.assertIn("LIMIT 100", sql)
        self.assertEqual(params, (
            "pending_debit",
            "pending_credit",
            "pending_refund",
        ))


if __name__ == "__main__":
    unittest.main()
