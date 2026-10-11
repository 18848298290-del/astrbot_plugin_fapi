import unittest
from decimal import Decimal

import httpx

from tokenrouter_client import (
    TokenRouterClient,
    TokenRouterError,
    is_insufficient_balance_error,
    resolve_balance_adjustment,
    resolve_balance_summary,
    resolve_exact_username_users,
    resolve_reward_result,
)


class ResolveExactUsernameUsersTest(unittest.TestCase):
    def test_requires_exact_username_after_fuzzy_search(self):
        data = {
            "items": [
                {
                    "id": 1,
                    "username": "alice-2",
                    "email": "12345@qq.com",
                    "status": "active",
                },
                {
                    "id": 2,
                    "username": "alice",
                    "email": "alice@example.com",
                    "status": "active",
                },
            ]
        }

        users = resolve_exact_username_users(data, "alice")

        self.assertEqual([user.user_id for user in users], [2])
        self.assertEqual(users[0].username, "alice")

    def test_username_match_is_case_sensitive(self):
        data = {
            "items": [
                {
                    "id": 1,
                    "username": "Alice",
                    "email": "alice@example.com",
                    "status": "active",
                }
            ]
        }

        self.assertEqual(resolve_exact_username_users(data, "alice"), [])

    def test_keeps_different_users_with_same_exact_username(self):
        data = {
            "items": [
                {
                    "id": 1,
                    "username": "alice",
                    "email": "12345@qq.com",
                    "status": "active",
                },
                {
                    "id": 2,
                    "username": "alice",
                    "email": "alice@example.com",
                    "status": "active",
                },
            ]
        }

        users = resolve_exact_username_users(data, "alice")

        self.assertEqual([user.user_id for user in users], [1, 2])

    def test_deduplicates_same_user_repeated_across_pages(self):
        item = {
            "id": 7,
            "username": "alice",
            "email": "alice@example.com",
            "status": "active",
        }

        users = resolve_exact_username_users({"items": [item, item]}, "alice")

        self.assertEqual([user.user_id for user in users], [7])


class ResolveBalanceSummaryTest(unittest.TestCase):
    def test_resolves_user_detail_and_string_usage_key(self):
        summary = resolve_balance_summary(
            {
                "id": 7,
                "username": "RYAN",
                "balance": "10.5",
                "concurrency": 5,
            },
            {
                "stats": {
                    "7": {
                        "total_actual_cost": "1.2345",
                    }
                }
            },
            7,
        )

        self.assertEqual(summary.user_id, 7)
        self.assertEqual(summary.username, "RYAN")
        self.assertEqual(summary.balance, 10.5)
        self.assertEqual(summary.used_last_30_days, 1.2345)
        self.assertEqual(summary.concurrency, 5)

    def test_rejects_missing_or_invalid_required_values(self):
        valid_user = {
            "id": 7,
            "username": "RYAN",
            "balance": 10,
            "concurrency": 5,
        }
        valid_usage = {"stats": {"7": {"total_actual_cost": 1.25}}}
        cases = [
            ({**valid_user, "balance": None}, valid_usage),
            ({**valid_user, "balance": "NaN"}, valid_usage),
            ({**valid_user, "concurrency": -1}, valid_usage),
            (valid_user, {"stats": {}}),
            (valid_user, {"stats": {"7": {"total_actual_cost": -1}}}),
        ]

        for user_data, usage_data in cases:
            with self.subTest(user_data=user_data, usage_data=usage_data):
                with self.assertRaises(TokenRouterError):
                    resolve_balance_summary(user_data, usage_data, 7)


class ResolveRewardResultTest(unittest.TestCase):
    def test_requires_matching_code_user_and_amount(self):
        result = resolve_reward_result(
            {
                "redeem_code": {
                    "code": "FAPI-ABC",
                    "type": "balance",
                    "value": 0.05,
                    "status": "used",
                    "used_by": 7,
                    "used_count": 1,
                }
            },
            expected_code="FAPI-ABC",
            expected_user_id=7,
            expected_amount=Decimal("0.05"),
        )

        self.assertEqual(result.redeem_code, "FAPI-ABC")
        self.assertEqual(result.user_id, 7)
        self.assertEqual(result.amount, Decimal("0.05"))

    def test_rejects_mismatched_redemption(self):
        with self.assertRaises(TokenRouterError):
            resolve_reward_result(
                {
                    "redeem_code": {
                        "code": "FAPI-ABC",
                        "type": "balance",
                        "value": 0.05,
                        "status": "used",
                        "used_by": 8,
                        "used_count": 1,
                    }
                },
                expected_code="FAPI-ABC",
                expected_user_id=7,
                expected_amount=Decimal("0.05"),
            )

    def test_rejects_amount_difference_at_eight_decimal_precision(self):
        with self.assertRaises(TokenRouterError) as raised:
            resolve_reward_result(
                {
                    "redeem_code": {
                        "code": "FAPI-ABC",
                        "type": "balance",
                        "value": 0,
                        "status": "used",
                        "used_by": 7,
                        "used_count": 1,
                    }
                },
                expected_code="FAPI-ABC",
                expected_user_id=7,
                expected_amount=Decimal("0.00000001"),
            )

        self.assertTrue(raised.exception.outcome_unknown)
        self.assertEqual(raised.exception.api_code, "REWARD_RESPONSE_MISMATCH")


class ResolveBalanceAdjustmentTest(unittest.TestCase):
    def test_requires_matching_user_and_non_negative_balance(self):
        result = resolve_balance_adjustment(
            {"id": 7, "balance": "12.34567890"},
            expected_user_id=7,
        )

        self.assertEqual(result.user_id, 7)
        self.assertEqual(result.balance, Decimal("12.34567890"))

        for data in ({"id": 8, "balance": 1}, {"id": 7, "balance": -1}):
            with self.subTest(data=data):
                with self.assertRaises(TokenRouterError) as raised:
                    resolve_balance_adjustment(data, expected_user_id=7)
                self.assertTrue(raised.exception.outcome_unknown)

    def test_identifies_atomic_insufficient_balance_error(self):
        self.assertTrue(
            is_insufficient_balance_error(
                TokenRouterError(
                    "balance cannot be negative",
                    status_code=400,
                    api_code="BALANCE_NEGATIVE",
                )
            )
        )
        self.assertFalse(
            is_insufficient_balance_error(
                TokenRouterError("service unavailable", status_code=503)
            )
        )


class TokenRouterClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_adjusts_balance_with_stable_idempotency_key(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/api/v1/admin/users/7/balance")
            self.assertEqual(request.headers["idempotency-key"], "game-op:debit")
            self.assertEqual(
                request.read(),
                b'{"balance":0.1,"operation":"subtract",'
                b'"notes":"slot stake"}',
            )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "success",
                    "data": {"id": 7, "balance": 9.9},
                },
            )

        client = TokenRouterClient(
            "https://tokenrouter.example.com",
            "secret",
            transport=httpx.MockTransport(handler),
        )
        try:
            result = await client.adjust_balance(
                user_id=7,
                amount=Decimal("0.10"),
                operation="subtract",
                idempotency_key="game-op:debit",
                notes="slot stake",
            )
        finally:
            await client.close()

        self.assertEqual(result.user_id, 7)
        self.assertEqual(result.balance, Decimal("9.9"))

    async def test_creates_and_redeems_reward_with_stable_idempotency_key(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(
                request.url.path,
                "/api/v1/admin/redeem-codes/create-and-redeem",
            )
            self.assertEqual(request.headers["idempotency-key"], "FAPI-ABC")
            self.assertEqual(
                request.read(),
                b'{"code":"FAPI-ABC","type":"balance","value":0.05,'
                b'"user_id":7,"notes":"fixed-note"}',
            )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "success",
                    "data": {
                        "redeem_code": {
                            "code": "FAPI-ABC",
                            "type": "balance",
                            "value": 0.05,
                            "status": "used",
                            "used_by": 7,
                            "used_count": 1,
                        }
                    },
                },
            )

        client = TokenRouterClient(
            "https://tokenrouter.example.com",
            "secret",
            transport=httpx.MockTransport(handler),
        )
        try:
            result = await client.create_and_redeem_reward(
                user_id=7,
                amount=Decimal("0.05"),
                redeem_code="FAPI-ABC",
                idempotency_key="FAPI-ABC",
                notes="fixed-note",
            )
        finally:
            await client.close()

        self.assertEqual(result.user_id, 7)
        self.assertEqual(result.amount, Decimal("0.05"))

    async def test_rejects_redeem_code_longer_than_database_column(self):
        client = TokenRouterClient(
            "https://tokenrouter.example.com",
            "secret",
            transport=httpx.MockTransport(
                lambda _request: self.fail("非法兑换码不应发起请求")
            ),
        )
        try:
            with self.assertRaisesRegex(TokenRouterError, "兑换码长度"):
                await client.create_and_redeem_reward(
                    user_id=7,
                    amount=Decimal("0.05"),
                    redeem_code="X" * 33,
                    idempotency_key="stable-key",
                    notes="fixed-note",
                )
        finally:
            await client.close()

    async def test_write_timeout_is_reported_as_unknown_outcome(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timeout", request=request)

        client = TokenRouterClient(
            "https://tokenrouter.example.com",
            "secret",
            transport=httpx.MockTransport(handler),
        )
        try:
            with self.assertRaises(TokenRouterError) as raised:
                await client.create_and_redeem_reward(
                    user_id=7,
                    amount=Decimal("0.05"),
                    redeem_code="FAPI-ABC",
                    idempotency_key="stable-key",
                    notes="fixed-note",
                )
        finally:
            await client.close()

        self.assertTrue(raised.exception.outcome_unknown)

    async def test_non_json_server_error_is_reported_as_unknown_outcome(self):
        client = TokenRouterClient(
            "https://tokenrouter.example.com",
            "secret",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(502, text="bad gateway")
            ),
        )
        try:
            with self.assertRaises(TokenRouterError) as raised:
                await client.create_and_redeem_reward(
                    user_id=7,
                    amount=Decimal("0.05"),
                    redeem_code="FAPI-ABC",
                    idempotency_key="stable-key",
                    notes="fixed-note",
                )
        finally:
            await client.close()

        self.assertTrue(raised.exception.outcome_unknown)
        self.assertEqual(raised.exception.status_code, 502)

    async def test_queries_user_detail_and_last_30_day_usage(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            self.assertEqual(request.headers["x-api-key"], "secret")
            if request.url.path == "/api/v1/admin/users/7":
                self.assertEqual(request.method, "GET")
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "success",
                        "data": {
                            "id": 7,
                            "username": "RYAN",
                            "balance": 10,
                            "concurrency": 5,
                        },
                    },
                )
            self.assertEqual(
                request.url.path,
                "/api/v1/admin/dashboard/users-usage",
            )
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.read(), b'{"user_ids":[7]}')
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "success",
                    "data": {
                        "stats": {
                            "7": {"total_actual_cost": 1.2345},
                        }
                    },
                },
            )

        client = TokenRouterClient(
            "https://tokenrouter.example.com",
            "secret",
            transport=httpx.MockTransport(handler),
        )
        try:
            summary = await client.get_balance_summary(7)
        finally:
            await client.close()

        self.assertEqual(len(requests), 2)
        self.assertEqual(summary.username, "RYAN")
        self.assertEqual(summary.balance, 10)
        self.assertEqual(summary.used_last_30_days, 1.2345)
        self.assertEqual(summary.concurrency, 5)

    async def test_queries_users_by_username_and_filters_fuzzy_results(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["x-api-key"], "secret")
            self.assertEqual(request.url.path, "/api/v1/admin/users")
            self.assertEqual(request.url.params["search"], "alice")
            self.assertEqual(request.url.params["include_subscriptions"], "false")
            self.assertEqual(request.url.params["page_size"], "1000")
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "success",
                    "data": {
                        "items": [
                            {
                                "id": 6,
                                "username": "alice-2",
                                "email": "12345@qq.com",
                                "status": "active",
                            },
                            {
                                "id": 7,
                                "username": "alice",
                                "email": "alice@example.com",
                                "status": "active",
                            },
                        ],
                        "total": 2,
                        "page": 1,
                        "page_size": 20,
                        "pages": 1,
                    },
                },
            )

        client = TokenRouterClient(
            "https://tokenrouter.example.com/api/v1",
            "secret",
            transport=httpx.MockTransport(handler),
        )
        try:
            users = await client.find_users_by_exact_username("alice")
        finally:
            await client.close()

        self.assertEqual([user.user_id for user in users], [7])

    async def test_checks_all_search_result_pages(self):
        requested_pages = []

        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params["page"])
            requested_pages.append(page)
            items = (
                [
                    {
                        "id": 1,
                        "username": "alice-2",
                        "email": "similar@example.com",
                        "status": "active",
                    }
                ]
                if page == 1
                else [
                    {
                        "id": 7,
                        "username": "alice",
                        "email": "alice@example.com",
                        "status": "active",
                    }
                ]
            )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "success",
                    "data": {
                        "items": items,
                        "total": 2,
                        "page": page,
                        "page_size": 1,
                        "pages": 2,
                    },
                },
            )

        client = TokenRouterClient(
            "https://tokenrouter.example.com",
            "secret",
            transport=httpx.MockTransport(handler),
        )
        try:
            users = await client.find_users_by_exact_username("alice")
        finally:
            await client.close()

        self.assertEqual(requested_pages, [1, 2])
        self.assertEqual([user.user_id for user in users], [7])

    async def test_reports_api_error_without_losing_status(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={"code": "INVALID_ADMIN_KEY", "message": "invalid key"},
            )

        client = TokenRouterClient(
            "https://tokenrouter.example.com",
            "bad-secret",
            transport=httpx.MockTransport(handler),
        )
        try:
            with self.assertRaises(TokenRouterError) as raised:
                await client.find_users_by_exact_username("alice")
        finally:
            await client.close()
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.api_code, "INVALID_ADMIN_KEY")


if __name__ == "__main__":
    unittest.main()
