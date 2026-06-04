from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app import database
from app.config import Settings
from app.limiter import LimitStatus
from app.main import app, get_app_settings, limiter_from_state
from seed_club import DEFAULT_STUDENTS, main as seed_main


class AllowingLimiter:
    def __init__(self) -> None:
        self.calls = 0

    async def check_and_record(
        self,
        *,
        virtual_key: str,
        tier: str,
        scope: str,
        window_seconds: int,
        limit: int,
    ) -> LimitStatus:
        self.calls += 1
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
        self.calls += 1
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


def test_seed_script_creates_exactly_seven_active_students(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    database_path = tmp_path / "club.db"

    monkeypatch.setattr(
        sys,
        "argv",
        ["seed_club.py", "--database", str(database_path)],
    )
    seed_main()

    users = database.list_users(str(database_path))
    assert len(users) == 7
    assert {user["student_name"] for user in users} == {
        student_name for student_name, _ in DEFAULT_STUDENTS
    }
    for user in users:
        assert user["virtual_key"].startswith("sk-poiesis-")
        assert user["total_tokens_consumed"] == 0
        assert user["is_active"] is True


def test_dry_run_chat_completion_is_openai_compatible_and_records_usage(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=True,
        poiesis_admin_token="admin-token",
    )

    limiter = AllowingLimiter()
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[limiter_from_state] = lambda: limiter
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-poiesis-ada-7f3c9d2a"},
            json={
                "model": "dry-run-minimax",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["role"] == "assistant"
    assert payload["usage"]["total_tokens"] > 0

    user = database.get_user(str(database_path), "sk-poiesis-ada-7f3c9d2a")
    assert user is not None
    assert user["total_tokens_consumed"] == payload["usage"]["total_tokens"]


def test_streaming_is_rejected_before_rate_limit_recording(tmp_path: Path) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=True,
        poiesis_admin_token="admin-token",
    )

    limiter = AllowingLimiter()
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[limiter_from_state] = lambda: limiter
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-poiesis-ada-7f3c9d2a"},
            json={
                "model": "dry-run-minimax",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert "Streaming is disabled" in response.json()["detail"]
    assert limiter.calls == 0
