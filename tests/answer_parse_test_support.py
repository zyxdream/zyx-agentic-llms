"""Adapt pre-migration raw-JSON test doubles to the typed Answer call sites."""

import json
from types import SimpleNamespace

from backend import answer_drafter


def install_legacy_answer_json_mock(monkeypatch):
    """Keep existing malformed/partial JSON fixtures useful for parser regression tests."""

    def parse(prompt, response_model, **_kwargs):
        raw_output = answer_drafter.chat_completion_j(prompt=prompt)
        if not raw_output:
            return None
        try:
            data = json.loads(raw_output) if isinstance(raw_output, str) else raw_output
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        # These tests intentionally exercise invalid/partial legacy payloads. New provider
        # contract tests validate the real Pydantic schema and Responses request separately.
        return SimpleNamespace(model_dump=lambda: data)

    monkeypatch.setattr(answer_drafter, "chat_completion_parse", parse)
