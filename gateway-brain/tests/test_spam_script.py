from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType


def load_spam_script() -> ModuleType:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "spam_test.py"
    spec = importlib.util.spec_from_file_location("spam_test", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_spam_script_builds_openai_compatible_chat_request() -> None:
    spam_test = load_spam_script()

    chat_request = spam_test.build_request(
        base_url="http://localhost:8000/",
        virtual_key="sk-poiesis-test",
        model="dry-run-minimax",
        message="hello",
        tier="standard",
    )

    assert chat_request.full_url == "http://localhost:8000/v1/chat/completions"
    assert chat_request.headers["Authorization"] == "Bearer sk-poiesis-test"
    assert chat_request.headers["X-request-tier"] == "standard"
    payload = json.loads(chat_request.data.decode("utf-8"))
    assert payload == {
        "model": "dry-run-minimax",
        "poiesis_tier": "standard",
        "messages": [{"role": "user", "content": "hello"}],
    }
