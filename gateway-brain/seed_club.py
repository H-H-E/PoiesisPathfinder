from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

from app.database import DEFAULT_KEY_HASH_SECRET, initialize_database, upsert_users


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
    parser.add_argument(
        "--key-hash-secret",
        default=os.getenv("POIESIS_KEY_HASH_SECRET", DEFAULT_KEY_HASH_SECRET),
        help="Secret used to HMAC student virtual keys before storing them.",
    )
    args = parser.parse_args()

    database_path = str(Path(args.database))
    initialize_database(database_path, args.key_hash_secret)
    users = random_students() if args.random else DEFAULT_STUDENTS
    upsert_users(database_path, users, args.key_hash_secret)

    print(f"Seeded {len(users)} PoiesisPathfinder students in {database_path}")
    print()
    for student_name, virtual_key in users:
        print(f"{student_name}: {virtual_key} (active)")


if __name__ == "__main__":
    main()
