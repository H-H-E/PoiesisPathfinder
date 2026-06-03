from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    virtual_key TEXT PRIMARY KEY,
    student_name TEXT NOT NULL,
    total_tokens_consumed INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ensure_parent(database_path: str) -> None:
    parent = Path(database_path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)


def connect(database_path: str) -> sqlite3.Connection:
    _ensure_parent(database_path)
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL;")
    connection.execute("PRAGMA busy_timeout=30000;")
    connection.execute("PRAGMA foreign_keys=ON;")
    return connection


def initialize_database(database_path: str) -> None:
    with connect(database_path) as connection:
        connection.executescript(SCHEMA)
        connection.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    data["is_active"] = bool(data["is_active"])
    return data


def get_user(database_path: str, virtual_key: str) -> dict[str, Any] | None:
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT virtual_key, student_name, total_tokens_consumed, is_active, created_at, updated_at
            FROM users
            WHERE virtual_key = ?
            """,
            (virtual_key,),
        ).fetchone()
    return row_to_dict(row)


def list_users(database_path: str) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT virtual_key, student_name, total_tokens_consumed, is_active, created_at, updated_at
            FROM users
            ORDER BY student_name ASC
            """
        ).fetchall()
    return [row_to_dict(row) for row in rows if row is not None]


def upsert_users(database_path: str, users: Iterable[tuple[str, str]]) -> None:
    now = utc_now()
    with connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO users (virtual_key, student_name, total_tokens_consumed, is_active, created_at, updated_at)
            VALUES (?, ?, 0, 1, ?, ?)
            ON CONFLICT(virtual_key) DO UPDATE SET
                student_name = excluded.student_name,
                updated_at = excluded.updated_at
            """,
            [(virtual_key, student_name, now, now) for student_name, virtual_key in users],
        )
        connection.commit()


def increment_tokens(database_path: str, virtual_key: str, token_delta: int) -> int:
    now = utc_now()
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE;")
        connection.execute(
            """
            UPDATE users
            SET total_tokens_consumed = MAX(total_tokens_consumed + ?, 0),
                updated_at = ?
            WHERE virtual_key = ?
            """,
            (token_delta, now, virtual_key),
        )
        row = connection.execute(
            "SELECT total_tokens_consumed FROM users WHERE virtual_key = ?",
            (virtual_key,),
        ).fetchone()
        connection.commit()
    if row is None:
        raise KeyError(f"Unknown virtual key: {virtual_key}")
    return int(row["total_tokens_consumed"])


def set_active(database_path: str, virtual_key: str, active: bool) -> None:
    now = utc_now()
    with connect(database_path) as connection:
        connection.execute(
            """
            UPDATE users
            SET is_active = ?,
                updated_at = ?
            WHERE virtual_key = ?
            """,
            (1 if active else 0, now, virtual_key),
        )
        connection.commit()


def deactivate_if_spent(database_path: str, virtual_key: str, ceiling: int) -> bool:
    now = utc_now()
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE;")
        row = connection.execute(
            "SELECT total_tokens_consumed FROM users WHERE virtual_key = ?",
            (virtual_key,),
        ).fetchone()
        if row is None:
            connection.commit()
            return False
        spent = int(row["total_tokens_consumed"]) >= ceiling
        if spent:
            connection.execute(
                """
                UPDATE users
                SET is_active = 0,
                    updated_at = ?
                WHERE virtual_key = ?
                """,
                (now, virtual_key),
            )
        connection.commit()
    return spent


def database_exists(database_path: str) -> bool:
    return os.path.exists(database_path)
