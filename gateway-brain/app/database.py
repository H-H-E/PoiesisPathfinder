from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_KEY_HASH_SECRET = "replace-with-local-key-hash-secret"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    student_id TEXT PRIMARY KEY,
    virtual_key_hash TEXT NOT NULL UNIQUE,
    key_preview TEXT NOT NULL,
    student_name TEXT NOT NULL,
    total_tokens_consumed INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active);
CREATE INDEX IF NOT EXISTS idx_users_virtual_key_hash ON users(virtual_key_hash);
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


def key_preview(virtual_key: str) -> str:
    if len(virtual_key) <= 18:
        return f"{virtual_key[:11]}..."
    return f"{virtual_key[:11]}...{virtual_key[-6:]}"


def virtual_key_hash(virtual_key: str, key_hash_secret: str = DEFAULT_KEY_HASH_SECRET) -> str:
    return hmac.new(
        key_hash_secret.encode("utf-8"),
        virtual_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def student_id_for_name(student_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", student_name.lower()).strip("-")
    return f"stu-{slug or 'student'}"


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def _migrate_legacy_users(connection: sqlite3.Connection, key_hash_secret: str) -> None:
    columns = _table_columns(connection, "users")
    if not columns:
        return
    if "virtual_key" not in columns:
        if {"student_id", "virtual_key_hash", "key_preview"}.issubset(columns):
            return
        raise RuntimeError("Unsupported users table schema; cannot migrate safely.")

    selected_columns = [
        "virtual_key",
        "student_name",
        "total_tokens_consumed",
        "is_active",
        "created_at",
        "updated_at",
    ]
    if "student_id" in columns:
        selected_columns.append("student_id")
    rows = connection.execute(
        f"""
        SELECT {", ".join(selected_columns)}
        FROM users
        ORDER BY student_name ASC
        """
    ).fetchall()
    connection.execute("ALTER TABLE users RENAME TO users_legacy_raw_keys;")
    connection.executescript(SCHEMA)
    for row in rows:
        row_data = dict(row)
        student_id = str(row_data.get("student_id") or student_id_for_name(str(row["student_name"])))
        virtual_key = str(row["virtual_key"])
        connection.execute(
            """
            INSERT INTO users (
                student_id,
                virtual_key_hash,
                key_preview,
                student_name,
                total_tokens_consumed,
                is_active,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                student_id,
                virtual_key_hash(virtual_key, key_hash_secret),
                key_preview(virtual_key),
                row["student_name"],
                row["total_tokens_consumed"],
                row["is_active"],
                row["created_at"],
                row["updated_at"],
            ),
        )
    connection.execute("DROP TABLE users_legacy_raw_keys;")


def initialize_database(
    database_path: str,
    key_hash_secret: str = DEFAULT_KEY_HASH_SECRET,
) -> None:
    with connect(database_path) as connection:
        _migrate_legacy_users(connection, key_hash_secret)
        connection.executescript(SCHEMA)
        connection.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    data["is_active"] = bool(data["is_active"])
    return data


def get_user_by_virtual_key(
    database_path: str,
    virtual_key: str,
    key_hash_secret: str = DEFAULT_KEY_HASH_SECRET,
) -> dict[str, Any] | None:
    key_hash = virtual_key_hash(virtual_key, key_hash_secret)
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT student_id, key_preview, student_name, total_tokens_consumed, is_active, created_at, updated_at
            FROM users
            WHERE virtual_key_hash = ?
            """,
            (key_hash,),
        ).fetchone()
    return row_to_dict(row)


def get_user_by_student_id(database_path: str, student_id: str) -> dict[str, Any] | None:
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT student_id, key_preview, student_name, total_tokens_consumed, is_active, created_at, updated_at
            FROM users
            WHERE student_id = ?
            """,
            (student_id,),
        ).fetchone()
    return row_to_dict(row)


def list_users(database_path: str) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT student_id, key_preview, student_name, total_tokens_consumed, is_active, created_at, updated_at
            FROM users
            ORDER BY student_name ASC
            """
        ).fetchall()
    return [row_to_dict(row) for row in rows if row is not None]


def upsert_users(
    database_path: str,
    users: Iterable[tuple[str, str]],
    key_hash_secret: str = DEFAULT_KEY_HASH_SECRET,
) -> None:
    now = utc_now()
    with connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO users (
                student_id,
                virtual_key_hash,
                key_preview,
                student_name,
                total_tokens_consumed,
                is_active,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 0, 1, ?, ?)
            ON CONFLICT(student_id) DO UPDATE SET
                virtual_key_hash = excluded.virtual_key_hash,
                key_preview = excluded.key_preview,
                student_name = excluded.student_name,
                updated_at = excluded.updated_at
            """,
            [
                (
                    student_id_for_name(student_name),
                    virtual_key_hash(virtual_key, key_hash_secret),
                    key_preview(virtual_key),
                    student_name,
                    now,
                    now,
                )
                for student_name, virtual_key in users
            ],
        )
        connection.commit()


def increment_tokens(database_path: str, student_id: str, token_delta: int) -> int:
    now = utc_now()
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE;")
        connection.execute(
            """
            UPDATE users
            SET total_tokens_consumed = MAX(total_tokens_consumed + ?, 0),
                updated_at = ?
            WHERE student_id = ?
            """,
            (token_delta, now, student_id),
        )
        row = connection.execute(
            "SELECT total_tokens_consumed FROM users WHERE student_id = ?",
            (student_id,),
        ).fetchone()
        connection.commit()
    if row is None:
        raise KeyError(f"Unknown student ID: {student_id}")
    return int(row["total_tokens_consumed"])


def set_active(database_path: str, student_id: str, active: bool) -> None:
    now = utc_now()
    with connect(database_path) as connection:
        connection.execute(
            """
            UPDATE users
            SET is_active = ?,
                updated_at = ?
            WHERE student_id = ?
            """,
            (1 if active else 0, now, student_id),
        )
        connection.commit()


def deactivate_if_spent(database_path: str, student_id: str, ceiling: int) -> bool:
    now = utc_now()
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE;")
        row = connection.execute(
            "SELECT total_tokens_consumed FROM users WHERE student_id = ?",
            (student_id,),
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
                WHERE student_id = ?
                """,
                (now, student_id),
            )
        connection.commit()
    return spent


def database_exists(database_path: str) -> bool:
    return os.path.exists(database_path)
