from __future__ import annotations

import argparse
import secrets
from pathlib import Path

from app.database import initialize_database, list_users, upsert_users


DEFAULT_STUDENTS = [
    ("Ada Lovelace", "sk-poiesis-ada-7f3c9d2a"),
    ("Grace Hopper", "sk-poiesis-grace-11b09e4c"),
    ("Katherine Johnson", "sk-poiesis-katherine-88d41a0b"),
    ("Dorothy Vaughan", "sk-poiesis-dorothy-452d0baf"),
    ("Margaret Hamilton", "sk-poiesis-margaret-c6ee2842"),
    ("James Baldwin", "sk-poiesis-james-0ac18d3e"),
    ("Alan Turing", "sk-poiesis-alan-bb2d7139"),
]


def random_students() -> list[tuple[str, str]]:
    return [
        (student_name, f"sk-poiesis-{secrets.token_urlsafe(24)}")
        for student_name, _ in DEFAULT_STUDENTS
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the PoiesisPathfinder SQLite user table.")
    parser.add_argument(
        "--database",
        default="./data/database.db",
        help="Path to the SQLite database file.",
    )
    parser.add_argument(
        "--random",
        action="store_true",
        help="Generate fresh secure virtual keys instead of the documented local-demo keys.",
    )
    args = parser.parse_args()

    database_path = str(Path(args.database))
    initialize_database(database_path)
    users = random_students() if args.random else DEFAULT_STUDENTS
    upsert_users(database_path, users)

    print(f"Seeded {len(users)} PoiesisPathfinder students in {database_path}")
    print()
    for user in list_users(database_path):
        status = "active" if user["is_active"] else "inactive"
        print(f"{user['student_name']}: {user['virtual_key']} ({status})")


if __name__ == "__main__":
    main()
