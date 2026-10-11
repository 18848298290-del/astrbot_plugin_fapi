from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
import ssl
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable

try:
    import aiomysql
except ImportError:  # pragma: no cover - AstrBot 安装依赖后不会走此分支
    aiomysql = None


MONEY_SCALE = 100_000_000
MIN_RED_PACKET_UNITS = 1_000_000  # $0.01
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_PLATFORM_ID_MAX_LENGTH = 128


class RewardDatabaseError(RuntimeError):
    pass


class RewardBudgetExhausted(RewardDatabaseError):
    pass


class RedPacketUnavailable(RewardDatabaseError):
    pass


class RewardJobStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"


@dataclass(frozen=True, slots=True)
class RewardJob:
    operation_id: str
    source_type: str
    qq_id: str
    tokenrouter_user_id: int
    amount_units: int
    redeem_code: str
    status: RewardJobStatus
    attempt_count: int
    first_attempt_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CheckinReservation:
    job: RewardJob
    reward_date: date
    streak: int
    total_days: int
    created: bool


@dataclass(frozen=True, slots=True)
class RedPacketRecord:
    packet_id: int
    group_id: str
    creator_qq_id: str
    total_units: int
    remaining_units: int
    total_count: int
    remaining_count: int
    expires_at: datetime
    created: bool = True
    credited_count: int = 0


@dataclass(frozen=True, slots=True)
class RedPacketClaimReservation:
    packet_id: int
    job: RewardJob
    created: bool


def money_to_units(value: Any, *, allow_zero: bool = False) -> int:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError) as exc:
        raise ValueError("金额格式不正确") from exc
    if not amount.is_finite():
        raise ValueError("金额必须是有限数字")
    if amount < 0 or (amount == 0 and not allow_zero):
        raise ValueError("金额必须大于 0")
    scaled = amount * MONEY_SCALE
    if scaled != scaled.to_integral_value():
        raise ValueError("金额最多支持 8 位小数")
    units = int(scaled)
    if units > 9_000_000_000_000_000_000:
        raise ValueError("金额超出支持范围")
    return units


def units_to_decimal(units: int) -> Decimal:
    return Decimal(units) / Decimal(MONEY_SCALE)


def split_red_packet_share(
    remaining_units: int,
    remaining_count: int,
    *,
    randbelow: Callable[[int], int] = secrets.randbelow,
) -> int:
    if remaining_count <= 0:
        raise ValueError("红包剩余份数必须大于 0")
    if remaining_units < remaining_count * MIN_RED_PACKET_UNITS:
        raise ValueError("红包剩余金额不足以分配")
    if remaining_count == 1:
        return remaining_units

    maximum = remaining_units - (remaining_count - 1) * MIN_RED_PACKET_UNITS
    double_average = max(
        MIN_RED_PACKET_UNITS,
        (remaining_units * 2) // remaining_count,
    )
    upper = min(maximum, double_average)
    choices = upper - MIN_RED_PACKET_UNITS + 1
    return MIN_RED_PACKET_UNITS + randbelow(choices)


class MySQLRewardStore:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        jobs_table: str = "astrbot_fapi_reward_jobs",
        checkins_table: str = "astrbot_fapi_checkins",
        packets_table: str = "astrbot_fapi_red_packets",
        claims_table: str = "astrbot_fapi_red_claims",
        budgets_table: str = "astrbot_fapi_reward_budgets",
        connect_timeout: int = 10,
        ssl_enabled: bool = True,
        ssl_verify: bool = True,
    ) -> None:
        self.host = host.strip()
        self.port = port
        self.user = user.strip()
        self.password = password
        self.database = database.strip()
        self.jobs_table = jobs_table.strip()
        self.checkins_table = checkins_table.strip()
        self.packets_table = packets_table.strip()
        self.claims_table = claims_table.strip()
        self.budgets_table = budgets_table.strip()
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
                raise RewardDatabaseError("缺少 aiomysql 依赖")
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
                await self._create_tables()
            except Exception as exc:
                if self._pool is not None:
                    self._pool.close()
                    await self._pool.wait_closed()
                    self._pool = None
                if isinstance(exc, RewardDatabaseError):
                    raise
                raise RewardDatabaseError("无法初始化奖励数据库") from exc

    async def close(self) -> None:
        if self._pool is None:
            return
        self._pool.close()
        await self._pool.wait_closed()
        self._pool = None

    async def reserve_checkin(
        self,
        *,
        qq_id: str,
        tokenrouter_user_id: int,
        reward_date: date,
        amount_units: int,
        daily_budget_units: int,
    ) -> CheckinReservation:
        await self.initialize()
        async with self._pool.acquire() as connection:
            await connection.begin()
            try:
                await self._lock_budget_row(connection, reward_date)
                existing = await self._get_checkin(
                    connection,
                    qq_id,
                    reward_date,
                    for_update=True,
                )
                if existing is not None:
                    await connection.commit()
                    return existing

                budget = await self._get_budget(connection, reward_date)
                reserved = int(budget["checkin_reserved_units"])
                if reserved + amount_units > daily_budget_units:
                    await connection.rollback()
                    raise RewardBudgetExhausted("今日签到奖励预算已发完")

                previous_date = reward_date - timedelta(days=1)
                previous_streak = await self._previous_checkin_streak(
                    connection,
                    qq_id,
                    previous_date,
                )
                total_days = await self._successful_checkin_count(connection, qq_id)
                operation_id = str(uuid.uuid4())
                redeem_code = _redeem_code(operation_id)
                await self._insert_reward_job(
                    connection,
                    operation_id=operation_id,
                    source_type="checkin",
                    qq_id=qq_id,
                    tokenrouter_user_id=tokenrouter_user_id,
                    amount_units=amount_units,
                    redeem_code=redeem_code,
                )
                sql = (
                    f"INSERT INTO `{self.checkins_table}` "
                    "(operation_id, qq_id, tokenrouter_user_id, reward_date, "
                    "amount_units, streak) VALUES (%s, %s, %s, %s, %s, %s)"
                )
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        sql,
                        (
                            operation_id,
                            qq_id,
                            tokenrouter_user_id,
                            reward_date,
                            amount_units,
                            previous_streak + 1,
                        ),
                    )
                    await cursor.execute(
                        f"UPDATE `{self.budgets_table}` "
                        "SET checkin_reserved_units = checkin_reserved_units + %s "
                        "WHERE reward_date = %s",
                        (amount_units, reward_date),
                    )
                await connection.commit()
                return CheckinReservation(
                    job=RewardJob(
                        operation_id=operation_id,
                        source_type="checkin",
                        qq_id=qq_id,
                        tokenrouter_user_id=tokenrouter_user_id,
                        amount_units=amount_units,
                        redeem_code=redeem_code,
                        status=RewardJobStatus.PENDING,
                        attempt_count=0,
                    ),
                    reward_date=reward_date,
                    streak=previous_streak + 1,
                    total_days=total_days + 1,
                    created=True,
                )
            except RewardBudgetExhausted:
                raise
            except Exception as exc:
                await connection.rollback()
                raise RewardDatabaseError("预留签到奖励失败") from exc

    async def create_red_packet(
        self,
        *,
        creation_key: str,
        group_id: str,
        creator_qq_id: str,
        total_units: int,
        total_count: int,
        reward_date: date,
        daily_budget_units: int,
        expires_at: datetime,
    ) -> RedPacketRecord:
        await self.initialize()
        async with self._pool.acquire() as connection:
            await connection.begin()
            try:
                await self._lock_budget_row(connection, reward_date)
                existing = await self._get_packet_by_creation_key(
                    connection,
                    creation_key,
                )
                if existing is not None:
                    record = self._existing_red_packet_record(
                        existing,
                        group_id=group_id,
                        creator_qq_id=creator_qq_id,
                        total_units=total_units,
                        total_count=total_count,
                    )
                    await connection.commit()
                    return record

                budget = await self._get_budget(connection, reward_date)
                reserved = int(budget["red_packet_reserved_units"])
                if reserved + total_units > daily_budget_units:
                    await connection.rollback()
                    raise RewardBudgetExhausted("今日红包预算不足")

                sql = f"""
                    INSERT INTO `{self.packets_table}` (
                        creation_key, group_id, creator_qq_id,
                        total_units, remaining_units,
                        total_count, remaining_count, status, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'open', %s)
                """
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        sql,
                        (
                            creation_key,
                            group_id,
                            creator_qq_id,
                            total_units,
                            total_units,
                            total_count,
                            total_count,
                            expires_at,
                        ),
                    )
                    packet_id = int(cursor.lastrowid)
                    await cursor.execute(
                        f"UPDATE `{self.budgets_table}` "
                        "SET red_packet_reserved_units = red_packet_reserved_units + %s "
                        "WHERE reward_date = %s",
                        (total_units, reward_date),
                    )
                await connection.commit()
                return RedPacketRecord(
                    packet_id=packet_id,
                    group_id=group_id,
                    creator_qq_id=creator_qq_id,
                    total_units=total_units,
                    remaining_units=total_units,
                    total_count=total_count,
                    remaining_count=total_count,
                    expires_at=expires_at,
                )
            except RewardBudgetExhausted:
                raise
            except RewardDatabaseError:
                await connection.rollback()
                raise
            except Exception as exc:
                await connection.rollback()
                if _is_duplicate_key_error(exc):
                    try:
                        existing = await self._get_packet_by_creation_key(
                            connection,
                            creation_key,
                        )
                    except Exception as lookup_exc:
                        raise RewardDatabaseError(
                            "红包创建冲突后读取结果失败"
                        ) from lookup_exc
                    if existing is not None:
                        return self._existing_red_packet_record(
                            existing,
                            group_id=group_id,
                            creator_qq_id=creator_qq_id,
                            total_units=total_units,
                            total_count=total_count,
                        )
                raise RewardDatabaseError("创建群红包失败") from exc

    async def reserve_red_packet_claim(
        self,
        *,
        group_id: str,
        qq_id: str,
        tokenrouter_user_id: int,
        packet_id: int | None = None,
    ) -> RedPacketClaimReservation:
        await self.initialize()
        async with self._pool.acquire() as connection:
            await connection.begin()
            try:
                packet = await self._lock_claimable_packet(
                    connection,
                    group_id=group_id,
                    packet_id=packet_id,
                )
                if packet is None:
                    await connection.rollback()
                    raise RedPacketUnavailable("当前群没有可领取的红包")

                existing = await self._get_claim(
                    connection,
                    int(packet["id"]),
                    qq_id,
                )
                if existing is not None:
                    await connection.commit()
                    return existing

                if packet["status"] != "open":
                    await connection.rollback()
                    raise RedPacketUnavailable("该红包已经领完")
                if packet["expires_at"] <= datetime.now(timezone.utc).replace(tzinfo=None):
                    await self._expire_packet(connection, int(packet["id"]))
                    await connection.commit()
                    raise RedPacketUnavailable("该红包已经过期")

                remaining_units = int(packet["remaining_units"])
                remaining_count = int(packet["remaining_count"])
                share_units = split_red_packet_share(
                    remaining_units,
                    remaining_count,
                )
                operation_id = str(uuid.uuid4())
                redeem_code = _redeem_code(operation_id)
                await self._insert_reward_job(
                    connection,
                    operation_id=operation_id,
                    source_type="red_packet",
                    qq_id=qq_id,
                    tokenrouter_user_id=tokenrouter_user_id,
                    amount_units=share_units,
                    redeem_code=redeem_code,
                )
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        f"INSERT INTO `{self.claims_table}` "
                        "(operation_id, packet_id, qq_id, tokenrouter_user_id, amount_units) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (
                            operation_id,
                            int(packet["id"]),
                            qq_id,
                            tokenrouter_user_id,
                            share_units,
                        ),
                    )
                    new_count = remaining_count - 1
                    new_status = "finished" if new_count == 0 else "open"
                    await cursor.execute(
                        f"UPDATE `{self.packets_table}` SET "
                        "remaining_units = remaining_units - %s, "
                        "remaining_count = remaining_count - 1, status = %s "
                        "WHERE id = %s",
                        (share_units, new_status, int(packet["id"])),
                    )
                await connection.commit()
                return RedPacketClaimReservation(
                    packet_id=int(packet["id"]),
                    job=RewardJob(
                        operation_id=operation_id,
                        source_type="red_packet",
                        qq_id=qq_id,
                        tokenrouter_user_id=tokenrouter_user_id,
                        amount_units=share_units,
                        redeem_code=redeem_code,
                        status=RewardJobStatus.PENDING,
                        attempt_count=0,
                    ),
                    created=True,
                )
            except RedPacketUnavailable:
                raise
            except Exception as exc:
                await connection.rollback()
                raise RewardDatabaseError("领取群红包失败") from exc

    async def get_red_packet(
        self,
        *,
        group_id: str,
        packet_id: int | None = None,
    ) -> RedPacketRecord | None:
        await self.initialize()
        conditions = "p.group_id = %s"
        values: list[Any] = [group_id]
        if packet_id is not None:
            conditions += " AND p.id = %s"
            values.append(packet_id)
        sql = f"""
            SELECT p.id, p.group_id, p.creator_qq_id,
                   p.total_units, p.remaining_units,
                   p.total_count, p.remaining_count, p.expires_at,
                   COALESCE(SUM(j.status = 'succeeded'), 0) AS credited_count
            FROM `{self.packets_table}` p
            LEFT JOIN `{self.claims_table}` c ON c.packet_id = p.id
            LEFT JOIN `{self.jobs_table}` j ON j.operation_id = c.operation_id
            WHERE {conditions}
            GROUP BY p.id
            ORDER BY p.id DESC
            LIMIT 1
        """
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql, tuple(values))
                    row = await cursor.fetchone()
        except Exception as exc:
            raise RewardDatabaseError("查询红包状态失败") from exc
        return _row_to_packet(row) if row else None

    async def get_reward_job(self, operation_id: str) -> RewardJob | None:
        await self.initialize()
        sql = f"SELECT * FROM `{self.jobs_table}` WHERE operation_id = %s LIMIT 1"
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql, (operation_id,))
                    row = await cursor.fetchone()
        except Exception as exc:
            raise RewardDatabaseError("读取奖励任务失败") from exc
        return _row_to_job(row) if row else None

    async def list_pending_jobs(self, limit: int = 20) -> list[RewardJob]:
        await self.initialize()
        safe_limit = max(1, min(limit, 100))
        sql = f"""
            SELECT * FROM `{self.jobs_table}`
            WHERE status = 'pending' AND next_attempt_at <= UTC_TIMESTAMP(6)
            ORDER BY created_at ASC
            LIMIT {safe_limit}
        """
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql)
                    rows = await cursor.fetchall()
        except Exception as exc:
            raise RewardDatabaseError("读取待发奖励失败") from exc
        return [_row_to_job(row) for row in rows]

    async def record_reward_attempt(self, operation_id: str) -> None:
        await self.initialize()
        sql = f"""
            UPDATE `{self.jobs_table}`
            SET attempt_count = attempt_count + 1,
                first_attempt_at = COALESCE(first_attempt_at, UTC_TIMESTAMP(6)),
                updated_at = UTC_TIMESTAMP(6)
            WHERE operation_id = %s AND status = 'pending'
        """
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql, (operation_id,))
        except Exception as exc:
            raise RewardDatabaseError("记录奖励发放尝试失败") from exc

    async def mark_reward_succeeded(self, operation_id: str) -> None:
        await self.initialize()
        sql = f"""
            UPDATE `{self.jobs_table}`
            SET status = 'succeeded', last_error = NULL,
                completed_at = UTC_TIMESTAMP(6), updated_at = UTC_TIMESTAMP(6)
            WHERE operation_id = %s
        """
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql, (operation_id,))
        except Exception as exc:
            raise RewardDatabaseError("确认奖励发放结果失败") from exc

    async def record_reward_error(
        self,
        operation_id: str,
        error: str,
        *,
        retry_seconds: int,
    ) -> None:
        await self.initialize()
        sql = f"""
            UPDATE `{self.jobs_table}`
            SET last_error = %s,
                next_attempt_at = TIMESTAMPADD(SECOND, %s, UTC_TIMESTAMP(6)),
                updated_at = UTC_TIMESTAMP(6)
            WHERE operation_id = %s AND status = 'pending'
        """
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        sql,
                        (error[:1000], max(1, retry_seconds), operation_id),
                    )
        except Exception as exc:
            raise RewardDatabaseError("记录奖励发放错误失败") from exc

    async def _create_tables(self) -> None:
        statements = [
            f"""
                CREATE TABLE IF NOT EXISTS `{self.jobs_table}` (
                    operation_id CHAR(36) NOT NULL,
                    source_type VARCHAR(24) NOT NULL,
                    qq_id VARCHAR(128) CHARACTER SET utf8mb4
                        COLLATE utf8mb4_bin NOT NULL,
                    tokenrouter_user_id BIGINT UNSIGNED NOT NULL,
                    amount_units BIGINT UNSIGNED NOT NULL,
                    redeem_code VARCHAR(32) NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
                    first_attempt_at DATETIME(6) NULL,
                    next_attempt_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                    last_error VARCHAR(1000) NULL,
                    completed_at DATETIME(6) NULL,
                    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                        ON UPDATE CURRENT_TIMESTAMP(6),
                    PRIMARY KEY (operation_id),
                    UNIQUE KEY uq_fapi_reward_code (redeem_code),
                    KEY idx_fapi_reward_pending (status, next_attempt_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            f"""
                CREATE TABLE IF NOT EXISTS `{self.checkins_table}` (
                    operation_id CHAR(36) NOT NULL,
                    qq_id VARCHAR(128) CHARACTER SET utf8mb4
                        COLLATE utf8mb4_bin NOT NULL,
                    tokenrouter_user_id BIGINT UNSIGNED NOT NULL,
                    reward_date DATE NOT NULL,
                    amount_units BIGINT UNSIGNED NOT NULL,
                    streak INT UNSIGNED NOT NULL,
                    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                    PRIMARY KEY (operation_id),
                    UNIQUE KEY uq_fapi_checkin_day (qq_id, reward_date),
                    UNIQUE KEY uq_fapi_checkin_user_day (
                        tokenrouter_user_id, reward_date
                    )
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            f"""
                CREATE TABLE IF NOT EXISTS `{self.packets_table}` (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    creation_key VARCHAR(191) NOT NULL,
                    group_id VARCHAR(128) CHARACTER SET utf8mb4
                        COLLATE utf8mb4_bin NOT NULL,
                    creator_qq_id VARCHAR(128) CHARACTER SET utf8mb4
                        COLLATE utf8mb4_bin NOT NULL,
                    total_units BIGINT UNSIGNED NOT NULL,
                    remaining_units BIGINT UNSIGNED NOT NULL,
                    total_count INT UNSIGNED NOT NULL,
                    remaining_count INT UNSIGNED NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'open',
                    expires_at DATETIME(6) NOT NULL,
                    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                        ON UPDATE CURRENT_TIMESTAMP(6),
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_fapi_packet_creation (creation_key),
                    KEY idx_fapi_packet_group (group_id, status, expires_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            f"""
                CREATE TABLE IF NOT EXISTS `{self.claims_table}` (
                    operation_id CHAR(36) NOT NULL,
                    packet_id BIGINT UNSIGNED NOT NULL,
                    qq_id VARCHAR(128) CHARACTER SET utf8mb4
                        COLLATE utf8mb4_bin NOT NULL,
                    tokenrouter_user_id BIGINT UNSIGNED NOT NULL,
                    amount_units BIGINT UNSIGNED NOT NULL,
                    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                    PRIMARY KEY (operation_id),
                    UNIQUE KEY uq_fapi_packet_qq (packet_id, qq_id),
                    UNIQUE KEY uq_fapi_packet_user (
                        packet_id, tokenrouter_user_id
                    ),
                    KEY idx_fapi_claim_packet (packet_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            f"""
                CREATE TABLE IF NOT EXISTS `{self.budgets_table}` (
                    reward_date DATE NOT NULL,
                    checkin_reserved_units BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    red_packet_reserved_units BIGINT UNSIGNED NOT NULL DEFAULT 0,
                    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                        ON UPDATE CURRENT_TIMESTAMP(6),
                    PRIMARY KEY (reward_date)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
        ]
        async with self._pool.acquire() as connection:
            async with connection.cursor() as cursor:
                for statement in statements:
                    await cursor.execute(statement)
                await self._ensure_platform_id_columns(cursor)
            await connection.commit()

    async def _ensure_platform_id_columns(self, cursor: Any) -> None:
        columns = (
            (self.jobs_table, "qq_id"),
            (self.checkins_table, "qq_id"),
            (self.packets_table, "group_id"),
            (self.packets_table, "creator_qq_id"),
            (self.claims_table, "qq_id"),
        )
        for table, column in columns:
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
                (table, column),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RewardDatabaseError(f"奖励表 {table} 缺少 {column} 字段")
            if (
                int(row["max_length"]) >= _PLATFORM_ID_MAX_LENGTH
                and str(row["collation_name"]).casefold() == "utf8mb4_bin"
            ):
                continue
            await cursor.execute(
                f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` "
                "VARCHAR(128) CHARACTER SET utf8mb4 "
                "COLLATE utf8mb4_bin NOT NULL"
            )

    async def _lock_budget_row(self, connection: Any, reward_date: date) -> None:
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"INSERT INTO `{self.budgets_table}` (reward_date) VALUES (%s) "
                "ON DUPLICATE KEY UPDATE reward_date = VALUES(reward_date)",
                (reward_date,),
            )
            await cursor.execute(
                f"SELECT reward_date FROM `{self.budgets_table}` "
                "WHERE reward_date = %s FOR UPDATE",
                (reward_date,),
            )
            await cursor.fetchone()

    async def _get_budget(self, connection: Any, reward_date: date) -> dict[str, Any]:
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"SELECT checkin_reserved_units, red_packet_reserved_units "
                f"FROM `{self.budgets_table}` WHERE reward_date = %s",
                (reward_date,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RewardDatabaseError("奖励预算记录不存在")
        return row

    async def _get_checkin(
        self,
        connection: Any,
        qq_id: str,
        reward_date: date,
        *,
        for_update: bool,
    ) -> CheckinReservation | None:
        suffix = " FOR UPDATE" if for_update else ""
        sql = f"""
            SELECT c.operation_id, c.reward_date, c.streak,
                   j.source_type, j.qq_id, j.tokenrouter_user_id,
                   j.amount_units, j.redeem_code, j.status,
                   j.attempt_count, j.first_attempt_at,
                   (SELECT COUNT(*) FROM `{self.checkins_table}` c2
                    JOIN `{self.jobs_table}` j2 ON j2.operation_id = c2.operation_id
                    WHERE c2.qq_id = c.qq_id AND j2.status = 'succeeded') AS paid_days
            FROM `{self.checkins_table}` c
            JOIN `{self.jobs_table}` j ON j.operation_id = c.operation_id
            WHERE c.qq_id = %s AND c.reward_date = %s
            LIMIT 1{suffix}
        """
        async with connection.cursor() as cursor:
            await cursor.execute(sql, (qq_id, reward_date))
            row = await cursor.fetchone()
        if row is None:
            return None
        paid_days = int(row.pop("paid_days"))
        return CheckinReservation(
            job=_row_to_job(row),
            reward_date=row["reward_date"],
            streak=int(row["streak"]),
            total_days=paid_days + (0 if row["status"] == "succeeded" else 1),
            created=False,
        )

    async def _previous_checkin_streak(
        self,
        connection: Any,
        qq_id: str,
        previous_date: date,
    ) -> int:
        sql = f"""
            SELECT c.streak FROM `{self.checkins_table}` c
            JOIN `{self.jobs_table}` j ON j.operation_id = c.operation_id
            WHERE c.qq_id = %s AND c.reward_date = %s AND j.status = 'succeeded'
            LIMIT 1
        """
        async with connection.cursor() as cursor:
            await cursor.execute(sql, (qq_id, previous_date))
            row = await cursor.fetchone()
        return int(row["streak"]) if row else 0

    async def _successful_checkin_count(self, connection: Any, qq_id: str) -> int:
        sql = f"""
            SELECT COUNT(*) AS total FROM `{self.checkins_table}` c
            JOIN `{self.jobs_table}` j ON j.operation_id = c.operation_id
            WHERE c.qq_id = %s AND j.status = 'succeeded'
        """
        async with connection.cursor() as cursor:
            await cursor.execute(sql, (qq_id,))
            row = await cursor.fetchone()
        return int(row["total"])

    async def _insert_reward_job(
        self,
        connection: Any,
        *,
        operation_id: str,
        source_type: str,
        qq_id: str,
        tokenrouter_user_id: int,
        amount_units: int,
        redeem_code: str,
    ) -> None:
        sql = f"""
            INSERT INTO `{self.jobs_table}` (
                operation_id, source_type, qq_id, tokenrouter_user_id,
                amount_units, redeem_code
            ) VALUES (%s, %s, %s, %s, %s, %s)
        """
        async with connection.cursor() as cursor:
            await cursor.execute(
                sql,
                (
                    operation_id,
                    source_type,
                    qq_id,
                    tokenrouter_user_id,
                    amount_units,
                    redeem_code,
                ),
            )

    async def _get_packet_by_creation_key(
        self,
        connection: Any,
        creation_key: str,
    ) -> dict[str, Any] | None:
        sql = f"""
            SELECT id AS packet_id, group_id, creator_qq_id,
                   total_units, remaining_units, total_count,
                   remaining_count, expires_at, 0 AS credited_count
            FROM `{self.packets_table}`
            WHERE creation_key = %s LIMIT 1
        """
        async with connection.cursor() as cursor:
            await cursor.execute(sql, (creation_key,))
            return await cursor.fetchone()

    @staticmethod
    def _existing_red_packet_record(
        existing: dict[str, Any],
        *,
        group_id: str,
        creator_qq_id: str,
        total_units: int,
        total_count: int,
    ) -> RedPacketRecord:
        if (
            str(existing["group_id"]) != group_id
            or str(existing["creator_qq_id"]) != creator_qq_id
            or int(existing["total_units"]) != total_units
            or int(existing["total_count"]) != total_count
        ):
            raise RewardDatabaseError("红包创建幂等键对应的参数不一致")
        return _row_to_packet(existing)

    async def _lock_claimable_packet(
        self,
        connection: Any,
        *,
        group_id: str,
        packet_id: int | None,
    ) -> dict[str, Any] | None:
        if packet_id is None:
            sql = f"""
                SELECT * FROM `{self.packets_table}`
                WHERE group_id = %s AND status = 'open'
                  AND expires_at > UTC_TIMESTAMP(6)
                ORDER BY id DESC LIMIT 1 FOR UPDATE
            """
            values = (group_id,)
        else:
            sql = f"""
                SELECT * FROM `{self.packets_table}`
                WHERE id = %s AND group_id = %s
                LIMIT 1 FOR UPDATE
            """
            values = (packet_id, group_id)
        async with connection.cursor() as cursor:
            await cursor.execute(sql, values)
            return await cursor.fetchone()

    async def _get_claim(
        self,
        connection: Any,
        packet_id: int,
        qq_id: str,
    ) -> RedPacketClaimReservation | None:
        sql = f"""
            SELECT c.packet_id, j.*
            FROM `{self.claims_table}` c
            JOIN `{self.jobs_table}` j ON j.operation_id = c.operation_id
            WHERE c.packet_id = %s AND c.qq_id = %s
            LIMIT 1
        """
        async with connection.cursor() as cursor:
            await cursor.execute(sql, (packet_id, qq_id))
            row = await cursor.fetchone()
        if row is None:
            return None
        return RedPacketClaimReservation(
            packet_id=int(row["packet_id"]),
            job=_row_to_job(row),
            created=False,
        )

    async def _expire_packet(self, connection: Any, packet_id: int) -> None:
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"UPDATE `{self.packets_table}` SET status = 'expired' "
                "WHERE id = %s AND status = 'open'",
                (packet_id,),
            )

    def _validate_config(self) -> None:
        if not self.host or not self.user or not self.database:
            raise RewardDatabaseError("远程 MySQL 配置不完整")
        if not 1 <= self.port <= 65535:
            raise RewardDatabaseError("MySQL 端口不正确")
        table_names = {
            self.jobs_table,
            self.checkins_table,
            self.packets_table,
            self.claims_table,
            self.budgets_table,
        }
        if len(table_names) != 5:
            raise RewardDatabaseError("奖励数据表名称不能重复")
        if not all(_IDENTIFIER_PATTERN.fullmatch(name) for name in table_names):
            raise RewardDatabaseError("奖励数据表名称不合法")

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self.ssl_enabled:
            return None
        context = ssl.create_default_context()
        if not self.ssl_verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context


def _redeem_code(operation_id: str) -> str:
    digest = hashlib.sha256(operation_id.encode("ascii")).digest()
    token = base64.b32encode(digest).decode("ascii").rstrip("=")[:26]
    return f"FAPIR_{token}"


def _is_duplicate_key_error(exc: Exception) -> bool:
    return bool(exc.args and exc.args[0] == 1062)


def _row_to_job(row: dict[str, Any]) -> RewardJob:
    return RewardJob(
        operation_id=str(row["operation_id"]),
        source_type=str(row["source_type"]),
        qq_id=str(row["qq_id"]),
        tokenrouter_user_id=int(row["tokenrouter_user_id"]),
        amount_units=int(row["amount_units"]),
        redeem_code=str(row["redeem_code"]),
        status=RewardJobStatus(str(row["status"])),
        attempt_count=int(row["attempt_count"]),
        first_attempt_at=row.get("first_attempt_at"),
    )


def _row_to_packet(row: dict[str, Any]) -> RedPacketRecord:
    return RedPacketRecord(
        packet_id=int(row["id"] if "id" in row else row["packet_id"]),
        group_id=str(row["group_id"]),
        creator_qq_id=str(row["creator_qq_id"]),
        total_units=int(row["total_units"]),
        remaining_units=int(row["remaining_units"]),
        total_count=int(row["total_count"]),
        remaining_count=int(row["remaining_count"]),
        expires_at=row["expires_at"],
        created=False,
        credited_count=int(row.get("credited_count", 0)),
    )
