from __future__ import annotations

import argparse
import os
import re
import uuid
from pathlib import Path

from app import database


PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reset monthly student token totals while retaining audit history.",
    )
    parser.add_argument(
        "--database",
        default=os.getenv("DATABASE_PATH", "./data/database.db"),
        help="Path to the SQLite database file.",
    )
    parser.add_argument(
        "--period",
        required=True,
        help="Billing period label in YYYY-MM format, for example 2026-07.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    period = args.period.strip()
    if not PERIOD_PATTERN.fullmatch(period):
        print("--period must use YYYY-MM format.")
        return 2

    database_path = str(Path(args.database))
    database.initialize_database(database_path)
    request_id = f"monthly-reset-{period}-{uuid.uuid4().hex[:12]}"
    rows = database.reset_monthly_token_totals(
        database_path,
        request_id=request_id,
        period_label=period,
    )

    print(f"Monthly reset complete for {period}")
    print(f"Request ID: {request_id}")
    print(f"Students reset: {len(rows)}")
    for row in rows:
        state = "active" if row["is_active"] else "inactive"
        print(
            f"{row['student_id']}: {row['previous_total_tokens_consumed']} -> "
            f"{row['total_tokens_consumed']} tokens ({state})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
