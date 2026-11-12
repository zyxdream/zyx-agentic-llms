"""The request-generation tasks have a longer budget than the worker default."""

import importlib
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from billiard.exceptions import TimeLimitExceeded
from celery.exceptions import SoftTimeLimitExceeded

from backend.ai.discovery_request_schemas import RFARequests, RFPRequests, ROGRequests
from backend.views import _task_failure_value


@pytest.fixture(autouse=True)
def enabled_account(monkeypatch):
    # Timeout contracts use fake memory rather than persisted account owners.
    from backend.util import account_access
    monkeypatch.setattr(account_access, 'chat_account_id', lambda chat_id: 1)
    monkeypatch.setattr(account_access, 'account_execution', lambda user_id: nullcontext())
    monkeypatch.setattr(account_access, 'require_enabled_account', lambda user_id: None)


PRODUCTS = (
    ("rfp", "rfp_generate_requests_task", "RFP_ACTIVE_GENERATE_REQUESTS_TASK_KEY",
     RFPRequests, "Requests for Production of Documents", "Request", "Justification"),
    ("rfa", "rfa_continue_task", "RFA_ACTIVE_CONTINUE_TASK_KEY",
     RFARequests, "Requests for Admission", "Admission Request", "Strategic Rationale"),
    ("rog", "rog_continue_task", "ROG_ACTIVE_CONTINUE_TASK_KEY",
     ROGRequests, "INTERROGATORIES", "Interrogatory", "Rationale"),
)


def _raise_soft_timeout(*_args, **_kwargs):
    raise SoftTimeLimitExceeded()


def _setup(monkeypatch, product, active_key_name):
    module = importlib.import_module(f"backend.{product}_drafter")
    active_key = getattr(module, active_key_name)
    memory = {
        "complaint_cover_text": "Cover text",
        "complaint_text": json.dumps(["Complaint body"]),
        "inputs/side": "defendant",
        active_key: "current-task",
    }
    monkeypatch.setattr(module.Drafts, "resolve_id", lambda _draft_id: 42)
    monkeypatch.setattr(module, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(module, "sleep", lambda _seconds: None)
    return module, memory, active_key


@pytest.mark.parametrize("product,live_task,active_key,schema,alias,request_key,rationale_key", PRODUCTS)
def test_generation_gets_twenty_minutes_and_later_steps_keep_ten(
    product, live_task, active_key, schema, alias, request_key, rationale_key,
):
    module = importlib.import_module(f"backend.{product}_drafter")
    reduction_task = getattr(module, "further_reduce_task" if product == "rog" else f"further_reduce_{product}_task")
    for task in (module.state0, getattr(module, live_task)):
        assert (task.soft_time_limit, task.time_limit) == (1200, 1260)
    for task in (module.state1, reduction_task):
        assert (task.soft_time_limit, task.time_limit) == (600, 630)

    parse_task = getattr(module, f"{product}_parse_complaint_task")
    assert parse_task.soft_time_limit is None
    assert parse_task.time_limit is None


@pytest.mark.parametrize("product,live_task,active_key,schema,alias,request_key,rationale_key", PRODUCTS)
@pytest.mark.parametrize("step", ["reduce", "draft"])
def test_later_step_soft_timeout_is_not_saved_as_success(
    monkeypatch, product, live_task, active_key, schema, alias, request_key, rationale_key, step,
):
    module = importlib.import_module(f"backend.{product}_drafter")
    from backend.util import analytics

    monkeypatch.setattr(analytics, "start_step_analytics", lambda *_args: None)
    monkeypatch.setattr(analytics, "latest_or_create_step_analytics", lambda *_args: None)
    monkeypatch.setattr(module.Drafts, "resolve_id", lambda _draft_id: 42)
    memory = {
        "inputs/defendant": "Defendant", "inputs/plaintiff": "Plaintiff",
        "inputs/side": "defendant", "inputs/is_federal": "federal",
    }
    monkeypatch.setattr(module, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(module, "chat_completion_parse", _raise_soft_timeout)
    if step == "reduce":
        task = getattr(module, "further_reduce_task" if product == "rog" else f"further_reduce_{product}_task")
        with pytest.raises(SoftTimeLimitExceeded):
            task.run({"draftId": "draft", "targetN": 25})
        assert not any(key.startswith("temp/reduced_") for key in memory)
    else:
        with pytest.raises(SoftTimeLimitExceeded):
            module.state1.run({"draftId": "draft"})
        assert not any(key.startswith("drafted_sections/") for key in memory)


@pytest.mark.parametrize("product,live_task,active_key_name,schema,alias,request_key,rationale_key", PRODUCTS)
def test_live_task_returns_timeout_error_and_releases_active_id(
    monkeypatch, product, live_task, active_key_name, schema, alias, request_key, rationale_key,
):
    module, memory, active_key = _setup(monkeypatch, product, active_key_name)
    monkeypatch.setattr(module, f"generate_{product}_requests_from_memory", _raise_soft_timeout)
    result = getattr(module, live_task).run({"draftId": "draft"})
    assert result["message"] == "error"
    assert "timed out after 20 minutes" in result["error_message"]
    assert active_key not in memory


@pytest.mark.parametrize("product,live_task,active_key_name,schema,alias,request_key,rationale_key", PRODUCTS)
def test_progress_update_does_not_swallow_timeout(
    product, live_task, active_key_name, schema, alias, request_key, rationale_key,
):
    module = importlib.import_module(f"backend.{product}_drafter")
    progress = getattr(module, f"_{product}_progress")
    task = SimpleNamespace(update_state=_raise_soft_timeout)
    with pytest.raises(SoftTimeLimitExceeded):
        progress(task, "Generating", 35)


@pytest.mark.parametrize("product,live_task,active_key_name,schema,alias,request_key,rationale_key", PRODUCTS)
@pytest.mark.parametrize("stage", ["proposal", "consolidation"])
def test_parallel_chunk_timeout_reaches_task_boundary(
    monkeypatch, product, live_task, active_key_name, schema, alias, request_key, rationale_key, stage,
):
    module, memory, active_key = _setup(monkeypatch, product, active_key_name)
    monkeypatch.setattr(module, "extract_parties_venue", lambda *_args, **_kwargs: (["P"], ["D"], "Court"))
    monkeypatch.setattr(module, "chat_completion", lambda *_args, **_kwargs: "STATE")
    monkeypatch.setattr(module, "split_into_chunks", lambda lines, **_kwargs: [lines])
    calls = []

    def parse(_prompt, response_model, **_kwargs):
        assert response_model is schema
        calls.append(1)
        if stage == "proposal" or len(calls) == 2:
            raise SoftTimeLimitExceeded()
        return schema.model_validate({alias: [{
            "Topic": "Contracts", request_key: "Request text", rationale_key: "Reason",
        }]})

    monkeypatch.setattr(module, "chat_completion_parse", parse)
    result = getattr(module, live_task).run({"draftId": "draft"})
    assert result["message"] == "error"
    assert "timed out" in result["error_message"]
    assert len(calls) == (1 if stage == "proposal" else 2)
    assert active_key not in memory
    assert f"temp/consolidated_{product}s" not in memory


@pytest.mark.parametrize("product,live_task,active_key_name,schema,alias,request_key,rationale_key", PRODUCTS)
@pytest.mark.parametrize("stage", ["parsing", "generation"])
def test_legacy_combined_task_returns_timeout_error(
    monkeypatch, product, live_task, active_key_name, schema, alias, request_key, rationale_key, stage,
):
    module, _memory, _active_key = _setup(monkeypatch, product, active_key_name)
    if stage == "parsing":
        monkeypatch.setattr(module, "get_complaint_text_with_line_metadata_from_file", _raise_soft_timeout)
    else:
        monkeypatch.setattr(module, "get_complaint_text_with_line_metadata_from_file",
                            lambda _path: ("Complaint", None, None, []))
        monkeypatch.setattr(module, f"save_parsed_{product}_complaint", lambda *args: None)
        monkeypatch.setattr(module, f"generate_{product}_requests_from_memory", _raise_soft_timeout)
        if product in {"rfa", "rog"}:
            monkeypatch.setattr(module, "get_last_ocr_warning", lambda: None)
    result = module.state0.run("complaint.pdf", "draft", "defendant")
    assert result["message"] == "error"
    assert "timed out after 20 minutes" in result["error_message"]


def test_hard_limit_keeps_existing_retryable_result_shape():
    result = _task_failure_value(TimeLimitExceeded(1260))
    assert result["error"] is True
    assert result["error_code"] == "task_timeout"
    assert result["retryable"] is True
    assert result["error_message"]
