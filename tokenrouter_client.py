from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
from typing import Any

import httpx


class TokenRouterError(RuntimeError):
    """TokenRouter 请求或响应错误。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        api_code: str | int | None = None,
        outcome_unknown: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.api_code = api_code
        self.outcome_unknown = outcome_unknown
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class TokenRouterUser:
    user_id: int
    username: str
    email: str
    status: str


@dataclass(frozen=True, slots=True)
class TokenRouterBalanceSummary:
    user_id: int
    username: str
    balance: float
    used_last_30_days: float
    concurrency: int


@dataclass(frozen=True, slots=True)
class TokenRouterRewardResult:
    redeem_code: str
    user_id: int
    amount: Decimal


@dataclass(frozen=True, slots=True)
class TokenRouterBalanceAdjustment:
    user_id: int
    balance: Decimal


def resolve_exact_username_users(
    data: Any,
    expected_username: str,
) -> list[TokenRouterUser]:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise TokenRouterError("用户列表响应格式不正确")

    expected = expected_username.strip()
    matches: dict[int, TokenRouterUser] = {}
    for item in data["items"]:
        if not isinstance(item, dict):
            continue
        username = item.get("username")
        email = item.get("email")
        user_id = _positive_int(item.get("id"))
        if (
            not isinstance(username, str)
            or not isinstance(email, str)
            or user_id is None
        ):
            continue
        if username != expected:
            continue
        matches[user_id] = TokenRouterUser(
            user_id=user_id,
            username=username,
            email=email,
            status=str(item.get("status") or ""),
        )
    return list(matches.values())


def resolve_balance_summary(
    user_data: Any,
    usage_data: Any,
    expected_user_id: int,
) -> TokenRouterBalanceSummary:
    if not isinstance(user_data, dict):
        raise TokenRouterError("用户详情响应格式不正确")

    user_id = _positive_int(user_data.get("id"))
    username = user_data.get("username")
    balance = _finite_float(user_data.get("balance"))
    concurrency = _non_negative_int(user_data.get("concurrency"))
    if (
        user_id != expected_user_id
        or not isinstance(username, str)
        or balance is None
        or concurrency is None
    ):
        raise TokenRouterError("用户详情响应缺少余额查询所需字段")

    if not isinstance(usage_data, dict) or not isinstance(usage_data.get("stats"), dict):
        raise TokenRouterError("用户用量响应格式不正确")
    stats = usage_data["stats"]
    usage = stats.get(str(expected_user_id), stats.get(expected_user_id))
    if not isinstance(usage, dict):
        raise TokenRouterError("用户用量响应缺少当前用户")
    used_last_30_days = _finite_float(usage.get("total_actual_cost"))
    if used_last_30_days is None or used_last_30_days < 0:
        raise TokenRouterError("用户用量响应缺少有效消费金额")

    return TokenRouterBalanceSummary(
        user_id=user_id,
        username=username,
        balance=balance,
        used_last_30_days=used_last_30_days,
        concurrency=concurrency,
    )


def resolve_reward_result(
    data: Any,
    *,
    expected_code: str,
    expected_user_id: int,
    expected_amount: Decimal,
) -> TokenRouterRewardResult:
    if not isinstance(data, dict) or not isinstance(data.get("redeem_code"), dict):
        raise TokenRouterError("奖励发放响应格式不正确")
    redeem = data["redeem_code"]
    code = redeem.get("code")
    code_type = redeem.get("type")
    user_id = _positive_int(redeem.get("used_by"))
    try:
        amount = Decimal(str(redeem.get("value")))
    except (InvalidOperation, ValueError):
        amount = None
    used_count = _positive_int(redeem.get("used_count"))
    if (
        code != expected_code
        or code_type != "balance"
        or redeem.get("status") != "used"
        or user_id != expected_user_id
        or amount is None
        or not amount.is_finite()
        or used_count is None
        or amount != expected_amount
    ):
        raise TokenRouterError(
            "奖励发放响应与请求不一致",
            api_code="REWARD_RESPONSE_MISMATCH",
            outcome_unknown=True,
        )
    return TokenRouterRewardResult(
        redeem_code=code,
        user_id=user_id,
        amount=amount,
    )


def resolve_balance_adjustment(
    data: Any,
    *,
    expected_user_id: int,
) -> TokenRouterBalanceAdjustment:
    if not isinstance(data, dict):
        raise TokenRouterError(
            "余额调整响应格式不正确",
            api_code="BALANCE_RESPONSE_MISMATCH",
            outcome_unknown=True,
        )
    user_id = _positive_int(data.get("id"))
    try:
        balance = Decimal(str(data.get("balance")))
    except (InvalidOperation, ValueError):
        balance = None
    if (
        user_id != expected_user_id
        or balance is None
        or not balance.is_finite()
        or balance < 0
    ):
        raise TokenRouterError(
            "余额调整响应与请求不一致",
            api_code="BALANCE_RESPONSE_MISMATCH",
            outcome_unknown=True,
        )
    return TokenRouterBalanceAdjustment(user_id=user_id, balance=balance)


def is_insufficient_balance_error(exc: TokenRouterError) -> bool:
    if str(exc.api_code or "").upper() in {
        "BALANCE_NEGATIVE",
        "INSUFFICIENT_BALANCE",
    }:
        return True
    message = str(exc).casefold()
    return exc.status_code == 400 and (
        "balance cannot be negative" in message
        or "insufficient balance" in message
        or "余额不足" in message
    )


class TokenRouterClient:
    def __init__(
        self,
        base_url: str,
        admin_api_key: str,
        *,
        timeout: float = 10.0,
        verify_ssl: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized_base_url = base_url.strip().rstrip("/")
        if normalized_base_url.endswith("/api/v1"):
            normalized_base_url = normalized_base_url[: -len("/api/v1")]

        self.base_url = normalized_base_url
        self.admin_api_key = admin_api_key.strip()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            verify=verify_ssl,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def find_users_by_exact_username(
        self,
        username: str,
    ) -> list[TokenRouterUser]:
        normalized_username = username.strip()
        all_items: list[Any] = []
        page = 1
        while True:
            data = await self._get(
                "/api/v1/admin/users",
                params={
                    "page": page,
                    "page_size": 1000,
                    "search": normalized_username,
                    "include_subscriptions": "false",
                },
            )
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise TokenRouterError("用户列表响应格式不正确")
            all_items.extend(data["items"])

            pages = _positive_int(data.get("pages")) or 1
            if page >= pages:
                break
            page += 1

        return resolve_exact_username_users(
            {"items": all_items},
            normalized_username,
        )

    async def get_balance_summary(self, user_id: int) -> TokenRouterBalanceSummary:
        if user_id <= 0:
            raise TokenRouterError("TokenRouter 用户 ID 不正确")

        user_data = await self._get(f"/api/v1/admin/users/{user_id}")
        usage_data = await self._post(
            "/api/v1/admin/dashboard/users-usage",
            json={"user_ids": [user_id]},
        )
        return resolve_balance_summary(user_data, usage_data, user_id)

    async def create_and_redeem_reward(
        self,
        *,
        user_id: int,
        amount: Decimal,
        redeem_code: str,
        idempotency_key: str,
        notes: str,
    ) -> TokenRouterRewardResult:
        if user_id <= 0:
            raise TokenRouterError("TokenRouter 用户 ID 不正确")
        try:
            decimal_amount = Decimal(str(amount))
        except (InvalidOperation, ValueError) as exc:
            raise TokenRouterError("奖励金额不正确") from exc
        scaled_amount = decimal_amount * Decimal(100_000_000)
        if (
            not decimal_amount.is_finite()
            or decimal_amount <= 0
            or scaled_amount != scaled_amount.to_integral_value()
        ):
            raise TokenRouterError("奖励金额不正确")
        if not _valid_ascii_token(redeem_code, minimum=3, maximum=32):
            raise TokenRouterError("奖励兑换码长度不正确")
        if not _valid_ascii_token(idempotency_key, minimum=3, maximum=128):
            raise TokenRouterError("奖励幂等键长度不正确")

        data = await self._post(
            "/api/v1/admin/redeem-codes/create-and-redeem",
            json={
                "code": redeem_code,
                "type": "balance",
                "value": float(decimal_amount),
                "user_id": user_id,
                "notes": notes,
            },
            headers={"Idempotency-Key": idempotency_key},
            outcome_unknown_on_transport=True,
        )
        return resolve_reward_result(
            data,
            expected_code=redeem_code,
            expected_user_id=user_id,
            expected_amount=decimal_amount,
        )

    async def adjust_balance(
        self,
        *,
        user_id: int,
        amount: Decimal,
        operation: str,
        idempotency_key: str,
        notes: str,
    ) -> TokenRouterBalanceAdjustment:
        if user_id <= 0:
            raise TokenRouterError("TokenRouter 用户 ID 不正确")
        try:
            decimal_amount = Decimal(str(amount))
        except (InvalidOperation, ValueError) as exc:
            raise TokenRouterError("余额调整金额不正确") from exc
        scaled_amount = decimal_amount * Decimal(100_000_000)
        if (
            not decimal_amount.is_finite()
            or decimal_amount <= 0
            or scaled_amount != scaled_amount.to_integral_value()
        ):
            raise TokenRouterError("余额调整金额不正确")
        if operation not in {"add", "subtract"}:
            raise TokenRouterError("余额调整操作不正确")
        if not _valid_ascii_token(idempotency_key, minimum=3, maximum=128):
            raise TokenRouterError("余额调整幂等键不正确")

        data = await self._post(
            f"/api/v1/admin/users/{user_id}/balance",
            json={
                "balance": float(decimal_amount),
                "operation": operation,
                "notes": notes,
            },
            headers={"Idempotency-Key": idempotency_key},
            outcome_unknown_on_transport=True,
        )
        return resolve_balance_adjustment(data, expected_user_id=user_id)

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return await self._request("GET", path, params=params)

    async def _post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        outcome_unknown_on_transport: bool = False,
    ) -> Any:
        return await self._request(
            "POST",
            path,
            json=json,
            headers=headers,
            outcome_unknown_on_transport=outcome_unknown_on_transport,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        outcome_unknown_on_transport: bool = False,
    ) -> Any:
        if not self.base_url:
            raise TokenRouterError("未配置 TokenRouter 地址")
        if not self.admin_api_key:
            raise TokenRouterError("未配置 TokenRouter 管理 API Key")

        try:
            request_headers = {
                "x-api-key": self.admin_api_key,
                "Accept": "application/json",
                "Accept-Language": "zh-CN",
            }
            if headers:
                request_headers.update(headers)
            response = await self._client.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=json,
                headers=request_headers,
            )
        except httpx.TimeoutException as exc:
            raise TokenRouterError(
                "连接 TokenRouter 超时",
                outcome_unknown=outcome_unknown_on_transport,
            ) from exc
        except httpx.RequestError as exc:
            raise TokenRouterError(
                "无法连接 TokenRouter",
                outcome_unknown=outcome_unknown_on_transport,
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise TokenRouterError(
                "TokenRouter 返回了非 JSON 响应",
                status_code=response.status_code,
                outcome_unknown=(
                    outcome_unknown_on_transport
                    and (response.is_success or response.status_code >= 500)
                ),
            ) from exc

        if not response.is_success:
            message = _error_message(payload) or f"TokenRouter 请求失败（HTTP {response.status_code}）"
            raise TokenRouterError(
                message,
                status_code=response.status_code,
                api_code=payload.get("code") if isinstance(payload, dict) else None,
                outcome_unknown=(
                    outcome_unknown_on_transport and response.status_code >= 500
                ),
                retry_after=_retry_after_seconds(response.headers.get("Retry-After")),
            )

        if not isinstance(payload, dict) or "data" not in payload:
            raise TokenRouterError(
                "TokenRouter 响应缺少 data 字段",
                outcome_unknown=outcome_unknown_on_transport,
            )
        return payload["data"]


def _error_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message")
    return message.strip() if isinstance(message, str) else ""


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _valid_ascii_token(value: str, *, minimum: int, maximum: int) -> bool:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return (
        minimum <= len(encoded) <= maximum
        and all(33 <= character <= 126 for character in encoded)
    )


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 and math.isfinite(parsed) else None
