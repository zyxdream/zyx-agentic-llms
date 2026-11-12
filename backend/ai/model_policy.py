"""Extracted, application-owned helpers for the Workspace agent."""

import os
from backend.ai import base as ai_base

ASSISTANT_MODES = {"fast", "deep"}


WORKSPACE_FAST_MODEL = os.getenv("WORKSPACE_FAST_MODEL", "gpt-6-luna")


def resolve_assistant_mode(data: dict) -> str:
    """Resolve the new mode field, including older model-valued requests/tasks."""
    if "assistant_mode" in data:
        explicit_mode = data["assistant_mode"]
        if explicit_mode not in ASSISTANT_MODES:
            raise ValueError("Invalid Workspace mode. Choose Fast or Deep.")
        return explicit_mode
    legacy_model = data.get("model") or data.get("selected_model")
    return "fast" if legacy_model in {"gpt-6-sol", "gpt-6.1-sol"} else "deep"


def _assistant_model(assistant_mode: str) -> str:
    return WORKSPACE_FAST_MODEL if assistant_mode == "fast" else os.getenv("WORKSPACE_DEEP_MODEL", ai_base.DEFAULT_MODEL)


def _output_effort(assistant_mode: str) -> str:
    return "medium" if assistant_mode == "fast" else "high"
