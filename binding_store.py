from __future__ import annotations

import asyncio
import re
import ssl
from dataclasses import dataclass
from enum import Enum
from typing import Any

try:
    import aiomysql
except ImportError:  # pragma: no cover - AstrBot 安装依赖后不会走此分支
    aiomysql = None


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_PLATFORM_ID_MAX_LENGTH = 128


class BindingDatabaseError(RuntimeError):
    pass


class BindCooldownActive(BindingDatabaseError):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        super().__init__(f"binding cooldown active for {self.retry_after_seconds} seconds")


class BindStatus(str, Enum):
    CREATED = "created"
    EXISTING = "existing"
    QQ_CONFLICT = "qq_conflict"
    USER_CONFLICT = "user_conflict"


@dataclass(frozen=True, slots=True)
class BindingRecord:
    qq_id: str
    tokenrouter_user_id: int
    email: str
    blocked: bool = False


@dataclass(frozen=True, slots=True)
class BindResult:
    status: BindStatus
    binding: BindingRecord | None = None


def classify_existing_bindings(
    rows: list[dict[str, Any]],
    *,
    qq_id: str,
    tokenrouter_user_id: int,
) -> BindResult | None:
    for row in rows:
        if str(row.get("qq_id")) == qq_id:
            binding = _row_to_binding(row)
            if binding.tokenrouter_user_id == tokenrouter_user_id:
                return BindResult(BindStatus.EXISTING, binding)
            return BindResult(BindStatus.QQ_CONFLICT, binding)

    for row in rows:
        if int(row.get("tokenrouter_user_id", 0)) == tokenrouter_user_id:
            return BindResult(BindStatus.USER_CONFLICT, _row_to_binding(row))
    return None


class MySQLBindingStore:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        table: str = "astrbot_fapi_bindings",
        cooldown_table: str = "astrbot_fapi_binding_cooldowns",
        connect_timeout: int = 10,
        ssl_enabled: bool = True,
        ssl_verify: bool = True,
    ) -> None:
        self.host = host.strip()
        self.port = port
        self.user = user.strip()
        self.password = password
        self.database = database.strip()
        self.table = table.strip()
        self.cooldown_table = cooldown_table.strip()
        self.connect_timeout = max(1, connect_timeout)
        self.ssl_enabled = ssl_enabled
        self.ssl_verify = ssl_verify
        self._pool: Any = None
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        async with self._initialize_lock:
            if self._pool is not None:
                return
            self._validate_config()
            if aiomysql is None:
                raise BindingDatabaseError("缺少 aiomysql 依赖")

            try:
                self._pool = await aiomysql.create_pool(
                    host=self.host,
                    port=self.port,
                    user=self.user,
                    password=self.password,
                    db=self.database,
                    charset="utf8mb4",
                    autocommit=True,
                    minsize=1,
                    maxsize=5,
                    connect_timeout=self.connect_timeout,
                    pool_recycle=1800,
                    cursorclass=aiomysql.DictCursor,
                    ssl=self._ssl_context(),
                    init_command="SET time_zone = '+00:00'",
                )
                await self._create_table()
            except Exception as exc:
                if self._pool is not None:
                    self._pool.close()
                    await self._pool.wait_closed()
                    self._pool = None
                raise BindingDatabaseError("无法连接或初始化远程 MySQL") from exc

    async def close(self) -> None:
        if self._pool is None:
            return
        self._pool.close()
        await self._pool.wait_closed()
        self._pool = None

    async def bind(
        self,
        *,
        qq_id: str,
        tokenrouter_user_id: int,
        email: str,
    ) -> BindResult:
        await self.initialize()
        try:
            return await self._bind_once(
                qq_id=qq_id,
                tokenrouter_user_id=tokenrouter_user_id,
                email=email,
            )
        except BindCooldownActive:
            raise
        except Exception as exc:
            if not _is_duplicate_key_error(exc):
                raise BindingDatabaseError("写入远程 MySQL 失败") from exc

        # 并发绑定由唯一索引裁决；发生竞争后重新读取最终状态。
        try:
            rows = await self._find_conflicts(qq_id, tokenrouter_user_id)
        except Exception as exc:
            raise BindingDatabaseError("读取远程 MySQL 失败") from exc
        result = classify_existing_bindings(
            rows,
            qq_id=qq_id,
            tokenrouter_user_id=tokenrouter_user_id,
        )
        if result is None:
            raise BindingDatabaseError("绑定写入冲突，但未找到冲突记录")
        return result

    async def get_by_qq(self, qq_id: str) -> BindingRecord | None:
        await self.initialize()
        sql = (
            f"SELECT qq_id, tokenrouter_user_id, email, blocked_at FROM `{self.table}` "
            "WHERE qq_id = %s AND blocked_at IS NULL LIMIT 1"
        )
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql, (qq_id,))
                    row = await cursor.fetchone()
        except Exception as exc:
            raise BindingDatabaseError("读取远程 MySQL 失败") from exc
        return _row_to_binding(row) if row else None

    async def get_by_qq_for_admin(self, qq_id: str) -> BindingRecord | None:
        await self.initialize()
        sql = (
            f"SELECT qq_id, tokenrouter_user_id, email, blocked_at FROM `{self.table}` "
            "WHERE qq_id = %s LIMIT 1"
        )
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql, (qq_id,))
                    row = await cursor.fetchone()
        except Exception as exc:
            raise BindingDatabaseError("查询管理员绑定关系失败") from exc
        return _row_to_binding(row) if row else None

    async def set_user_blocked(self, tokenrouter_user_id: int, blocked: bool) -> bool:
        await self.initialize()
        sql = (
            f"UPDATE `{self.table}` SET blocked_at = "
            "CASE WHEN %s THEN UTC_TIMESTAMP(6) ELSE NULL END "
            "WHERE tokenrouter_user_id = %s"
        )
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql, (int(blocked), tokenrouter_user_id))
                    return cursor.rowcount == 1
        except Exception as exc:
            raise BindingDatabaseError("更新用户封禁状态失败") from exc

    async def list_blocked(self, limit: int = 20) -> list[BindingRecord]:
        await self.initialize()
        safe_limit = max(1, min(int(limit), 50))
        sql = f"""
            SELECT qq_id, tokenrouter_user_id, email, blocked_at
            FROM `{self.table}` WHERE blocked_at IS NOT NULL
            ORDER BY blocked_at DESC LIMIT {safe_limit}
        """
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql)
                    rows = await cursor.fetchall()
        except Exception as exc:
            raise BindingDatabaseError("查询封禁用户失败") from exc
        return [_row_to_binding(row) for row in rows]

    async def get_by_user_ids(
        self,
        user_ids: list[int] | tuple[int, ...] | set[int],
    ) -> dict[int, BindingRecord]:
        await self.initialize()
        normalized = sorted({int(user_id) for user_id in user_ids if int(user_id) > 0})
        if not normalized:
            return {}
        placeholders = ", ".join("%s" for _ in normalized)
        sql = (
            f"SELECT qq_id, tokenrouter_user_id, email, blocked_at FROM `{self.table}` "
            f"WHERE tokenrouter_user_id IN ({placeholders})"
        )
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql, tuple(normalized))
                    rows = await cursor.fetchall()
        except Exception as exc:
            raise BindingDatabaseError("读取远程 MySQL 失败") from exc
        records = (_row_to_binding(row) for row in rows)
        return {record.tokenrouter_user_id: record for record in records}

    async def _bind_once(
        self,
        *,
        qq_id: str,
        tokenrouter_user_id: int,
        email: str,
    ) -> BindResult:
        async with self._pool.acquire() as connection:
            await connection.begin()
            try:
                rows = await self._find_conflicts(
                    qq_id,
                    tokenrouter_user_id,
                    connection=connection,
                    for_update=True,
                )
                existing = classify_existing_bindings(
                    rows,
                    qq_id=qq_id,
                    tokenrouter_user_id=tokenrouter_user_id,
                )
                if existing is not None:
                    await connection.commit()
                    return existing

                async with connection.cursor() as cursor:
                    await cursor.execute(
                        f"SELECT CEIL(GREATEST(0, TIMESTAMPDIFF(MICROSECOND, "
                        "UTC_TIMESTAMP(6), next_bind_at)) / 1000000) AS retry_after "
                        f"FROM `{self.cooldown_table}` WHERE qq_id = %s FOR UPDATE",
                        (qq_id,),
                    )
                    cooldown = await cursor.fetchone()
                if cooldown and int(cooldown.get("retry_after") or 0) > 0:
                    raise BindCooldownActive(int(cooldown["retry_after"]))

                sql = (
                    f"INSERT INTO `{self.table}` "
                    "(qq_id, tokenrouter_user_id, email) VALUES (%s, %s, %s)"
                )
                async with connection.cursor() as cursor:
                    await cursor.execute(sql, (qq_id, tokenrouter_user_id, email))
                await connection.commit()
                return BindResult(
                    BindStatus.CREATED,
                    BindingRecord(qq_id, tokenrouter_user_id, email),
                )
            except Exception:
                await connection.rollback()
                raise

    async def _find_conflicts(
        self,
        qq_id: str,
        tokenrouter_user_id: int,
        *,
        connection: Any = None,
        for_update: bool = False,
    ) -> list[dict[str, Any]]:
        sql = (
            f"SELECT qq_id, tokenrouter_user_id, email FROM `{self.table}` "
            "WHERE qq_id = %s OR tokenrouter_user_id = %s"
        )
        if for_update:
            sql += " FOR UPDATE"

        if connection is not None:
            async with connection.cursor() as cursor:
                await cursor.execute(sql, (qq_id, tokenrouter_user_id))
                return list(await cursor.fetchall())

        async with self._pool.acquire() as acquired:
            async with acquired.cursor() as cursor:
                await cursor.execute(sql, (qq_id, tokenrouter_user_id))
                return list(await cursor.fetchall())

    async def _create_table(self) -> None:
        sql = f"""
            CREATE TABLE IF NOT EXISTS `{self.table}` (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                qq_id VARCHAR(128) CHARACTER SET utf8mb4
                    COLLATE utf8mb4_bin NOT NULL,
                tokenrouter_user_id BIGINT UNSIGNED NOT NULL,
                email VARCHAR(254) NOT NULL,
                blocked_at DATETIME(6) NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uq_fapi_qq_id (qq_id),
                UNIQUE KEY uq_fapi_tokenrouter_user_id (tokenrouter_user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
        cooldown_sql = f"""
            CREATE TABLE IF NOT EXISTS `{self.cooldown_table}` (
                qq_id VARCHAR(128) CHARACTER SET utf8mb4
                    COLLATE utf8mb4_bin NOT NULL,
                next_bind_at DATETIME(6) NOT NULL,
                updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ON UPDATE CURRENT_TIMESTAMP(6),
                PRIMARY KEY (qq_id),
                KEY idx_fapi_binding_cooldown (next_bind_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
        async with self._pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(sql)
                await self._ensure_platform_id_column(cursor)
                await self._ensure_blocked_column(cursor)
                await cursor.execute(cooldown_sql)
            await connection.commit()

    async def _ensure_platform_id_column(self, cursor: Any) -> None:
        await cursor.execute(
            """
            SELECT CHARACTER_MAXIMUM_LENGTH AS max_length,
                   COLLATION_NAME AS collation_name
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
              AND COLUMN_NAME = 'qq_id'
            LIMIT 1
            """,
            (self.table,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise BindingDatabaseError("绑定表缺少 qq_id 字段")
        if (
            int(row["max_length"]) >= _PLATFORM_ID_MAX_LENGTH
            and str(row["collation_name"]).casefold() == "utf8mb4_bin"
        ):
            return
        await cursor.execute(
            f"ALTER TABLE `{self.table}` MODIFY COLUMN `qq_id` "
            "VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL"
        )

    async def _ensure_blocked_column(self, cursor: Any) -> None:
        await cursor.execute(
            """
            SELECT 1 FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
              AND COLUMN_NAME = 'blocked_at' LIMIT 1
            """,
            (self.table,),
        )
        if await cursor.fetchone() is None:
            await cursor.execute(
                f"ALTER TABLE `{self.table}` ADD COLUMN blocked_at DATETIME(6) NULL"
            )

    def _validate_config(self) -> None:
        if not self.host or not self.user or not self.database:
            raise BindingDatabaseError("远程 MySQL 配置不完整")
        if not 1 <= self.port <= 65535:
            raise BindingDatabaseError("MySQL 端口不正确")
        if not _IDENTIFIER_PATTERN.fullmatch(self.table):
            raise BindingDatabaseError("MySQL 表名不合法")
        if not _IDENTIFIER_PATTERN.fullmatch(self.cooldown_table):
            raise BindingDatabaseError("MySQL 解绑冷却表名不合法")
        if self.table.casefold() == self.cooldown_table.casefold():
            raise BindingDatabaseError("绑定表和解绑冷却表不能同名")

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self.ssl_enabled:
            return None
        context = ssl.create_default_context()
        if not self.ssl_verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context


def _row_to_binding(row: dict[str, Any]) -> BindingRecord:
    return BindingRecord(
        qq_id=str(row["qq_id"]),
        tokenrouter_user_id=int(row["tokenrouter_user_id"]),
        email=str(row["email"]),
        blocked=row.get("blocked_at") is not None,
    )


def _is_duplicate_key_error(exc: Exception) -> bool:
    return bool(exc.args and exc.args[0] == 1062)
