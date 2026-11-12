from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from backend import answer_drafter


@pytest.fixture(autouse=True)
def enabled_account(monkeypatch):
    # Introduction tests use dictionary memory without persisted account owners.
    from backend.util import account_access
    monkeypatch.setattr(account_access, 'chat_account_id', lambda chat_id: 1)
    monkeypatch.setattr(account_access, 'account_execution', lambda user_id: nullcontext())


class Memory(dict):
    user = SimpleNamespace(contact_info="Test contact")

    def get(self, key, default=None):
        return super().get(key, default)


def _patch_state3_infrastructure(monkeypatch, memory: Memory, pdf_calls: list[dict]) -> None:
    monkeypatch.setattr(answer_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(answer_drafter.Drafts, "pdf_filename", lambda draft_id: "answer-output.pdf")
    monkeypatch.setattr(answer_drafter, "DBMemory", lambda db, doc_id: memory)

    def fake_create_answer_pdf(**kwargs):
        pdf_calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(answer_drafter, "create_answer_pdf", fake_create_answer_pdf)


def test_answer_state3_builds_intro_for_lowercase_state(monkeypatch):
    pdf_calls = []
    memory = Memory({
        "inputs/is_federal": "state",
        "inputs/plaintiff": "XLON Beauty, LLC",
        "inputs/defendant": "Doris Day",
        "inputs/chosen_defendants": "Doris Day",
        "inputs/venue": "County of New York",
        "drafted_sections/responses": "Responses.",
        "drafted_sections/affirmative_defenses": "Affirmative defenses.",
    })
    _patch_state3_infrastructure(monkeypatch, memory, pdf_calls)

    result = answer_drafter.state3.run({
        "draftId": "draft-public-id",
        "wants_counterclaim": "off",
    })

    assert result["message"] == "Form processed successfully"
    assert "DORIS DAY" in result["intro"]
    assert "XLON BEAUTY, LLC" in result["intro"]
    assert "hereby answers the Complaint" in result["intro"]
    assert memory["drafted_sections/counter_claims"].startswith("\nWHEREFORE")
    assert pdf_calls


def test_answer_state3_builds_intro_for_lowercase_federal(monkeypatch):
    pdf_calls = []
    memory = Memory({
        "inputs/is_federal": "federal",
        "inputs/plaintiff": "Acme Corp.",
        "inputs/defendant": "Beta LLC",
        "inputs/chosen_defendants": "Beta LLC",
        "inputs/venue": "United States District Court",
        "drafted_sections/responses": "Responses.",
        "drafted_sections/affirmative_defenses": "Affirmative defenses.",
    })
    _patch_state3_infrastructure(monkeypatch, memory, pdf_calls)

    result = answer_drafter.state3.run({
        "draftId": "draft-public-id",
        "wants_counterclaim": "off",
    })

    assert result["message"] == "Form processed successfully"
    assert "Rule 8(b)" in result["intro"]
    assert "BETA LLC" in result["intro"]
    assert "ACME CORP." in result["intro"]
