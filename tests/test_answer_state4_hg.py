import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from backend import answer_drafter
from backend import hallucination_guard


@pytest.fixture(autouse=True)
def enabled_account(monkeypatch):
    # HG integration tests use dictionary memory without persisted account owners.
    from backend.util import account_access
    monkeypatch.setattr(account_access, 'chat_account_id', lambda chat_id: 1)
    monkeypatch.setattr(account_access, 'account_execution', lambda user_id: nullcontext())


class Memory(dict):
    user = SimpleNamespace(contact_info="Test contact")

    def get(self, key, default=None):
        return super().get(key, default)


def _patch_state4_infrastructure(monkeypatch, memory: Memory) -> list[str]:
    drafted_files = []
    monkeypatch.setattr(answer_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(answer_drafter.Drafts, "pdf_filename", lambda draft_id: "answer-output.pdf")
    monkeypatch.setattr(answer_drafter, "DBMemory", lambda db, doc_id: memory)

    def fake_draft_full_answer(mem, filename):
        drafted_files.append(filename)
        return "ok"

    monkeypatch.setattr(answer_drafter, "draft_full_answer", fake_draft_full_answer)
    return drafted_files


def _issue_case_result() -> dict:
    return {
        "citation": "123 F.3d 456",
        "citation_as_drafted": "Bad v. Case, 123 F.3d 456",
        "location": "Affirmative Defenses",
        "quotes": [],
        "pincite": "",
        "year": "2020",
        "is_valid": False,
        "is_applicable": False,
        "reason": "Not found in our database",
    }


def test_answer_state4_runs_hg_on_drafted_ads_without_reparsing_pdf(monkeypatch):
    ad_text = "FIRST AFFIRMATIVE DEFENSE\nSee Bad v. Case, 123 F.3d 456."
    memory = Memory({
        "drafted_sections/intro": "Intro.",
        "drafted_sections/responses": "Responses.",
        "drafted_sections/affirmative_defenses": ad_text,
        "drafted_sections/counter_claims": "Prayer.",
    })
    drafted_files = _patch_state4_infrastructure(monkeypatch, memory)
    calls = []

    monkeypatch.setattr(
        answer_drafter,
        "get_text_from_pdf_fitz",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("state4 should not parse generated PDF for HG")),
    )
    monkeypatch.setattr(
        answer_drafter,
        "chat_completion_j",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("old Answer HG prompt should not be used")),
    )

    def fake_run_hg(file_text, *, state=None, task=None, persist_state=None, case_viewer_prefix=None, source_label="uploaded document"):
        calls.append({
            "file_text": file_text,
            "state": state,
            "source_label": source_label,
        })
        return {
            "case_results": [_issue_case_result()],
            "statute_results": [],
            "lookup_diagnostics": [{"citation": "123 F.3d 456"}],
            "filtered_citations": [{"displayName": "123 F.3d 456"}],
            "prompt_token_counts": [100, 50],
        }

    monkeypatch.setattr(hallucination_guard, "run_hallucination_guard_on_text", fake_run_hg)

    result = answer_drafter.state4.run({
        "draftId": "draft-public-id",
        "wants_intro": "on",
        "introBody": "Intro.",
    })

    assert result["message"] == "Form processed successfully"
    assert drafted_files == ["backend/static/answer-output.pdf"]
    assert calls == [{
        "file_text": ad_text,
        "state": {},
        "source_label": "drafted affirmative defenses",
    }]
    assert result["halls"][0][0] == "incorrect"
    assert "Bad v. Case" in result["halls"][0][1]
    assert json.loads(memory["hallucination_check"]) == result["halls"]
    saved_case_results = json.loads(memory["answer_hg/case_results"])
    assert saved_case_results[0]["citation"] == "123 F.3d 456"
    assert saved_case_results[0]["citation_as_drafted"] == "Bad v. Case, 123 F.3d 456"
    assert saved_case_results[0]["status"] == "Issue"
    assert json.loads(memory["answer_hg/lookup_diagnostics"]) == [{"citation": "123 F.3d 456"}]
    assert json.loads(memory["answer_hg/filtered_citations"]) == [{"displayName": "123 F.3d 456"}]
    assert memory["answer_hg/results_html"]


def test_answer_state4_empty_ads_save_empty_hg_without_llm(monkeypatch):
    memory = Memory({
        "drafted_sections/affirmative_defenses": "",
    })
    _patch_state4_infrastructure(monkeypatch, memory)
    monkeypatch.setattr(
        hallucination_guard,
        "run_hallucination_guard_on_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("empty ADs should not run HG")),
    )

    result = answer_drafter.state4.run({
        "draftId": "draft-public-id",
    })

    assert result["message"] == "Form processed successfully"
    assert result["halls"] == []
    assert json.loads(memory["hallucination_check"]) == []
    assert json.loads(memory["answer_hg/case_results"]) == []
    assert json.loads(memory["answer_hg/statute_results"]) == []
    assert memory["answer_hg/results_html"] == ""


def test_answer_hg_ad_chunks_stay_under_configured_char_limit(monkeypatch):
    monkeypatch.setattr(answer_drafter, "ANSWER_HG_AD_CHUNK_CHARS", 1000)
    monkeypatch.setattr(answer_drafter, "ANSWER_HG_AD_CHUNK_OVERLAP", 50)
    ad_text = "\n".join([f"Defense paragraph {index}. " + ("x" * 100) for index in range(30)])

    chunks = answer_drafter._build_answer_hg_ad_chunks(ad_text)

    assert len(chunks) > 1
    assert all(chunk["char_count"] <= 1000 for chunk in chunks)
    assert chunks[0]["start_offset"] == 0
    assert chunks[1]["start_offset"] < chunks[0]["end_offset"]


def test_answer_hg_merge_dedupes_overlap_and_prefers_richer_issue_record():
    weak_record = {
        "citation": "123 F.3d 456",
        "citation_as_drafted": "Bad v. Case, 123 F.3d 456",
        "location": "Affirmative Defenses",
        "quotes": [],
        "is_valid": True,
        "is_applicable": True,
        "reason": "",
    }
    rich_record = _issue_case_result()

    result = answer_drafter._merge_answer_hg_chunk_results(
        [
            {"case_results": [weak_record], "statute_results": [], "lookup_diagnostics": [], "filtered_citations": [], "chunk_metadata": {"chunk_index": 1}},
            {"case_results": [rich_record], "statute_results": [], "lookup_diagnostics": [], "filtered_citations": [], "chunk_metadata": {"chunk_index": 2}},
        ],
        [],
        2,
    )

    assert result["case_results"] == [rich_record]
    assert len(result["halls"]) == 1
    assert result["halls"][0][0] == "incorrect"


def test_answer_hg_chunk_failure_is_partial_and_nonfatal(monkeypatch):
    monkeypatch.setattr(answer_drafter, "ANSWER_HG_AD_CHUNK_CHARS", 80)
    monkeypatch.setattr(answer_drafter, "ANSWER_HG_AD_CHUNK_OVERLAP", 10)
    memory = Memory({
        "drafted_sections/affirmative_defenses": "FIRST AFFIRMATIVE DEFENSE\n" + ("x " * 80),
    })
    calls = []

    def fake_run_hg(file_text, *, state=None, task=None, persist_state=None, case_viewer_prefix=None, source_label="uploaded document"):
        calls.append(file_text)
        if len(calls) == 1:
            return {
                "case_results": [_issue_case_result()],
                "statute_results": [],
                "lookup_diagnostics": [],
                "filtered_citations": [],
                "prompt_token_counts": [10, 5],
            }
        raise RuntimeError("HG unavailable")

    monkeypatch.setattr(hallucination_guard, "run_hallucination_guard_on_text", fake_run_hg)

    result = answer_drafter.run_answer_hallucination_guard_on_ad_text(memory)

    assert len(calls) > 1
    assert any("partial coverage" in hall[1] for hall in result["halls"])
    chunk_metadata = json.loads(memory["answer_hg/chunk_metadata"])
    assert any(chunk["status"] == "success" for chunk in chunk_metadata)
    assert any(chunk["status"] == "error" for chunk in chunk_metadata)
