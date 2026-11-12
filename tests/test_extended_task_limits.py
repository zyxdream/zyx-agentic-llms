"""Budgets and timeout cleanup for longer-running product steps."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from billiard.exceptions import TimeLimitExceeded
from celery.exceptions import SoftTimeLimitExceeded
from flask import Flask

from backend import complaint_drafter, everything, hallucination_guard, views
from backend import rfa_drafter, rfp_drafter, rog_drafter
from backend.util import analytics


@pytest.fixture(autouse=True)
def enabled_account(monkeypatch):
    # Timeout contracts use fake memory rather than persisted account owners.
    from backend.util import account_access
    monkeypatch.setattr(account_access, 'chat_account_id', lambda chat_id: 1)
    monkeypatch.setattr(account_access, 'account_execution', lambda user_id: nullcontext())


def test_extended_task_limits_and_unrelated_parse_limits():
    for task in (
        complaint_drafter.state0,
        complaint_drafter.state1,
        complaint_drafter.state2,
        complaint_drafter.regenerate_citations_complaint,
    ):
        assert (task.soft_time_limit, task.time_limit) == (600, 630)
    assert (complaint_drafter.state3.soft_time_limit, complaint_drafter.state3.time_limit) != (600, 630)
    assert (hallucination_guard.standalone_hg_task.soft_time_limit,
            hallucination_guard.standalone_hg_task.time_limit) == (1200, 1260)
    assert (everything.hallucination_guard_task.soft_time_limit,
            everything.hallucination_guard_task.time_limit) == (1200, 1260)
    for module, parse_name in (
        (rfp_drafter, "rfp_parse_complaint_task"),
        (rfa_drafter, "rfa_parse_complaint_task"),
        (rog_drafter, "rog_parse_complaint_task"),
    ):
        assert getattr(module, parse_name).soft_time_limit != 600
        assert getattr(module, parse_name).time_limit != 630


class _RunningProcess:
    def __init__(self):
        self.terminated = False
        self.waited = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout):
        self.waited = True


def test_complaint_step_zero_stops_started_llm_children_on_soft_timeout(monkeypatch):
    process = _RunningProcess()
    launches = 0

    def launch(*_args, **_kwargs):
        nonlocal launches
        launches += 1
        if launches == 2:
            raise SoftTimeLimitExceeded()
        return process

    monkeypatch.setattr(analytics, "start_step_analytics", lambda *_args: None)
    monkeypatch.setattr(complaint_drafter.Drafts, "resolve_id", lambda _id: 42)
    memory = {}
    monkeypatch.setattr(complaint_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(complaint_drafter, "launch_llm_call_process_j", launch)

    with pytest.raises(SoftTimeLimitExceeded):
        complaint_drafter.state0.run({
            "draftId": "draft", "venue": "Court", "factualAllegations": "Facts",
            "plaintiff-1": "Plaintiff", "defendant-1": "Defendant",
        })
    assert launches == 2
    assert process.terminated and process.waited
    assert "drafted_sections/factual_allegations" not in memory


def test_complaint_step_zero_stops_all_children_when_wait_times_out(monkeypatch):
    processes = [_RunningProcess() for _ in range(5)]
    monkeypatch.setattr(analytics, "start_step_analytics", lambda *_args: None)
    monkeypatch.setattr(complaint_drafter.Drafts, "resolve_id", lambda _id: 42)
    memory = {}
    monkeypatch.setattr(complaint_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(complaint_drafter, "launch_llm_call_process_j", lambda *_args: processes.pop(0))
    launched = []

    def still_running(current):
        launched.extend(current)
        return True

    monkeypatch.setattr(complaint_drafter, "any_process_remaining", still_running)
    monkeypatch.setattr(complaint_drafter, "sleep", lambda _seconds: (_ for _ in ()).throw(SoftTimeLimitExceeded()))
    with pytest.raises(SoftTimeLimitExceeded):
        complaint_drafter.state0.run({
            "draftId": "draft", "venue": "Court", "factualAllegations": "Facts",
            "plaintiff-1": "Plaintiff", "defendant-1": "Defendant",
        })
    assert len(launched) == 5
    assert all(process.terminated and process.waited for process in launched)


@pytest.mark.parametrize("stage", ["provider", "citation_filter"])
def test_complaint_citation_regeneration_propagates_soft_timeout(monkeypatch, stage):
    memory = {"inputs/venue": "California", "analysis/coa_suggestions": "[]"}
    monkeypatch.setattr(analytics, "latest_or_create_step_analytics", lambda *_args: None)
    monkeypatch.setattr(complaint_drafter.Drafts, "resolve_id", lambda _id: 42)
    monkeypatch.setattr(complaint_drafter, "DBMemory", lambda db, doc_id: memory)

    def parse(_prompt, _schema, *, model, reasoning_effort):
        assert model == "gpt-6.1-sol"
        assert reasoning_effort == "high"
        if stage == "provider":
            raise SoftTimeLimitExceeded()
        return {"citations": [{
            "case_name": "", "citation": "35 U.S.C. § 271", "parenthetical": "(infringement)",
        }]}

    monkeypatch.setattr(complaint_drafter, "_parse_complaint_output", parse)
    if stage == "citation_filter":
        monkeypatch.setattr(
            complaint_drafter, "filter_raw_citations",
            lambda _citation: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
        )

    with pytest.raises(SoftTimeLimitExceeded):
        complaint_drafter.regenerate_citations_complaint.run({
            "draftId": "draft", "coa_index": 0, "coa_name": "Patent infringement",
        })
    assert "analysis/coa_suggestions" in memory


def test_standalone_guard_soft_timeout_releases_active_task(monkeypatch):
    active_key = hallucination_guard.STANDALONE_HG_ACTIVE_HG_TASK_KEY
    memory = {active_key: "guard-task"}
    monkeypatch.setattr(hallucination_guard.ChatThread, "resolve_id", lambda _id: 42)
    monkeypatch.setattr(hallucination_guard, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(
        hallucination_guard, "_standalone_hg_impl",
        lambda *_args: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )
    with pytest.raises(SoftTimeLimitExceeded):
        hallucination_guard.standalone_hg_task.run({"draftId": "draft"})
    assert active_key not in memory


def test_guard_enrichment_does_not_swallow_soft_timeout(monkeypatch):
    monkeypatch.setattr(
        hallucination_guard, "get_freelaw_cluster_cache",
        lambda _id: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )
    with pytest.raises(SoftTimeLimitExceeded):
        hallucination_guard._enrich_validated_cases_for_standalone({
            "123 F.3d 456": {"clusterId": 123},
        })


@pytest.mark.parametrize("failure", [SoftTimeLimitExceeded(), TimeLimitExceeded(1260)])
def test_non_case_assistant_timeout_has_generic_message(monkeypatch, failure):
    task_result = SimpleNamespace(
        result=failure, state="FAILURE", info=failure,
        failed=lambda: True, ready=lambda: True, successful=lambda: False,
    )
    monkeypatch.setattr(views, "AsyncResult", lambda _id: task_result)
    monkeypatch.setattr(views, "_cancel_redis", SimpleNamespace(get=lambda _key: None))
    app = Flask(__name__)
    with app.test_request_context():
        response = views.result.__wrapped__("other-task")
    assert response["value"]["error_code"] == "task_timeout"
    assert response["value"]["error_message"] == views.OTHER_TASK_TIMEOUT_MESSAGE
