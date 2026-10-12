import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import binding_store
from binding_store import (
    BindCooldownActive,
    BindStatus,
    MySQLBindingStore,
    classify_existing_bindings,
)


class ClassifyExistingBindingsTest(unittest.TestCase):
    def test_same_binding_is_idempotent(self):
        result = classify_existing_bindings(
            [{"qq_id": "12345", "tokenrouter_user_id": 7, "email": "12345@qq.com"}],
            qq_id="12345",
            tokenrouter_user_id=7,
        )
        self.assertEqual(result.status, BindStatus.EXISTING)

    def test_qq_cannot_be_rebound_to_another_user(self):
        result = classify_existing_bindings(
            [{"qq_id": "12345", "tokenrouter_user_id": 7, "email": "12345@qq.com"}],
            qq_id="12345",
            tokenrouter_user_id=8,
        )
        self.assertEqual(result.status, BindStatus.QQ_CONFLICT)

    def test_user_cannot_be_bound_to_another_qq(self):
        result = classify_existing_bindings(
            [{"qq_id": "54321", "tokenrouter_user_id": 7, "email": "12345@qq.com"}],
            qq_id="12345",
            tokenrouter_user_id=7,
        )
        self.assertEqual(result.status, BindStatus.USER_CONFLICT)

    def test_no_conflict_returns_none(self):
        self.assertIsNone(
            classify_existing_bindings([], qq_id="12345", tokenrouter_user_id=7)
        )


class MySQLConfigTest(unittest.TestCase):
    def test_rejects_unsafe_table_name(self):
        store = MySQLBindingStore(
            host="db.example.com",
            port=3306,
            user="fapi",
            password="secret",
            database="astrbot",
            table="bindings; DROP TABLE users",
        )
        with self.assertRaisesRegex(Exception, "表名不合法"):
            store._validate_config()


class MySQLInitializationTest(unittest.IsolatedAsyncioTestCase):
    async def test_connection_pool_uses_utc_session_timezone(self):
        store = MySQLBindingStore(
            host="db.example.com",
            port=3306,
            user="fapi",
            password="secret",
            database="astrbot",
            ssl_enabled=False,
        )
        pool = AsyncMock()
        store._create_table = AsyncMock()
        create_pool = AsyncMock(return_value=pool)

        with patch.object(
            binding_store,
            "aiomysql",
            SimpleNamespace(create_pool=create_pool, DictCursor=object()),
        ):
            await store.initialize()

        self.assertEqual(
            create_pool.await_args.kwargs["init_command"],
            "SET time_zone = '+00:00'",
        )


class PlatformIdMigrationTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def store():
        return MySQLBindingStore(
            host="db.example.com",
            port=3306,
            user="fapi",
            password="secret",
            database="astrbot",
        )

    async def test_widens_and_binary_collates_legacy_qq_id(self):
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
        await self.store()._ensure_platform_id_column(cursor)

        alter_sql = [sql for sql, _params in cursor.executions if "ALTER TABLE" in sql]
        self.assertEqual(len(alter_sql), 1)
        self.assertIn("VARCHAR(128)", alter_sql[0])
        self.assertIn("utf8mb4_bin", alter_sql[0])

    async def test_skips_alter_when_identity_column_is_current(self):
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
        await self.store()._ensure_platform_id_column(cursor)

        self.assertFalse(any("ALTER TABLE" in sql for sql, _ in cursor.executions))


class BindingCooldownTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def store(cursor):
        class Connection:
            def __init__(self):
                self.commits = 0
                self.rollbacks = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def cursor(self):
                return cursor

            async def begin(self):
                pass

            async def commit(self):
                self.commits += 1

            async def rollback(self):
                self.rollbacks += 1

        class Pool:
            def __init__(self, connection):
                self.connection = connection

            def acquire(self):
                return self.connection

        store = MySQLBindingStore(
            host="db.example.com",
            port=3306,
            user="fapi",
            password="secret",
            database="astrbot",
        )
        connection = Connection()
        store._pool = Pool(connection)
        return store, connection

    async def test_bind_rejects_active_cooldown_before_insert(self):
        class Cursor:
            def __init__(self):
                self.executions = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def execute(self, sql, params=None):
                self.executions.append((sql, params))

            async def fetchall(self):
                return []

            async def fetchone(self):
                return {"retry_after": 1}

        cursor = Cursor()
        store, connection = self.store(cursor)

        with self.assertRaises(BindCooldownActive) as raised:
            await store.bind(
                qq_id="member-openid",
                tokenrouter_user_id=7,
                email="alice@example.com",
            )

        self.assertEqual(raised.exception.retry_after_seconds, 1)
        self.assertEqual(connection.rollbacks, 1)
        cooldown_sql = cursor.executions[1][0]
        self.assertIn("TIMESTAMPDIFF(MICROSECOND", cooldown_sql)
        self.assertFalse(any("INSERT INTO `astrbot_fapi_bindings`" in sql for sql, _ in cursor.executions))

    async def test_unbind_deletes_expected_binding_and_persists_cooldown(self):
        class Cursor:
            rowcount = 1

            def __init__(self):
                self.executions = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def execute(self, sql, params=None):
                self.executions.append((sql, params))

            async def fetchone(self):
                return {"tokenrouter_user_id": 7}

        cursor = Cursor()
        store, connection = self.store(cursor)

        removed = await store.unbind(
            qq_id="member-openid",
            expected_user_id=7,
            cooldown_seconds=3600,
        )

        self.assertTrue(removed)
        self.assertEqual(connection.commits, 1)
        self.assertIn("DELETE FROM", cursor.executions[1][0])
        self.assertIn(store.cooldown_table, cursor.executions[2][0])
        self.assertEqual(cursor.executions[2][1], ("member-openid", 3600))


if __name__ == "__main__":
    unittest.main()
