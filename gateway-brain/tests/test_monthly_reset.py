from __future__ import annotations

import sys
from pathlib import Path

from app import database
from seed_club import DEFAULT_STUDENTS

import reset_monthly_tokens


ADA_STUDENT_ID = "stu-ada-lovelace"
GRACE_STUDENT_ID = "stu-grace-hopper"


def test_monthly_reset_clears_totals_and_retains_audit_history(tmp_path: Path) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    database.increment_tokens(str(database_path), ADA_STUDENT_ID, 125)
    database.increment_tokens(str(database_path), GRACE_STUDENT_ID, 25)
    database.set_active(str(database_path), ADA_STUDENT_ID, False)
    original_audit_id = database.record_audit_event(
        str(database_path),
        request_id="req-before-reset",
        student_id=ADA_STUDENT_ID,
        key_preview="sk-p...before",
        token_delta=125,
        route="/v1/chat/completions",
        model="dry-run-minimax",
        status_code=200,
        error_class=None,
    )

    rows = database.reset_monthly_token_totals(
        str(database_path),
        request_id="monthly-reset-2026-07-test",
        period_label="2026-07",
    )

    assert len(rows) == 7
    users = database.list_users(str(database_path))
    assert {user["total_tokens_consumed"] for user in users} == {0}
    ada = next(user for user in users if user["student_id"] == ADA_STUDENT_ID)
    assert ada["is_active"] is False

    audit_events = database.list_audit_events(str(database_path), limit=20)
    assert any(event["id"] == original_audit_id for event in audit_events)
    reset_events = [
        event
        for event in audit_events
        if event["request_id"] == "monthly-reset-2026-07-test"
    ]
    assert len(reset_events) == 7
    assert {event["route"] for event in reset_events} == {"/admin/monthly-reset"}
    assert {event["model"] for event in reset_events} == {"2026-07"}
    assert {event["error_class"] for event in reset_events} == {"monthly_reset"}
    token_deltas = {event["student_id"]: event["token_delta"] for event in reset_events}
    assert token_deltas[ADA_STUDENT_ID] == -125
    assert token_deltas[GRACE_STUDENT_ID] == -25


def test_monthly_reset_script_rejects_invalid_period(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reset_monthly_tokens.py",
            "--database",
            str(tmp_path / "club.db"),
            "--period",
            "2026-13",
        ],
    )

    assert reset_monthly_tokens.main() == 2
    assert "--period must use YYYY-MM format." in capsys.readouterr().out


def test_monthly_reset_script_prints_safe_summary(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    database_path = tmp_path / "club.db"
    database.initialize_database(str(database_path))
    database.upsert_users(str(database_path), DEFAULT_STUDENTS)
    database.increment_tokens(str(database_path), ADA_STUDENT_ID, 7)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reset_monthly_tokens.py",
            "--database",
            str(database_path),
            "--period",
            "2026-07",
        ],
    )

    assert reset_monthly_tokens.main() == 0
    output = capsys.readouterr().out
    assert "Monthly reset complete for 2026-07" in output
    assert "Students reset: 7" in output
    for _, raw_key in DEFAULT_STUDENTS:
        assert raw_key not in output
