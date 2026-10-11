from __future__ import annotations

import asyncio
import re
import secrets
import ssl
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Iterable

try:
    import aiomysql
except ImportError:  # pragma: no cover - installed by AstrBot in production
    aiomysql = None


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_PLATFORM_ID_MAX_LENGTH = 128
_PENDING_STATUSES = (
    "pending_debit",
    "pending_credit",
    "pending_refund",
)


class GameDatabaseError(RuntimeError):
    pass


class GameOperationConflict(GameDatabaseError):
    pass


class GameCooldownActive(GameDatabaseError):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        super().__init__(f"game cooldown active for {self.retry_after_seconds} seconds")


class GameStatus(str, Enum):
    PENDING_DEBIT = "pending_debit"
    PENDING_CREDIT = "pending_credit"
    PENDING_REFUND = "pending_refund"
    SUCCEEDED = "succeeded"
    INSUFFICIENT = "insufficient"
    FAILED = "failed"
    REFUNDED = "refunded"


@dataclass(frozen=True, slots=True)
class GameOperation:
    operation_id: str
    operation_key: str
    game_type: str
    group_id: str
    actor_qq_id: str
    actor_user_id: int
    target_qq_id: str | None
    target_user_id: int | None
    debit_user_id: int
    credit_user_id: int | None
    debit_units: int
    credit_units: int
    won: bool
    status: GameStatus
    attempt_count: int
    last_error: str | None
    next_attempt_at: datetime
    created_at: datetime

    @property
    def type(self) -> str:
        return self.game_type


@dataclass(frozen=True, slots=True)
class GameReservation:
    operation: GameOperation
    existing: bool

    @property
    def created(self) -> bool:
        return not self.existing


def roll_success(
    percent: int,
    *,
    randbelow: Callable[[int], int] = secrets.randbelow,
) -> bool:
    """Return a cryptographically random percentage roll with exact boundaries."""
    if isinstance(percent, bool) or not isinstance(percent, int):
        raise TypeError("percent must be an integer")
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    if percent == 0:
        return False
    if percent == 100:
        return True
    return randbelow(100) < percent


class MySQLGameStore:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        table: str = "astrbot_fapi_game_operations",
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
                raise GameDatabaseError("missing aiomysql dependency")
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
                )
                await self._create_table()
            except Exception as exc:
                if self._pool is not None:
                    self._pool.close()
                    await self._pool.wait_closed()
                    self._pool = None
                if isinstance(exc, GameDatabaseError):
                    raise
                raise GameDatabaseError("unable to initialize game database") from exc

    async def close(self) -> None:
        if self._pool is None:
            return
        self._pool.close()
        await self._pool.wait_closed()
        self._pool = None

    async def reserve_game(
        self,
        *,
        operation_key: str,
        game_type: str,
        group_id: str,
        actor_qq_id: str,
        actor_user_id: int,
        target_qq_id: str | None = None,
        target_user_id: int | None = None,
        debit_user_id: int,
        credit_user_id: int | None = None,
        debit_units: int,
        credit_units: int,
        won: bool,
        cooldown_seconds: int = 0,
    ) -> GameReservation:
        self._validate_reservation(
            operation_key=operation_key,
            game_type=game_type,
            group_id=group_id,
            actor_qq_id=actor_qq_id,
            actor_user_id=actor_user_id,
            target_qq_id=target_qq_id,
            target_user_id=target_user_id,
            debit_user_id=debit_user_id,
            credit_user_id=credit_user_id,
            debit_units=debit_units,
            credit_units=credit_units,
            won=won,
            cooldown_seconds=cooldown_seconds,
        )
        await self.initialize()
        values = {
            "operation_key": operation_key,
            "game_type": game_type,
            "group_id": group_id,
            "actor_qq_id": actor_qq_id,
            "actor_user_id": actor_user_id,
            "target_qq_id": target_qq_id,
            "target_user_id": target_user_id,
            "debit_user_id": debit_user_id,
            "credit_user_id": credit_user_id,
            "debit_units": debit_units,
            "credit_units": credit_units,
            "won": won,
        }
        try:
            return await self._reserve_once(values, cooldown_seconds)
        except GameCooldownActive:
            raise
        except GameOperationConflict:
            raise
        except Exception as exc:
            if not _is_duplicate_key_error(exc):
                raise GameDatabaseError("unable to reserve game operation") from exc

        # A concurrent delivery won the unique operation_key race. Read its result.
        existing = await self.get_by_key(operation_key)
        if existing is None:
            raise GameDatabaseError("operation key conflicted but no operation was found")
        self._assert_same_operation(existing, values)
        return GameReservation(existing, existing=True)

    async def get(self, operation_id: str) -> GameOperation | None:
        await self.initialize()
        return await self._get_one("operation_id", operation_id)

    async def get_by_key(self, operation_key: str) -> GameOperation | None:
        await self.initialize()
        return await self._get_one("operation_key", operation_key)

    async def mark_debited(self, operation_id: str) -> None:
        sql = f"""
            UPDATE `{self.table}`
            SET status = CASE
                    WHEN credit_units > 0 THEN 'pending_credit'
                    ELSE 'succeeded'
                END,
                last_error = NULL,
                next_attempt_at = UTC_TIMESTAMP(6),
                completed_at = CASE
                    WHEN credit_units = 0 THEN UTC_TIMESTAMP(6)
                    ELSE NULL
                END,
                updated_at = UTC_TIMESTAMP(6)
            WHERE operation_id = %s AND status = 'pending_debit'
        """
        await self._execute_update(sql, (operation_id,), "mark game debit")

    async def mark_succeeded(self, operation_id: str) -> None:
        await self._transition(
            operation_id,
            GameStatus.SUCCEEDED,
            (GameStatus.PENDING_CREDIT,),
            completed=True,
        )

    async def mark_insufficient(self, operation_id: str) -> None:
        await self._transition(
            operation_id,
            GameStatus.INSUFFICIENT,
            (GameStatus.PENDING_DEBIT,),
            completed=True,
        )

    async def mark_failed(self, operation_id: str) -> None:
        # A debited operation must use pending_refund, never failed.
        await self._transition(
            operation_id,
            GameStatus.FAILED,
            (GameStatus.PENDING_DEBIT,),
            completed=True,
        )

    async def mark_pending_refund(self, operation_id: str) -> None:
        await self._transition(
            operation_id,
            GameStatus.PENDING_REFUND,
            (GameStatus.PENDING_CREDIT,),
            completed=False,
        )

    async def mark_refunded(self, operation_id: str) -> None:
        await self._transition(
            operation_id,
            GameStatus.REFUNDED,
            (GameStatus.PENDING_REFUND,),
            completed=True,
        )

    async def record_error(
        self,
        operation_id: str,
        error: str,
        *,
        base_delay_seconds: int = 5,
        max_delay_seconds: int = 3600,
    ) -> None:
        base_delay = max(1, int(base_delay_seconds))
        max_delay = max(base_delay, int(max_delay_seconds))
        placeholders = ", ".join("%s" for _ in _PENDING_STATUSES)
        sql = f"""
            UPDATE `{self.table}`
            SET attempt_count = attempt_count + 1,
                last_error = %s,
                next_attempt_at = TIMESTAMPADD(
                    SECOND,
                    LEAST(%s, %s * POW(2, LEAST(attempt_count, 20))),
                    UTC_TIMESTAMP(6)
                ),
                updated_at = UTC_TIMESTAMP(6)
            WHERE operation_id = %s AND status IN ({placeholders})
        """
        await self._execute_update(
            sql,
            (
                str(error)[:1000],
                max_delay,
                base_delay,
                operation_id,
                *_PENDING_STATUSES,
            ),
            "record game error",
        )

    async def list_pending(self, limit: int = 20) -> list[GameOperation]:
        await self.initialize()
        safe_limit = max(1, min(int(limit), 100))
        placeholders = ", ".join("%s" for _ in _PENDING_STATUSES)
        sql = f"""
            SELECT * FROM `{self.table}`
            WHERE status IN ({placeholders})
              AND next_attempt_at <= UTC_TIMESTAMP(6)
            ORDER BY created_at ASC
            LIMIT {safe_limit}
        """
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql, _PENDING_STATUSES)
                    rows = await cursor.fetchall()
        except Exception as exc:
            raise GameDatabaseError("unable to list pending game operations") from exc
        return [_row_to_operation(row) for row in rows]

    async def _reserve_once(
        self,
        values: dict[str, Any],
        cooldown_seconds: int,
    ) -> GameReservation:
        async with self._pool.acquire() as connection:
            await connection.begin()
            try:
                existing = await self._get_by_key(
                    connection,
                    values["operation_key"],
                    for_update=True,
                )
                if existing is not None:
                    operation = _row_to_operation(existing)
                    self._assert_same_operation(operation, values)
                    await connection.commit()
                    return GameReservation(operation, existing=True)

                if cooldown_seconds > 0:
                    await self._check_cooldown(
                        connection,
                        group_id=values["group_id"],
                        game_type=values["game_type"],
                        actor_qq_id=values["actor_qq_id"],
                        cooldown_seconds=cooldown_seconds,
                    )

                operation_id = str(uuid.uuid4())
                initial_status = (
                    GameStatus.PENDING_DEBIT
                    if values["debit_units"] > 0
                    else GameStatus.PENDING_CREDIT
                    if values["credit_units"] > 0
                    else GameStatus.SUCCEEDED
                )
                sql = f"""
                    INSERT INTO `{self.table}` (
                        operation_id, operation_key, game_type, group_id,
                        actor_qq_id, actor_user_id,
                        target_qq_id, target_user_id,
                        debit_user_id, credit_user_id,
                        debit_units, credit_units, won, status,
                        completed_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s = 'succeeded' THEN UTC_TIMESTAMP(6) ELSE NULL END
                    )
                """
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        sql,
                        (
                            operation_id,
                            values["operation_key"],
                            values["game_type"],
                            values["group_id"],
                            values["actor_qq_id"],
                            values["actor_user_id"],
                            values["target_qq_id"],
                            values["target_user_id"],
                            values["debit_user_id"],
                            values["credit_user_id"],
                            values["debit_units"],
                            values["credit_units"],
                            int(values["won"]),
                            initial_status.value,
                            initial_status.value,
                        ),
                    )
                row = await self._get_by_id(connection, operation_id)
                if row is None:
                    raise GameDatabaseError("reserved game operation could not be read")
                await connection.commit()
                return GameReservation(_row_to_operation(row), existing=False)
            except Exception:
                await connection.rollback()
                raise

    async def _check_cooldown(
        self,
        connection: Any,
        *,
        group_id: str,
        game_type: str,
        actor_qq_id: str,
        cooldown_seconds: int,
    ) -> None:
        sql = f"""
            SELECT GREATEST(
                1,
                %s - TIMESTAMPDIFF(SECOND, created_at, UTC_TIMESTAMP(6))
            ) AS retry_after_seconds
            FROM `{self.table}`
            WHERE group_id = %s
              AND game_type = %s
              AND actor_qq_id = %s
              AND created_at > TIMESTAMPADD(SECOND, -%s, UTC_TIMESTAMP(6))
            ORDER BY created_at DESC
            LIMIT 1
            FOR UPDATE
        """
        async with connection.cursor() as cursor:
            await cursor.execute(
                sql,
                (
                    cooldown_seconds,
                    group_id,
                    game_type,
                    actor_qq_id,
                    cooldown_seconds,
                ),
            )
            row = await cursor.fetchone()
        if row is not None:
            raise GameCooldownActive(int(row["retry_after_seconds"]))

    async def _get_one(self, column: str, value: str) -> GameOperation | None:
        sql = f"SELECT * FROM `{self.table}` WHERE `{column}` = %s LIMIT 1"
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql, (value,))
                    row = await cursor.fetchone()
        except Exception as exc:
            raise GameDatabaseError("unable to read game operation") from exc
        return _row_to_operation(row) if row else None

    async def _get_by_key(
        self,
        connection: Any,
        operation_key: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        sql = (
            f"SELECT * FROM `{self.table}` WHERE operation_key = %s LIMIT 1"
            + (" FOR UPDATE" if for_update else "")
        )
        async with connection.cursor() as cursor:
            await cursor.execute(sql, (operation_key,))
            return await cursor.fetchone()

    async def _get_by_id(
        self,
        connection: Any,
        operation_id: str,
    ) -> dict[str, Any] | None:
        sql = f"SELECT * FROM `{self.table}` WHERE operation_id = %s LIMIT 1"
        async with connection.cursor() as cursor:
            await cursor.execute(sql, (operation_id,))
            return await cursor.fetchone()

    async def _transition(
        self,
        operation_id: str,
        destination: GameStatus,
        sources: Iterable[GameStatus],
        *,
        completed: bool,
    ) -> None:
        source_values = tuple(source.value for source in sources)
        placeholders = ", ".join("%s" for _ in source_values)
        completed_sql = "UTC_TIMESTAMP(6)" if completed else "NULL"
        sql = f"""
            UPDATE `{self.table}`
            SET status = %s,
                last_error = NULL,
                next_attempt_at = UTC_TIMESTAMP(6),
                completed_at = {completed_sql},
                updated_at = UTC_TIMESTAMP(6)
            WHERE operation_id = %s AND status IN ({placeholders})
        """
        await self._execute_update(
            sql,
            (destination.value, operation_id, *source_values),
            f"mark game operation {destination.value}",
        )

    async def _execute_update(
        self,
        sql: str,
        params: tuple[Any, ...],
        action: str,
    ) -> None:
        await self.initialize()
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql, params)
        except Exception as exc:
            raise GameDatabaseError(f"unable to {action}") from exc

    async def _create_table(self) -> None:
        sql = f"""
            CREATE TABLE IF NOT EXISTS `{self.table}` (
                operation_id CHAR(36) NOT NULL,
                operation_key VARCHAR(191) CHARACTER SET utf8mb4
                    COLLATE utf8mb4_bin NOT NULL,
                game_type VARCHAR(32) NOT NULL,
                group_id VARCHAR(128) CHARACTER SET utf8mb4
                    COLLATE utf8mb4_bin NOT NULL,
                actor_qq_id VARCHAR(128) CHARACTER SET utf8mb4
                    COLLATE utf8mb4_bin NOT NULL,
                actor_user_id BIGINT UNSIGNED NOT NULL,
                target_qq_id VARCHAR(128) CHARACTER SET utf8mb4
                    COLLATE utf8mb4_bin NULL,
                target_user_id BIGINT UNSIGNED NULL,
                debit_user_id BIGINT UNSIGNED NOT NULL,
                credit_user_id BIGINT UNSIGNED NULL,
                debit_units BIGINT UNSIGNED NOT NULL,
                credit_units BIGINT UNSIGNED NOT NULL,
                won BOOLEAN NOT NULL,
                status VARCHAR(24) NOT NULL,
                attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
                last_error VARCHAR(1000) NULL,
                next_attempt_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                completed_at DATETIME(6) NULL,
                created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                    ON UPDATE CURRENT_TIMESTAMP(6),
                PRIMARY KEY (operation_id),
                UNIQUE KEY uq_fapi_game_operation_key (operation_key),
                KEY idx_fapi_game_cooldown (
                    group_id, game_type, actor_qq_id, created_at
                ),
                KEY idx_fapi_game_pending (status, next_attempt_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
        async with self._pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(sql)
                await self._ensure_platform_id_columns(cursor)
            await connection.commit()

    async def _ensure_platform_id_columns(self, cursor: Any) -> None:
        definitions = {
            "group_id": "NOT NULL",
            "actor_qq_id": "NOT NULL",
            "target_qq_id": "NULL",
        }
        for column, nullable in definitions.items():
            await cursor.execute(
                """
                SELECT CHARACTER_MAXIMUM_LENGTH AS max_length,
                       COLLATION_NAME AS collation_name
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = %s
                  AND COLUMN_NAME = %s
                LIMIT 1
                """,
                (self.table, column),
            )
            row = await cursor.fetchone()
            if row is None:
                raise GameDatabaseError(f"game table is missing {column}")
            if (
                int(row["max_length"]) >= _PLATFORM_ID_MAX_LENGTH
                and str(row["collation_name"]).casefold() == "utf8mb4_bin"
            ):
                continue
            await cursor.execute(
                f"ALTER TABLE `{self.table}` MODIFY COLUMN `{column}` "
                "VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin "
                f"{nullable}"
            )

    def _validate_config(self) -> None:
        if not self.host or not self.user or not self.database:
            raise GameDatabaseError("incomplete MySQL configuration")
        if not 1 <= self.port <= 65535:
            raise GameDatabaseError("invalid MySQL port")
        if not _IDENTIFIER_PATTERN.fullmatch(self.table):
            raise GameDatabaseError("invalid game table name")

    @staticmethod
    def _validate_reservation(**values: Any) -> None:
        for key in ("operation_key", "game_type", "group_id", "actor_qq_id"):
            if not isinstance(values[key], str) or not values[key].strip():
                raise ValueError(f"{key} is required")
        if len(values["operation_key"]) > 191:
            raise ValueError("operation_key is too long")
        if len(values["game_type"]) > 32:
            raise ValueError("game_type is too long")
        for key in (
            "actor_user_id",
            "debit_user_id",
            "target_user_id",
            "credit_user_id",
        ):
            value = values[key]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{key} must be a positive integer")
        for key in ("debit_units", "credit_units", "cooldown_seconds"):
            value = values[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{key} must be a non-negative integer")
        if not isinstance(values["won"], bool):
            raise TypeError("won must be a boolean")
        if (values["target_qq_id"] is None) != (values["target_user_id"] is None):
            raise ValueError("target qq and user ids must both be set or both be absent")
        if values["credit_units"] > 0 and values["credit_user_id"] is None:
            raise ValueError("credit_user_id is required when credit_units is positive")

    @staticmethod
    def _assert_same_operation(
        existing: GameOperation,
        values: dict[str, Any],
    ) -> None:
        compared = (
            "game_type",
            "group_id",
            "actor_qq_id",
            "actor_user_id",
            "target_qq_id",
            "target_user_id",
            "debit_user_id",
            "credit_user_id",
            "debit_units",
            "credit_units",
            "won",
        )
        if any(getattr(existing, key) != values[key] for key in compared):
            raise GameOperationConflict(
                "operation_key already exists with different parameters"
            )

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self.ssl_enabled:
            return None
        context = ssl.create_default_context()
        if not self.ssl_verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context


def _row_to_operation(row: dict[str, Any]) -> GameOperation:
    return GameOperation(
        operation_id=str(row["operation_id"]),
        operation_key=str(row["operation_key"]),
        game_type=str(row["game_type"]),
        group_id=str(row["group_id"]),
        actor_qq_id=str(row["actor_qq_id"]),
        actor_user_id=int(row["actor_user_id"]),
        target_qq_id=(
            str(row["target_qq_id"]) if row.get("target_qq_id") is not None else None
        ),
        target_user_id=(
            int(row["target_user_id"])
            if row.get("target_user_id") is not None
            else None
        ),
        debit_user_id=int(row["debit_user_id"]),
        credit_user_id=(
            int(row["credit_user_id"])
            if row.get("credit_user_id") is not None
            else None
        ),
        debit_units=int(row["debit_units"]),
        credit_units=int(row["credit_units"]),
        won=bool(row["won"]),
        status=GameStatus(str(row["status"])),
        attempt_count=int(row["attempt_count"]),
        last_error=(str(row["last_error"]) if row.get("last_error") else None),
        next_attempt_at=row["next_attempt_at"],
        created_at=row["created_at"],
    )


def _is_duplicate_key_error(exc: Exception) -> bool:
    return bool(exc.args and exc.args[0] == 1062)
