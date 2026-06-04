#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib import error, request


@dataclass(frozen=True)
class AttemptResult:
    index: int
    status_code: int
    request_id: str | None
    body: str


def build_payload(*, model: str, message: str, tier: str) -> dict[str, Any]:
    return {
        "model": model,
        "poiesis_tier": tier,
        "messages": [{"role": "user", "content": message}],
    }


def build_request(
    *,
    base_url: str,
    virtual_key: str,
    model: str,
    message: str,
    tier: str,
) -> request.Request:
    payload = json.dumps(
        build_payload(model=model, message=message, tier=tier),
        separators=(",", ":"),
    ).encode("utf-8")
    return request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {virtual_key}",
            "Content-Type": "application/json",
            "x-request-tier": tier,
        },
    )


def send_attempt(
    *,
    index: int,
    base_url: str,
    virtual_key: str,
    model: str,
    message: str,
    tier: str,
    timeout_seconds: float,
) -> AttemptResult:
    chat_request = build_request(
        base_url=base_url,
        virtual_key=virtual_key,
        model=model,
        message=message,
        tier=tier,
    )
    try:
        with request.urlopen(chat_request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
            return AttemptResult(
                index=index,
                status_code=response.status,
                request_id=response.headers.get("x-request-id"),
                body=body,
            )
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return AttemptResult(
            index=index,
            status_code=exc.code,
            request_id=exc.headers.get("x-request-id"),
            body=body,
        )


def summarize_body(body: str) -> str:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return body[:180]
    detail = parsed.get("detail")
    if isinstance(detail, dict):
        message = str(detail.get("message", ""))
        scope = detail.get("scope")
        if scope:
            return f"{message} scope={scope}"
        return message
    usage = parsed.get("usage")
    if isinstance(usage, dict):
        return f"usage.total_tokens={usage.get('total_tokens')}"
    return body[:180]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send repeated dry-run chat requests and verify local burst blocking.",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="FastAPI gateway base URL.",
    )
    parser.add_argument(
        "--virtual-key",
        required=True,
        help="Student key to test, for example sk-poiesis-ada-7f3c9d2a.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=3,
        help="Number of chat requests to send.",
    )
    parser.add_argument(
        "--tier",
        choices=("standard", "high-speed"),
        default="standard",
        help="Tier label to include in local test payloads; production tier is still server-side.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds to wait between attempts.",
    )
    parser.add_argument(
        "--model",
        default="dry-run-minimax",
        help="Model name to send in the OpenAI-compatible request.",
    )
    parser.add_argument(
        "--message",
        default="PoiesisPathfinder local spam verification.",
        help="User message content to send.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP timeout in seconds for each attempt.",
    )
    parser.add_argument(
        "--no-expect-block",
        action="store_true",
        help="Return success even when no HTTP 429 is observed.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.count < 1:
        print("--count must be at least 1", file=sys.stderr)
        return 2

    saw_block = False
    for index in range(1, args.count + 1):
        result = send_attempt(
            index=index,
            base_url=args.base_url,
            virtual_key=args.virtual_key,
            model=args.model,
            message=args.message,
            tier=args.tier,
            timeout_seconds=args.timeout,
        )
        saw_block = saw_block or result.status_code == 429
        request_id = result.request_id or "none"
        print(
            f"{result.index:02d} status={result.status_code} request_id={request_id} "
            f"{summarize_body(result.body)}"
        )
        if index < args.count and args.delay > 0:
            time.sleep(args.delay)

    if not saw_block and not args.no_expect_block:
        print("Expected at least one HTTP 429 burst block but none occurred.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
