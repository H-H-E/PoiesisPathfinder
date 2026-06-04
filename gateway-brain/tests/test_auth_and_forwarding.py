from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app import database
from app.config import Settings
from app.limiter import LimitStatus
from app.main import app, forward_to_portkey, get_app_settings, limiter_from_state
from seed_club import DEFAULT_STUDENTS


class AllowingLimiter:
    async def check_and_record(
        self,
        *,
        virtual_key: str,
        tier: str,
        scope: str,
        window_seconds: int,
        limit: int,
    ) -> LimitStatus:
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
        virtual_key: str,
        tier: str,
        window_seconds: int,
        window_limit: int,
        burst_seconds: int,
        burst_limit: int,
    ) -> tuple[LimitStatus, LimitStatus]:
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


def _client_for_database(database_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=True,
        poiesis_admin_token="admin-token",
    )
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[limiter_from_state] = lambda: AllowingLimiter()
    return TestClient(app)


def test_chat_auth_rejects_missing_malformed_unknown_and_inactive_keys(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    database.set_active(str(database_path), "sk-poiesis-ada-7f3c9d2a", False)

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
        {"Authorization": "Bearer sk-poiesis-ada-7f3c9d2a"},
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

    response = asyncio.run(
        forward_to_portkey(
            payload={"model": "dry-run-minimax"},
            settings=settings,
            virtual_key=student_key,
            student_name="Grace Hopper",
            tier="standard",
        )
    )

    assert response.status_code == 200
    assert captured["target"] == "http://portkey:8787/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer master-secret"
    assert captured["headers"]["x-portkey-provider"] == "minimax"
    assert student_key not in json.dumps(captured["headers"])
