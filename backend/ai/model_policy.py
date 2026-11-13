"""Extracted, application-owned helpers for the Workspace agent."""

import os
from backend.ai import base as ai_base

ASSISTANT_MODES = {"fast", "deep"}


WORKSPACE_FAST_MODEL = os.getenv("WORKSPACE_FAST_MODEL", "gpt-6-luna")


def resolve_assistant_mode(data: dict) -> str:
    """Resolve saved policy, including legacy model-valued requests/tasks."""
    if "assistant_mode" in data:
        explicit_mode = data["assistant_mode"]
        if not isinstance(explicit_mode, str) or explicit_mode not in ASSISTANT_MODES:
            raise ValueError("Workspace model configuration is invalid.")
        return explicit_mode
    legacy_model = data.get("model") or data.get("selected_model")
    return "fast" if legacy_model in {"gpt-6-sol", "gpt-6.1-sol"} else "deep"


def apply_admission_policy(data: dict, previous: dict | None = None) -> dict:
    """Choose only for new mode-less runs; keep saved and legacy policy intact.

    The browser sends no model selectors. Older clients and saved tasks retain
    their exact fields, including the historical default for tasks with none.
    HTTP resubmission must not adopt a later server default.
    """
    selectors = ('assistant_mode', 'model', 'selected_model')
    result = dict(data)
    if any(key in result for key in selectors):
        return result
    if previous is not None:
        result.update({key: previous[key] for key in selectors if key in previous})
        return result
    from flask import current_app
    mode = current_app.config.get('WORKSPACE_DEFAULT_MODE', 'fast')
    if not isinstance(mode, str) or mode not in ASSISTANT_MODES:
        raise ValueError('Workspace model configuration is invalid.')
    result['assistant_mode'] = mode
    return result


def _assistant_model(assistant_mode: str) -> str:
    return WORKSPACE_FAST_MODEL if assistant_mode == "fast" else os.getenv("WORKSPACE_DEEP_MODEL", ai_base.DEFAULT_MODEL)


def _output_effort(assistant_mode: str) -> str:
    return "medium" if assistant_mode == "fast" else "high"
