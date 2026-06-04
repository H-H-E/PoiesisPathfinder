#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, request


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT_NAME = "poiesispathfinder-smoke"
DEFAULT_GATEWAY_URL = "http://localhost:8000"
DEFAULT_DASHBOARD_URL = "http://localhost:3000"
DEFAULT_ADMIN_TOKEN = "replace-with-local-admin-token"
DEFAULT_STUDENT_KEY = "sk-poiesis-ada-7f3c9d2a"


class SmokeError(RuntimeError):
    pass


def compose_command(project_name: str, *args: str) -> list[str]:
    return ["docker", "compose", "-p", project_name, *args]


def build_compose_env(admin_token: str) -> dict[str, str]:
    env = os.environ.copy()
    env["DRY_RUN_UPSTREAM"] = "true"
    env["ALLOW_INSECURE_ADMIN"] = "false"
    env["POIESIS_ADMIN_TOKEN"] = admin_token
    env["MINIMAX_API_KEY"] = ""
    return env


def run_command(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print(f"+ {shlex.join(command)}", flush=True)
    subprocess.run(command, cwd=REPO_ROOT, env=env, check=True)


def read_url(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float,
) -> tuple[int, dict[str, str], bytes]:
    data = None
    request_headers = headers or {}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request_headers = {"Content-Type": "application/json", **request_headers}
    http_request = request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with request.urlopen(http_request, timeout=timeout_seconds) as response:
            headers = {key.lower(): value for key, value in response.headers.items()}
            return response.status, headers, response.read()
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SmokeError(f"{url} returned HTTP {exc.code}: {body[:300]}") from exc
    except error.URLError as exc:
        raise SmokeError(f"{url} was unreachable: {exc}") from exc


def read_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float,
) -> tuple[dict[str, Any], dict[str, str]]:
    _, response_headers, body = read_url(
        url,
        method=method,
        headers=headers,
        payload=payload,
        timeout_seconds=timeout_seconds,
    )
    try:
        parsed = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SmokeError(f"{url} did not return JSON: {body[:300]!r}") from exc
    if not isinstance(parsed, dict):
        raise SmokeError(f"{url} returned non-object JSON")
    return parsed, response_headers


def wait_for_json(
    url: str,
    *,
    timeout_seconds: float,
    request_timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            payload, _ = read_json(url, timeout_seconds=request_timeout_seconds)
            return payload
        except Exception as exc:  # noqa: BLE001 - surface the last retry reason.
            last_error = exc
            time.sleep(2)
    raise SmokeError(f"Timed out waiting for {url}: {last_error}") from last_error


def verify_health(gateway_url: str, *, timeout_seconds: float) -> None:
    payload = wait_for_json(
        f"{gateway_url.rstrip('/')}/health",
        timeout_seconds=timeout_seconds,
        request_timeout_seconds=5,
    )
    if payload.get("ok") is not True or payload.get("redis") is not True:
        raise SmokeError(f"Unexpected health payload: {payload}")
    print("health ok: Redis is reachable")


def validate_admin_users_payload(payload: dict[str, Any]) -> None:
    users = payload.get("users")
    if not isinstance(users, list) or len(users) != 7:
        raise SmokeError(f"Expected seven seeded users, got: {users!r}")
    serialized = json.dumps(payload, sort_keys=True)
    if "sk-poiesis-" in serialized or "virtual_key_hash" in serialized:
        raise SmokeError("Admin users payload exposes raw student key material")
    for user in users:
        if not isinstance(user, dict):
            raise SmokeError("Admin users payload contains a non-object user")
        if not user.get("student_id") or not user.get("key_preview"):
            raise SmokeError(f"Seeded user is missing safe identity fields: {user}")
        if "virtual_key" in user:
            raise SmokeError("Admin users payload contains virtual_key")


def verify_seeded_users(
    gateway_url: str,
    *,
    admin_token: str,
    request_timeout_seconds: float,
) -> None:
    payload, _ = read_json(
        f"{gateway_url.rstrip('/')}/admin/users",
        headers={"x-admin-token": admin_token},
        timeout_seconds=request_timeout_seconds,
    )
    validate_admin_users_payload(payload)
    print("seed ok: seven safe student records are available")


def verify_dashboard(dashboard_url: str, *, request_timeout_seconds: float) -> None:
    status, _, body = read_url(dashboard_url, timeout_seconds=request_timeout_seconds)
    if status < 200 or status >= 300:
        raise SmokeError(f"Dashboard returned HTTP {status}")
    text = body[:500].decode("utf-8", errors="replace").lower()
    if "<html" not in text and "__next" not in text:
        raise SmokeError("Dashboard response did not look like a Next.js HTML page")
    print("dashboard ok: Next.js app returned HTML")


def verify_dry_run_chat(
    gateway_url: str,
    *,
    virtual_key: str,
    request_timeout_seconds: float,
) -> None:
    payload, headers = read_json(
        f"{gateway_url.rstrip('/')}/v1/chat/completions",
        method="POST",
        headers={"Authorization": f"Bearer {virtual_key}"},
        payload={
            "model": "dry-run-minimax",
            "messages": [{"role": "user", "content": "PoiesisPathfinder Docker smoke."}],
        },
        timeout_seconds=request_timeout_seconds,
    )
    usage = payload.get("usage")
    if not isinstance(usage, dict) or not isinstance(usage.get("total_tokens"), int):
        raise SmokeError(f"Dry-run chat response did not include usage: {payload}")
    if not headers.get("x-request-id"):
        raise SmokeError("Dry-run chat response did not include x-request-id")
    print(f"chat ok: dry-run response used {usage['total_tokens']} tokens")


def verify_spam_block(
    gateway_url: str,
    *,
    virtual_key: str,
    request_timeout_seconds: float,
) -> None:
    run_command(
        [
            sys.executable,
            "scripts/spam_test.py",
            "--base-url",
            gateway_url,
            "--virtual-key",
            virtual_key,
            "--count",
            "3",
            "--tier",
            "standard",
            "--delay",
            "0",
            "--timeout",
            str(request_timeout_seconds),
        ]
    )
    print("spam ok: standard tier burst block was observed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Docker Compose Gate A dry-run smoke test.",
    )
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL)
    parser.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD_URL)
    parser.add_argument("--admin-token", default=DEFAULT_ADMIN_TOKEN)
    parser.add_argument("--virtual-key", default=DEFAULT_STUDENT_KEY)
    parser.add_argument("--startup-timeout", type=float, default=180.0)
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument(
        "--skip-compose-up",
        action="store_true",
        help="Verify an already-running stack instead of starting Compose.",
    )
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="Leave containers and smoke-test volumes running after checks.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    compose_env = build_compose_env(args.admin_token)
    started_stack = False
    try:
        if not args.skip_compose_up:
            started_stack = True
            run_command(
                compose_command(args.project_name, "up", "--build", "--wait"),
                env=compose_env,
            )
        verify_health(args.gateway_url, timeout_seconds=args.startup_timeout)
        verify_seeded_users(
            args.gateway_url,
            admin_token=args.admin_token,
            request_timeout_seconds=args.request_timeout,
        )
        verify_dashboard(args.dashboard_url, request_timeout_seconds=args.request_timeout)
        verify_dry_run_chat(
            args.gateway_url,
            virtual_key=args.virtual_key,
            request_timeout_seconds=args.request_timeout,
        )
        verify_spam_block(
            args.gateway_url,
            virtual_key=args.virtual_key,
            request_timeout_seconds=args.request_timeout,
        )
    except (SmokeError, subprocess.CalledProcessError) as exc:
        print(f"Docker smoke failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if started_stack and not args.keep_running:
            try:
                run_command(
                    compose_command(
                        args.project_name,
                        "down",
                        "--volumes",
                        "--remove-orphans",
                    ),
                    env=compose_env,
                )
            except subprocess.CalledProcessError as exc:
                print(f"Cleanup failed: {exc}", file=sys.stderr)

    print("Docker smoke passed: compose, seed, dashboard, dry-run chat, and spam block verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
