from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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


class BlockingLimiter:
    def __init__(self) -> None:
        self.calls = 0

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
        return (
            LimitStatus(
                scope="window",
                tier=tier,
                allowed=False,
                count=window_limit,
                limit=window_limit,
                remaining=0,
                reset_after_seconds=42,
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


def test_random_seed_creates_non_demo_keys_and_persists_only_safe_previews(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    database_path = tmp_path / "club.db"
    generated_tokens = [f"generated-token-{index}" for index in range(7)]
    generated_keys = [f"sk-poiesis-{token}" for token in generated_tokens]

    monkeypatch.setattr(
        sys,
        "argv",
        ["seed_club.py", "--database", str(database_path), "--random"],
    )
    monkeypatch.setattr("seed_club.secrets.token_urlsafe", lambda _: generated_tokens.pop(0))

    seed_main()

    output = capsys.readouterr().out
    users = database.list_users(str(database_path))
    serialized_users = json.dumps(users)

    assert len(users) == 7
    for _, demo_key in DEFAULT_STUDENTS:
        assert demo_key not in output
        assert demo_key not in serialized_users
    for generated_key in generated_keys:
        assert generated_key in output
        assert generated_key not in serialized_users
    for user in users:
        assert user["key_preview"].startswith("sk-poiesis-")
        assert "..." in user["key_preview"]
        assert "virtual_key" not in user
        assert "virtual_key_hash" not in user


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


def test_concurrent_token_usage_records_do_not_lose_sqlite_updates(tmp_path: Path) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    increments = [7] * 24

    def record(delta: int) -> int:
        total, _ = database.record_token_usage(
            str(database_path),
            ADA_STUDENT_ID,
            token_delta=delta,
            ceiling=1_000_000,
        )
        return total

    with ThreadPoolExecutor(max_workers=8) as executor:
        totals = list(executor.map(record, increments))

    user = database.get_user_by_student_id(str(database_path), ADA_STUDENT_ID)
    assert user is not None
    assert user["total_tokens_consumed"] == sum(increments)
    assert max(totals) == sum(increments)
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


def test_global_model_allowlist_blocks_unlisted_model_before_rate_limit_recording(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=True,
        poiesis_allowed_models="dry-run-minimax",
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
                "model": "minimax-expensive",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["message"] == "Model is not allowed for this student."
    assert detail["model"] == "minimax-expensive"
    assert detail["allowed_models"] == ["dry-run-minimax"]
    assert detail["key_preview"] == database.key_preview(ADA_KEY)
    assert limiter.calls == 0


def test_student_model_allowlist_overrides_global_allowlist(tmp_path: Path) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=True,
        poiesis_allowed_models="dry-run-minimax,minimax-large",
        poiesis_student_allowed_models=f"{ADA_STUDENT_ID}=dry-run-minimax",
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
                "model": "minimax-large",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["message"] == "Model is not allowed for this student."
    assert detail["model"] == "minimax-large"
    assert detail["allowed_models"] == ["dry-run-minimax"]
    assert limiter.calls == 0


def test_student_model_blocklist_blocks_model_and_writes_audit_event(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=True,
        poiesis_student_blocked_models=f"{ADA_STUDENT_ID}=dry-run-minimax",
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

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["message"] == "Model is blocked for this student."
    assert detail["model"] == "dry-run-minimax"
    assert detail["error_class"] == "model_blocked"
    assert limiter.calls == 0

    events = database.list_audit_events(str(database_path), student_id=ADA_STUDENT_ID, limit=1)
    assert len(events) == 1
    assert events[0]["model"] == "dry-run-minimax"
    assert events[0]["status_code"] == 403
    assert events[0]["error_class"] == "model_blocked"
    assert events[0]["token_delta"] == 0


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
    spent_detail = second_response.json()["detail"]
    assert spent_detail["message"] == "Monthly token budget spent."
    assert spent_detail["key_preview"] == database.key_preview(ADA_KEY)
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
    stream_detail = response.json()["detail"]
    assert "Streaming is disabled" in stream_detail["message"]
    assert stream_detail["key_preview"] == database.key_preview(ADA_KEY)
    assert limiter.calls == 0


def test_rate_limit_error_payload_is_structured_with_key_preview(tmp_path: Path) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    settings = Settings(
        _env_file=None,
        database_path=str(database_path),
        dry_run_upstream=True,
        standard_window_limit=2,
        poiesis_admin_token="admin-token",
    )

    limiter = BlockingLimiter()
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

    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["message"].startswith("Rate limit exceeded.")
    assert detail["key_preview"] == database.key_preview(ADA_KEY)
    assert detail["scope"] == "window"
    assert detail["limit"] == settings.standard_window_limit
    assert detail["remaining"] == 0
    assert detail["reset_after_seconds"] == 42
    assert limiter.calls == 1


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
    malformed_detail = response.json()["detail"]
    assert "messages list" in malformed_detail["message"]
    assert malformed_detail["key_preview"] == database.key_preview(ADA_KEY)
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
    body_detail = response.json()["detail"]
    assert "Request body exceeds" in body_detail["message"]
    assert body_detail["key_preview"] == database.key_preview(ADA_KEY)
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
    content_detail = response.json()["detail"]
    assert "content exceeds" in content_detail["message"]
    assert content_detail["key_preview"] == database.key_preview(ADA_KEY)
    assert content_detail["limit"] == settings.max_message_content_chars
    assert limiter.calls == 0
