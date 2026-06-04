from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app import database
from app.config import Settings
from app.limiter import LimitStatus
from app.main import app, forward_to_portkey, get_app_settings, limiter_from_state
from seed_club import DEFAULT_STUDENTS


ADA_KEY = "sk-poiesis-ada-7f3c9d2a"
ADA_STUDENT_ID = "stu-ada-lovelace"


class AllowingLimiter:
    def __init__(self) -> None:
        self.identities: list[str] = []
        self.peek_identities: list[str] = []

    async def check_and_record(
        self,
        *,
        identity: str,
        tier: str,
        scope: str,
        window_seconds: int,
        limit: int,
    ) -> LimitStatus:
        self.identities.append(identity)
        return LimitStatus(
            scope=scope,
            tier=tier,
            allowed=True,
            count=1,
            limit=limit,
            remaining=limit - 1,
            reset_after_seconds=0,
            window_seconds=window_seconds,
        )

    async def check_and_record_window_and_burst(
        self,
        *,
        identity: str,
        tier: str,
        window_seconds: int,
        window_limit: int,
        burst_seconds: int,
        burst_limit: int,
    ) -> tuple[LimitStatus, LimitStatus]:
        self.identities.append(identity)
        return (
            LimitStatus(
                scope="window",
                tier=tier,
                allowed=True,
                count=1,
                limit=window_limit,
                remaining=window_limit - 1,
                reset_after_seconds=0,
                window_seconds=window_seconds,
            ),
            LimitStatus(
                scope="burst",
                tier=tier,
                allowed=True,
                count=1,
                limit=burst_limit,
                remaining=burst_limit - 1,
                reset_after_seconds=0,
                window_seconds=burst_seconds,
            ),
        )

    async def peek(
        self,
        *,
        identity: str,
        tier: str,
        scope: str,
        window_seconds: int,
        limit: int,
    ) -> LimitStatus:
        self.peek_identities.append(identity)
        return LimitStatus(
            scope=scope,
            tier=tier,
            allowed=True,
            count=0,
            limit=limit,
            remaining=limit,
            reset_after_seconds=0,
            window_seconds=window_seconds,
        )


class ResetRecordingLimiter(AllowingLimiter):
    def __init__(self) -> None:
        super().__init__()
        self.reset_calls: list[dict[str, Any]] = []

    async def reset(self, *, identity: str, tier: str, include_burst: bool = True) -> int:
        self.reset_calls.append(
            {
                "identity": identity,
                "tier": tier,
                "include_burst": include_burst,
            }
        )
        return 1


def _client_for_database(
    database_path: Path,
    limiter: AllowingLimiter | None = None,
) -> TestClient:
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=True,
        poiesis_admin_token="admin-token",
    )
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[limiter_from_state] = lambda: limiter or AllowingLimiter()
    return TestClient(app)


def test_chat_auth_rejects_missing_malformed_unknown_and_inactive_keys(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    user = database.get_user_by_virtual_key(str(database_path), ADA_KEY)
    assert user is not None
    assert user["student_id"] == ADA_STUDENT_ID
    assert user["key_preview"] == database.key_preview(ADA_KEY)
    assert "virtual_key" not in user
    assert "virtual_key_hash" not in user
    database.set_active(str(database_path), ADA_STUDENT_ID, False)

    request_body = {
        "model": "dry-run-minimax",
        "messages": [{"role": "user", "content": "hello"}],
    }
    cases = [
        {},
        {"Authorization": "Basic sk-poiesis-grace-11b09e4c"},
        {"Authorization": "Bearer not-poiesis"},
        {"Authorization": "Bearer sk-poiesis-"},
        {"Authorization": "Bearer sk-poiesis-missing"},
        {"Authorization": f"Bearer {ADA_KEY}"},
    ]

    try:
        client = _client_for_database(database_path)
        for headers in cases:
            response = client.post(
                "/v1/chat/completions",
                headers=headers,
                json=request_body,
            )
            assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_admin_users_payload_hides_raw_keys_and_hashes(tmp_path: Path) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    limiter = AllowingLimiter()

    try:
        client = _client_for_database(database_path, limiter)
        response = client.get(
            "/admin/users",
            headers={"x-admin-token": "admin-token"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    serialized_payload = json.dumps(payload)
    for _, raw_key in DEFAULT_STUDENTS:
        assert raw_key not in serialized_payload
    assert "virtual_key_hash" not in serialized_payload

    ada = next(
        user for user in payload["users"] if user["student_id"] == ADA_STUDENT_ID
    )
    assert ada["student_name"] == "Ada Lovelace"
    assert ada["key_preview"] == database.key_preview(ADA_KEY)
    assert "virtual_key" not in ada
    assert "virtual_key_hash" not in ada
    assert ADA_STUDENT_ID in limiter.peek_identities
    assert ADA_KEY not in limiter.peek_identities


def test_admin_reset_window_endpoint_resets_standard_high_speed_or_all_windows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    limiter = ResetRecordingLimiter()

    try:
        client = _client_for_database(database_path, limiter)
        standard_response = client.post(
            f"/admin/users/{ADA_STUDENT_ID}/reset-window",
            headers={"x-admin-token": "admin-token"},
            json={"tier": "standard", "include_burst": False},
        )
        high_speed_response = client.post(
            f"/admin/users/{ADA_STUDENT_ID}/reset-window",
            headers={"x-admin-token": "admin-token"},
            json={"tier": "high-speed"},
        )
        all_response = client.post(
            f"/admin/users/{ADA_STUDENT_ID}/reset-window",
            headers={"x-admin-token": "admin-token"},
            json={"tier": "all", "include_burst": True},
        )
    finally:
        app.dependency_overrides.clear()

    assert standard_response.status_code == 200
    assert high_speed_response.status_code == 200
    assert all_response.status_code == 200
    assert standard_response.json()["deleted_keys"] == 1
    assert high_speed_response.json()["deleted_keys"] == 1
    assert all_response.json()["deleted_keys"] == 2
    assert all_response.json()["student_id"] == ADA_STUDENT_ID
    assert all_response.json()["key_preview"] == database.key_preview(ADA_KEY)
    assert limiter.reset_calls == [
        {
            "identity": ADA_STUDENT_ID,
            "tier": "standard",
            "include_burst": False,
        },
        {
            "identity": ADA_STUDENT_ID,
            "tier": "high-speed",
            "include_burst": True,
        },
        {
            "identity": ADA_STUDENT_ID,
            "tier": "standard",
            "include_burst": True,
        },
        {
            "identity": ADA_STUDENT_ID,
            "tier": "high-speed",
            "include_burst": True,
        },
    ]


def test_admin_reset_window_endpoint_rejects_unknown_student_without_reset(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    limiter = ResetRecordingLimiter()

    try:
        client = _client_for_database(database_path, limiter)
        response = client.post(
            "/admin/users/stu-missing/reset-window",
            headers={"x-admin-token": "admin-token"},
            json={"tier": "all"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["detail"]["message"] == "Unknown student ID."
    assert limiter.reset_calls == []


def test_admin_adjust_tokens_applies_positive_and_negative_deltas(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    database.increment_tokens(str(database_path), ADA_STUDENT_ID, 100)

    try:
        client = _client_for_database(database_path)
        positive_response = client.post(
            f"/admin/users/{ADA_STUDENT_ID}/adjust-tokens",
            headers={"x-admin-token": "admin-token"},
            json={"delta_tokens": 25},
        )
        negative_response = client.post(
            f"/admin/users/{ADA_STUDENT_ID}/adjust-tokens",
            headers={"x-admin-token": "admin-token"},
            json={"delta_tokens": -40},
        )
    finally:
        app.dependency_overrides.clear()

    assert positive_response.status_code == 200
    assert positive_response.json() == {
        "ok": True,
        "student_id": ADA_STUDENT_ID,
        "key_preview": database.key_preview(ADA_KEY),
        "total_tokens_consumed": 125,
        "tokens_remaining": 185_699_875,
    }
    assert negative_response.status_code == 200
    assert negative_response.json()["total_tokens_consumed"] == 85
    assert negative_response.json()["tokens_remaining"] == 185_699_915

    user = database.get_user_by_student_id(str(database_path), ADA_STUDENT_ID)
    assert user is not None
    assert user["total_tokens_consumed"] == 85


def test_admin_adjust_tokens_clamps_negative_totals_at_zero(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    database.increment_tokens(str(database_path), ADA_STUDENT_ID, 12)

    try:
        client = _client_for_database(database_path)
        response = client.post(
            f"/admin/users/{ADA_STUDENT_ID}/adjust-tokens",
            headers={"x-admin-token": "admin-token"},
            json={"delta_tokens": -999},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["total_tokens_consumed"] == 0
    assert response.json()["tokens_remaining"] == 185_700_000

    user = database.get_user_by_student_id(str(database_path), ADA_STUDENT_ID)
    assert user is not None
    assert user["total_tokens_consumed"] == 0


def test_admin_adjust_tokens_rejects_unknown_student_without_adjustment(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)

    try:
        client = _client_for_database(database_path)
        response = client.post(
            "/admin/users/stu-missing/adjust-tokens",
            headers={"x-admin-token": "admin-token"},
            json={"delta_tokens": 25},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["detail"]["message"] == "Unknown student ID."


def test_forward_to_portkey_uses_master_credential_not_student_key(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def post(
            self,
            target: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            captured["target"] = target
            captured["headers"] = headers
            captured["payload"] = json
            return httpx.Response(200, json={"usage": {"total_tokens": 1}})

    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    settings = Settings(
        _env_file=None,
        portkey_base_url="http://portkey:8787",
        portkey_provider="minimax",
        PORTKEY_UPSTREAM_API_KEY="master-secret",
    )
    student_key = "sk-poiesis-grace-11b09e4c"
    student_id = "stu-grace-hopper"
    key_preview = database.key_preview(student_key)
    request_id = "req-direct-forward-0001"

    response = asyncio.run(
        forward_to_portkey(
            payload={"model": "dry-run-minimax"},
            settings=settings,
            student_id=student_id,
            key_preview=key_preview,
            request_id=request_id,
            tier="standard",
        )
    )

    assert response.status_code == 200
    assert captured["target"] == "http://portkey:8787/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer master-secret"
    assert captured["headers"]["x-portkey-provider"] == "minimax"
    metadata = json.loads(captured["headers"]["x-portkey-metadata"])
    assert metadata == {
        "student_id": student_id,
        "key_preview": key_preview,
        "request_id": request_id,
        "tier": "standard",
    }
    assert "virtual_key" not in metadata
    assert "virtual_key_hash" not in metadata
    assert student_key not in json.dumps(captured["headers"])


def test_chat_forwarding_threads_request_id_to_logs_headers_and_portkey(
    tmp_path: Path,
    monkeypatch: Any,
    caplog: Any,
) -> None:
    captured: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def post(
            self,
            target: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            captured["target"] = target
            captured["headers"] = headers
            captured["payload"] = json
            return httpx.Response(200, json={"usage": {"total_tokens": 3}})

    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=False,
        portkey_base_url="http://portkey:8787",
        portkey_provider="minimax",
        PORTKEY_UPSTREAM_API_KEY="master-secret",
        poiesis_admin_token="admin-token",
    )
    request_id = "req-route-forward-0001"

    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    caplog.set_level(logging.INFO, logger="poiesis.gateway")
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[limiter_from_state] = lambda: AllowingLimiter()
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {ADA_KEY}",
                "x-request-id": request_id,
            },
            json={
                "model": "dry-run-minimax",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["x-request-id"] == request_id
    assert captured["headers"]["Authorization"] == "Bearer master-secret"
    metadata = json.loads(captured["headers"]["x-portkey-metadata"])
    assert metadata["request_id"] == request_id
    assert metadata["student_id"] == ADA_STUDENT_ID
    assert ADA_KEY not in json.dumps(captured["headers"])
    assert ADA_KEY not in json.dumps(captured["payload"])
    assert request_id in caplog.text
    assert ADA_STUDENT_ID in caplog.text

    user = database.get_user_by_student_id(str(database_path), ADA_STUDENT_ID)
    assert user is not None
    assert user["total_tokens_consumed"] == 3


def test_chat_forwarding_records_prompt_and_completion_usage_once(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class UsageAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> UsageAsyncClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def post(
            self,
            target: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-usage",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 6,
                    },
                },
            )

    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=False,
        portkey_base_url="http://portkey:8787",
        portkey_provider="minimax",
        PORTKEY_UPSTREAM_API_KEY="master-secret",
        poiesis_admin_token="admin-token",
    )

    monkeypatch.setattr("app.main.httpx.AsyncClient", UsageAsyncClient)
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[limiter_from_state] = lambda: AllowingLimiter()
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {ADA_KEY}"},
            json={
                "model": "dry-run-minimax",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    user = database.get_user_by_student_id(str(database_path), ADA_STUDENT_ID)
    assert user is not None
    assert user["total_tokens_consumed"] == 10


def test_final_portkey_response_after_retries_is_accounted_once(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class RetriedUpstreamAsyncClient:
        calls = 0

        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> RetriedUpstreamAsyncClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def post(
            self,
            target: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            RetriedUpstreamAsyncClient.calls += 1
            return httpx.Response(
                200,
                headers={"x-portkey-retry-count": "2"},
                json={
                    "id": "chatcmpl-retried",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"total_tokens": 11},
                },
            )

    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=False,
        portkey_base_url="http://portkey:8787",
        portkey_provider="minimax",
        PORTKEY_UPSTREAM_API_KEY="master-secret",
        poiesis_admin_token="admin-token",
    )

    monkeypatch.setattr("app.main.httpx.AsyncClient", RetriedUpstreamAsyncClient)
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[limiter_from_state] = lambda: AllowingLimiter()
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {ADA_KEY}"},
            json={
                "model": "dry-run-minimax",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert RetriedUpstreamAsyncClient.calls == 1
    user = database.get_user_by_student_id(str(database_path), ADA_STUDENT_ID)
    assert user is not None
    assert user["total_tokens_consumed"] == 11


def test_chat_forwarding_missing_usage_returns_502_without_token_update(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class MissingUsageAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> MissingUsageAsyncClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def post(
            self,
            target: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-missing-usage",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=False,
        portkey_base_url="http://portkey:8787",
        portkey_provider="minimax",
        PORTKEY_UPSTREAM_API_KEY="master-secret",
        poiesis_admin_token="admin-token",
    )
    request_id = "req-missing-usage-0001"

    monkeypatch.setattr("app.main.httpx.AsyncClient", MissingUsageAsyncClient)
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[limiter_from_state] = lambda: AllowingLimiter()
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {ADA_KEY}",
                "x-request-id": request_id,
            },
            json={
                "model": "dry-run-minimax",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert response.headers["x-request-id"] == request_id
    detail = response.json()["detail"]
    assert detail["message"] == "Upstream response did not include usable usage; token usage was not updated."
    assert detail["key_preview"] == database.key_preview(ADA_KEY)
    assert detail["error_class"] == "missing_usage"

    user = database.get_user_by_student_id(str(database_path), ADA_STUDENT_ID)
    assert user is not None
    assert user["total_tokens_consumed"] == 0


def test_chat_forwarding_timeout_returns_504_without_token_update(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class TimeoutAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> TimeoutAsyncClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def post(
            self,
            target: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=False,
        portkey_base_url="http://portkey:8787",
        portkey_provider="minimax",
        PORTKEY_UPSTREAM_API_KEY="master-secret",
        poiesis_admin_token="admin-token",
    )
    request_id = "req-timeout-forward-0001"

    monkeypatch.setattr("app.main.httpx.AsyncClient", TimeoutAsyncClient)
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[limiter_from_state] = lambda: AllowingLimiter()
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {ADA_KEY}",
                "x-request-id": request_id,
            },
            json={
                "model": "dry-run-minimax",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 504
    assert response.headers["x-request-id"] == request_id
    detail = response.json()["detail"]
    assert detail["message"] == "Upstream request timed out; token usage was not updated."
    assert detail["key_preview"] == database.key_preview(ADA_KEY)
    assert detail["error_class"] == "upstream_timeout"

    user = database.get_user_by_student_id(str(database_path), ADA_STUDENT_ID)
    assert user is not None
    assert user["total_tokens_consumed"] == 0


def test_chat_forwarding_request_error_returns_502_without_token_update(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class FailingAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> FailingAsyncClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def post(
            self,
            target: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            raise httpx.ConnectError("portkey unavailable")

    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=False,
        portkey_base_url="http://portkey:8787",
        portkey_provider="minimax",
        PORTKEY_UPSTREAM_API_KEY="master-secret",
        poiesis_admin_token="admin-token",
    )
    request_id = "req-error-forward-0001"

    monkeypatch.setattr("app.main.httpx.AsyncClient", FailingAsyncClient)
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[limiter_from_state] = lambda: AllowingLimiter()
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {ADA_KEY}",
                "x-request-id": request_id,
            },
            json={
                "model": "dry-run-minimax",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert response.headers["x-request-id"] == request_id
    detail = response.json()["detail"]
    assert detail["message"] == "Upstream request failed; token usage was not updated."
    assert detail["key_preview"] == database.key_preview(ADA_KEY)
    assert detail["error_class"] == "upstream_request_error"

    user = database.get_user_by_student_id(str(database_path), ADA_STUDENT_ID)
    assert user is not None
    assert user["total_tokens_consumed"] == 0
