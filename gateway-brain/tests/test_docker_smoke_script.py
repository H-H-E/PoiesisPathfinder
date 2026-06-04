from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest


def load_docker_smoke_script() -> ModuleType:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "docker_smoke.py"
    spec = importlib.util.spec_from_file_location("docker_smoke", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_docker_smoke_forces_safe_dry_run_compose_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_smoke = load_docker_smoke_script()
    monkeypatch.setenv("DRY_RUN_UPSTREAM", "false")
    monkeypatch.setenv("MINIMAX_API_KEY", "real-key-that-must-not-be-used")
    monkeypatch.setenv("POIESIS_ADMIN_TOKEN", "existing-token")

    env = docker_smoke.build_compose_env("smoke-admin-token")

    assert env["DRY_RUN_UPSTREAM"] == "true"
    assert env["ALLOW_INSECURE_ADMIN"] == "false"
    assert env["MINIMAX_API_KEY"] == ""
    assert env["POIESIS_ADMIN_TOKEN"] == "smoke-admin-token"
    assert os.environ["DRY_RUN_UPSTREAM"] == "false"


def test_docker_smoke_validates_safe_seeded_admin_payload() -> None:
    docker_smoke = load_docker_smoke_script()

    docker_smoke.validate_admin_users_payload(
        {
            "users": [
                {
                    "student_id": f"student-{index}",
                    "student_name": f"Student {index}",
                    "key_preview": f"sk-p...{index}",
                    "is_active": True,
                }
                for index in range(7)
            ]
        }
    )


def test_docker_smoke_rejects_raw_student_keys_in_admin_payload() -> None:
    docker_smoke = load_docker_smoke_script()

    with pytest.raises(docker_smoke.SmokeError):
        docker_smoke.validate_admin_users_payload(
            {
                "users": [
                    {
                        "student_id": f"student-{index}",
                        "key_preview": f"sk-p...{index}",
                        "virtual_key": "sk-poiesis-raw-secret",
                    }
                    for index in range(7)
                ]
            }
        )
