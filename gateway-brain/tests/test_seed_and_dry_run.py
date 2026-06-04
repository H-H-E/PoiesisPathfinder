from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app import database
from app.config import Settings
from app.limiter import LimitStatus
from app.main import app, get_app_settings, limiter_from_state
from seed_club import DEFAULT_STUDENTS, main as seed_main


ADA_KEY = "sk-poiesis-ada-7f3c9d2a"
ADA_STUDENT_ID = "stu-ada-lovelace"


class AllowingLimiter:
    def __init__(self) -> None:
        self.calls = 0
        self.identities: list[str] = []
        self.tiers: list[str] = []

    async def check_and_record(
        self,
        *,
        identity: str,
        tier: str,
        scope: str,
        window_seconds: int,
        limit: int,
    ) -> LimitStatus:
        self.calls += 1
        self.identities.append(identity)
        self.tiers.append(tier)
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
        self.calls += 1
        self.identities.append(identity)
        self.tiers.append(tier)
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
    serialized_users = json.dumps(users)
    assert len(users) == 7
    assert {user["student_name"] for user in users} == {
        student_name for student_name, _ in DEFAULT_STUDENTS
    }
    for _, raw_key in DEFAULT_STUDENTS:
        assert raw_key not in serialized_users
    for user in users:
        assert "virtual_key" not in user
        assert "virtual_key_hash" not in user
        assert user["student_id"].startswith("stu-")
        assert user["key_preview"].startswith("sk-poiesis-")
        assert "..." in user["key_preview"]
        assert user["total_tokens_consumed"] == 0
        assert user["is_active"] is True


def test_short_key_preview_never_exposes_full_key() -> None:
    short_key = "sk-poiesis-a"

    preview = database.key_preview(short_key)

    assert preview == "sk-poiesis-..."
    assert short_key not in preview
    assert database.key_preview(ADA_KEY) != ADA_KEY


def test_migration_drops_legacy_virtual_key_column_from_mixed_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "club.db"
    now = database.utc_now()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE users (
                student_id TEXT PRIMARY KEY,
                virtual_key TEXT NOT NULL UNIQUE,
                virtual_key_hash TEXT NOT NULL UNIQUE,
                key_preview TEXT NOT NULL,
                student_name TEXT NOT NULL,
                total_tokens_consumed INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO users (
                student_id,
                virtual_key,
                virtual_key_hash,
                key_preview,
                student_name,
                total_tokens_consumed,
                is_active,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ADA_STUDENT_ID,
                ADA_KEY,
                database.virtual_key_hash(ADA_KEY),
                database.key_preview(ADA_KEY),
                "Ada Lovelace",
                42,
                1,
                now,
                now,
            ),
        )
        connection.commit()

    database.initialize_database(str(database_path))

    with database.connect(str(database_path)) as connection:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
    assert "virtual_key" not in columns
    assert {"student_id", "virtual_key_hash", "key_preview"}.issubset(columns)

    user = database.get_user_by_virtual_key(str(database_path), ADA_KEY)
    assert user is not None
    assert user["student_id"] == ADA_STUDENT_ID
    assert user["total_tokens_consumed"] == 42
    assert ADA_KEY not in json.dumps(database.list_users(str(database_path)))


def test_record_token_usage_deactivates_when_ceiling_is_reached(tmp_path: Path) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    database.increment_tokens(str(database_path), ADA_STUDENT_ID, 9)

    total, is_active = database.record_token_usage(
        str(database_path),
        ADA_STUDENT_ID,
        token_delta=1,
        ceiling=10,
    )

    assert total == 10
    assert is_active is False
    user = database.get_user_by_student_id(str(database_path), ADA_STUDENT_ID)
    assert user is not None
    assert user["total_tokens_consumed"] == 10
    assert user["is_active"] is False


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
            headers={"Authorization": f"Bearer {ADA_KEY}"},
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
    assert limiter.identities == [ADA_STUDENT_ID]

    user = database.get_user_by_virtual_key(
        str(database_path),
        ADA_KEY,
        settings.poiesis_key_hash_secret,
    )
    assert user is not None
    assert user["student_id"] == ADA_STUDENT_ID
    assert "virtual_key" not in user
    assert "virtual_key_hash" not in user
    assert user["total_tokens_consumed"] == payload["usage"]["total_tokens"]


def test_student_supplied_tier_hints_are_ignored(tmp_path: Path) -> None:
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
            headers={
                "Authorization": f"Bearer {ADA_KEY}",
                "x-poiesis-tier": "high-speed",
                "x-request-tier": "high-speed",
            },
            json={
                "model": "dry-run-minimax",
                "poiesis_tier": "high-speed",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert limiter.identities == [ADA_STUDENT_ID]
    assert limiter.tiers == ["standard"]


def test_high_speed_tier_comes_from_server_side_student_policy(tmp_path: Path) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=True,
        high_speed_student_ids=f" {ADA_STUDENT_ID} ",
        poiesis_admin_token="admin-token",
    )

    limiter = AllowingLimiter()
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[limiter_from_state] = lambda: limiter
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
    assert limiter.identities == [ADA_STUDENT_ID]
    assert limiter.tiers == ["high-speed"]


def test_dry_run_completion_that_crosses_ceiling_locks_future_requests(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=True,
        monthly_token_ceiling=1,
        poiesis_admin_token="admin-token",
    )

    limiter = AllowingLimiter()
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[limiter_from_state] = lambda: limiter
    try:
        client = TestClient(app)
        first_response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {ADA_KEY}"},
            json={
                "model": "dry-run-minimax",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        second_response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {ADA_KEY}"},
            json={
                "model": "dry-run-minimax",
                "messages": [{"role": "user", "content": "hello again"}],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert first_response.status_code == 200
    assert first_response.json()["usage"]["total_tokens"] >= settings.monthly_token_ceiling
    assert second_response.status_code == 403
    assert second_response.json()["detail"] == "Monthly token budget spent."
    assert limiter.calls == 1

    user = database.get_user_by_student_id(str(database_path), ADA_STUDENT_ID)
    assert user is not None
    assert user["total_tokens_consumed"] >= settings.monthly_token_ceiling
    assert user["is_active"] is False


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
            headers={"Authorization": f"Bearer {ADA_KEY}"},
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


def test_malformed_messages_are_rejected_before_rate_limit_recording(tmp_path: Path) -> None:
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
            headers={"Authorization": f"Bearer {ADA_KEY}"},
            json={"model": "dry-run-minimax", "messages": "hello"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert "messages list" in response.json()["detail"]
    assert limiter.calls == 0


def test_oversized_request_body_is_rejected_before_rate_limit_recording(tmp_path: Path) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=True,
        max_request_body_bytes=120,
        poiesis_admin_token="admin-token",
    )

    limiter = AllowingLimiter()
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[limiter_from_state] = lambda: limiter
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {ADA_KEY}"},
            json={
                "model": "dry-run-minimax",
                "messages": [{"role": "user", "content": "x" * 200}],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 413
    assert "Request body exceeds" in response.json()["detail"]
    assert limiter.calls == 0


def test_oversized_message_content_is_rejected_before_rate_limit_recording(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=True,
        max_message_content_chars=4,
        poiesis_admin_token="admin-token",
    )

    limiter = AllowingLimiter()
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[limiter_from_state] = lambda: limiter
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

    assert response.status_code == 413
    assert "content exceeds" in response.json()["detail"]
    assert limiter.calls == 0
