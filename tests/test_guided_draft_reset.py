import json
from types import SimpleNamespace

from flask import Flask, request
import pytest

from backend.util.guided_draft_reset import clear_memory_keys


@pytest.fixture(autouse=True)
def enabled_account(monkeypatch):
    # These memory-only behavior tests do not create account/database records.
    from backend.util import account_access
    monkeypatch.setattr(account_access, 'chat_account_id', lambda chat_id: 1)
    monkeypatch.setattr(account_access, 'require_enabled_account', lambda user_id: None)


class Memory(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _line_metadata_for(lines: list[str]) -> list[dict]:
    return [
        {
            "line_number": index,
            "page_number": 1,
            "page_line_number": index,
            "bold": False,
            "ocr": False,
        }
        for index, _line in enumerate(lines)
    ]


def test_clear_memory_keys_ignores_missing_keys():
    memory = Memory({"a": "keep", "b": "delete"})

    clear_memory_keys(memory, ["b", "missing"])

    assert memory == {"a": "keep"}


def _patch_answer_handler(monkeypatch, answer_drafter, memory):
    monkeypatch.setattr(answer_drafter, "handle_file_upload", lambda request, folder: ("new-answer.pdf", None))
    monkeypatch.setattr(answer_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(answer_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(answer_drafter.state0, "delay", lambda **kwargs: SimpleNamespace(id="task-answer"))


def _patch_rfp_handler(monkeypatch, rfp_drafter, memory):
    monkeypatch.setattr(rfp_drafter, "handle_file_upload", lambda request, folder: ("new-rfp.pdf", None))
    monkeypatch.setattr(rfp_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfp_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfp_drafter.state0, "delay", lambda **kwargs: SimpleNamespace(id="task-rfp"))


def _patch_rog_handler(monkeypatch, rog_drafter, memory):
    monkeypatch.setattr(rog_drafter, "handle_file_upload", lambda request, folder: ("new-rog.pdf", None))
    monkeypatch.setattr(rog_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rog_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rog_drafter.state0, "delay", lambda **kwargs: SimpleNamespace(id="task-rog"))


def _patch_rfa_handler(monkeypatch, rfa_drafter, memory):
    monkeypatch.setattr(rfa_drafter, "handle_file_upload", lambda request, folder: ("new-rfa.pdf", None))
    monkeypatch.setattr(rfa_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfa_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfa_drafter.state0, "delay", lambda **kwargs: SimpleNamespace(id="task-rfa"))


def _patch_rfp_response_handler(monkeypatch, rfp_response_drafter, memory):
    monkeypatch.setattr(rfp_response_drafter, "handle_file_upload", lambda request, folder: ("new-rfp-response.pdf", None))
    monkeypatch.setattr(rfp_response_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfp_response_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfp_response_drafter.state0, "delay", lambda **kwargs: SimpleNamespace(id="task-rfp-response"))


def _patch_rog_response_handler(monkeypatch, rog_response_drafter, memory):
    monkeypatch.setattr(rog_response_drafter, "handle_file_upload", lambda request, folder: ("new-rog-response.pdf", None))
    monkeypatch.setattr(rog_response_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rog_response_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rog_response_drafter.state0, "delay", lambda **kwargs: SimpleNamespace(id="task-rog-response"))


def test_answer_state0_reset_prevents_old_intro_restore(monkeypatch):
    from backend import answer_drafter

    app = Flask(__name__)
    memory = Memory({
        "complaint_cover_text": "old cover",
        "complaint_text": '["old body"]',
        "line_metadata": '[{"line_number": 0}]',
        "drafted_sections/intro": "old intro",
        "drafted_sections/counter_claims": "old counterclaims",
        "drafted_sections/affirmative_defenses": "old defenses",
        "drafted_sections/responses": "old responses",
        "inputs/uploaded_filename": "old-answer.pdf",
        "inputs/case_number": "old case",
        "inputs/generated_pdf_filename": "old-answer.pdf",
        "inputs/plaintiff": "old plaintiff",
        "inputs/defendant": "old defendant",
        "inputs/venue": "old venue",
        "inputs/is_federal": "state",
        "inputs/state": "california",
        "complaint_structure": "{}",
        "sections": "[]",
        "causes_of_action": "[]",
        "summary": "old summary",
        "section_headers": "{}",
        "paragraphs": "{}",
        "answer/active_parse_task_id": "old-parse-task",
        "answer/active_continue_task_id": "old-continue-task",
        "hallucination_check": '[["old"]]',
    })
    _patch_answer_handler(monkeypatch, answer_drafter, memory)

    with app.test_request_context(
        method="POST",
        data={"draftId": "draft-public-id", "file": "unused"},
    ):
        response = answer_drafter.handle_state0(request)

    assert response == ({"result_id": "task-answer"}, 200)
    assert memory["inputs/uploaded_filename"] == "new-answer.pdf"
    assert "complaint_cover_text" not in memory
    assert "complaint_text" not in memory
    assert "line_metadata" not in memory
    assert "drafted_sections/intro" not in memory
    assert "drafted_sections/counter_claims" not in memory
    assert "drafted_sections/affirmative_defenses" not in memory
    assert "drafted_sections/responses" not in memory
    assert "inputs/case_number" not in memory
    assert "inputs/generated_pdf_filename" not in memory
    assert "inputs/plaintiff" not in memory
    assert "inputs/defendant" not in memory
    assert "inputs/venue" not in memory
    assert "inputs/is_federal" not in memory
    assert "inputs/state" not in memory
    assert "complaint_structure" not in memory
    assert "sections" not in memory
    assert "causes_of_action" not in memory
    assert "summary" not in memory
    assert "section_headers" not in memory
    assert "paragraphs" not in memory
    assert "answer/active_parse_task_id" not in memory
    assert "answer/active_continue_task_id" not in memory
    assert "hallucination_check" not in memory

    memory["ads_suggestions"] = '{"Failure to State a Claim": {"1": {"description": "new"}}}'
    assert memory["ads_suggestions"]


def test_answer_upload_route_stores_active_parse_task_id(monkeypatch):
    from backend import answer_drafter

    app = Flask(__name__)
    memory = Memory()
    monkeypatch.setattr(answer_drafter, "handle_file_upload", lambda request, folder: ("new-answer.pdf", None))
    monkeypatch.setattr(answer_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(answer_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(answer_drafter, "get_active_answer_task_id", lambda doc_id, key: None)
    monkeypatch.setattr(
        answer_drafter.answer_parse_complaint_task,
        "delay",
        lambda **kwargs: SimpleNamespace(id="parse-task"),
    )

    with app.test_request_context(
        method="POST",
        data={"draftId": "draft-public-id", "file": "unused"},
    ):
        response = answer_drafter.handle_upload_complaint(request)

    assert response == ({"result_id": "parse-task", "uploaded_filename": "new-answer.pdf"}, 200)
    assert memory[answer_drafter.ANSWER_ACTIVE_PARSE_TASK_KEY] == "parse-task"


def test_answer_parse_task_saves_parsed_text_and_clears_active_task(monkeypatch):
    from backend import answer_drafter

    memory = Memory({
        answer_drafter.ANSWER_ACTIVE_PARSE_TASK_KEY: "parse-task",
    })
    monkeypatch.setattr(answer_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(answer_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(answer_drafter, "_answer_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(answer_drafter, "get_last_ocr_warning", lambda: False)
    monkeypatch.setattr(
        answer_drafter,
        "get_complaint_text_with_line_metadata_from_file",
        lambda path: (
            "Cover text\nFirst line\nSecond line",
            "case-123",
            False,
            _line_metadata_for(["Cover text", "First line", "Second line"]),
        ),
    )

    result = answer_drafter.answer_parse_complaint_task.run(
        filename="complaint.pdf",
        draftId="draft-public-id",
    )

    assert result["has_parsed_complaint"] is True
    assert memory["inputs/case_number"] == "case-123"
    assert memory["complaint_cover_text"] == "Cover text\nFirst line\nSecond line"
    assert json.loads(memory["complaint_text"]) == ["Cover text", "First line", "Second line"]
    assert json.loads(memory["line_metadata"]) == _line_metadata_for(["Cover text", "First line", "Second line"])
    assert memory["inputs/uploaded_filename"] == "complaint.pdf"
    assert answer_drafter.ANSWER_ACTIVE_PARSE_TASK_KEY not in memory


def test_answer_continue_refuses_without_parsed_text(monkeypatch):
    from backend import answer_drafter

    app = Flask(__name__)
    memory = Memory()
    monkeypatch.setattr(answer_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(answer_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(answer_drafter, "get_active_answer_task_id", lambda doc_id, key: None)

    with app.test_request_context(
        method="POST",
        json={"draftId": "draft-public-id"},
    ):
        response = answer_drafter.handle_continue(request)

    assert response == ({"message": "Please upload and parse a complaint before continuing."}, 400)


def test_answer_continue_route_stores_active_task_id(monkeypatch):
    from backend import answer_drafter

    app = Flask(__name__)
    memory = Memory({
        "complaint_cover_text": "Cover text",
        "complaint_text": '["First line"]',
    })
    monkeypatch.setattr(answer_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(answer_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(answer_drafter, "get_active_answer_task_id", lambda doc_id, key: None)
    monkeypatch.setattr(
        answer_drafter.answer_continue_task,
        "delay",
        lambda payload: SimpleNamespace(id="continue-task"),
    )

    with app.test_request_context(
        method="POST",
        json={"draftId": "draft-public-id"},
    ):
        response = answer_drafter.handle_continue(request)

    assert response == ({"result_id": "continue-task"}, 200)
    assert memory[answer_drafter.ANSWER_ACTIVE_CONTINUE_TASK_KEY] == "continue-task"


def test_answer_continue_task_uses_saved_text_and_clears_active_task(monkeypatch):
    from backend import answer_drafter

    lines = [
        "Caption text",
        "TABLE OF CONTENTS",
        "INTRODUCTION",
        "1. First line",
    ]
    memory = Memory({
        "complaint_cover_text": "Cover text",
        "complaint_text": json.dumps(lines),
        "line_metadata": json.dumps(_line_metadata_for(lines)),
        "inputs/uploaded_filename": "complaint.pdf",
        answer_drafter.ANSWER_ACTIVE_CONTINUE_TASK_KEY: "continue-task",
    })

    monkeypatch.setattr(answer_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(answer_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(answer_drafter, "_answer_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(answer_drafter, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        answer_drafter,
        "get_complaint_text_from_file",
        lambda path: (_ for _ in ()).throw(AssertionError("parser should not be called")),
    )
    # Setup formerly used separate parties/venue and court-classification calls;
    # the structure extractor now returns those fields together.
    monkeypatch.setattr(
        answer_drafter,
        "get_answer_complaint_structure_from_llm",
        lambda text_by_lines, line_metadata: {
            "plaintiffs": ["Plaintiff"],
            "defendants": ["Defendant"],
            "venue": "Federal Court",
            "court": "Federal Court",
            "jurisdiction_type": "federal",
            "state": "",
            "case_number": "case-123",
            "first_body_line_number": 2,
        },
    )
    section = {
        "start_line": 2,
        "end_line": 3,
        "title": "Facts",
        "type": "factual_allegations",
        "level": 1,
        "is_cause_of_action": False,
        "claim_name": None,
        "claim_number": None,
        "against": [],
        "by": [],
        "source_lines": [2],
        "confidence": "high",
        "reason": "Mock facts section.",
    }
    coa = {
        "start_line": 3,
        "end_line": 3,
        "title": "FIRST CAUSE OF ACTION",
        "type": "cause_of_action",
        "level": 1,
        "is_cause_of_action": True,
        "claim_name": "Mock Claim",
        "claim_number": 1,
        "against": ["Defendant"],
        "by": ["Plaintiff"],
        "source_lines": [3],
        "confidence": "high",
        "reason": "Mock COA.",
        "summary": "",
    }
    monkeypatch.setattr(
        answer_drafter,
        "get_answer_sections_and_coas_from_llm",
        lambda text_by_lines, line_metadata, first_body_line_number: {
            "sections": [section],
            "causes_of_action": [coa],
        }
        if text_by_lines == lines and first_body_line_number == 2
        else (_ for _ in ()).throw(AssertionError("section extraction should use global lines and body start")),
    )
    monkeypatch.setattr(
        answer_drafter,
        "chat_completion",
        lambda prompt: (_ for _ in ()).throw(AssertionError("summary should be skipped for federal setup")),
    )
    monkeypatch.setattr(
        answer_drafter,
        "extract_from_json",
        lambda key, output: (_ for _ in ()).throw(AssertionError("old AD extractor should not be called")),
    )
    monkeypatch.setattr(
        answer_drafter,
        "get_headers_from_llm",
        lambda lines_arg, first_body_line_number=0: (_ for _ in ()).throw(AssertionError("old heading extractor should not be called")),
    )
    monkeypatch.setattr(
        answer_drafter,
        "get_paralines_from_llm",
        lambda lines_arg, primary_start_line=0, return_warnings=False: (
            ({1: 3}, answer_drafter._empty_answer_para_warnings())
            if return_warnings
            else {1: 3}
        )
        if lines_arg == lines and primary_start_line == 2
        else (_ for _ in ()).throw(AssertionError("paragraphs should use global lines and body start")),
    )
    monkeypatch.setattr(answer_drafter, "longest_inc_sequence", lambda nums: nums)
    monkeypatch.setattr(answer_drafter, "is_consecutive_or_expand", lambda paras: paras)
    monkeypatch.setattr(
        answer_drafter,
        "get_answer_factual_digest_from_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("federal setup should defer factual digest")),
    )
    monkeypatch.setattr(
        answer_drafter,
        "get_answer_ad_suggestions_from_llm",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("federal setup should defer AD suggestions")),
    )

    result = answer_drafter.answer_continue_task.run({"draftId": "draft-public-id"})

    assert result["ads_suggestions"] == {}
    assert result["defendant"] == ["Defendant"]
    assert memory["inputs/plaintiff"] == "Plaintiff"
    assert memory["inputs/defendant"] == "Defendant"
    assert memory["inputs/case_number"] == "case-123"
    assert memory["inputs/is_federal"] == "federal"
    assert memory["inputs/state"] == ""
    assert json.loads(memory["complaint_structure"])["first_body_line_number"] == 2
    assert json.loads(memory["sections"]) == [section]
    assert json.loads(memory["causes_of_action"]) == [coa]
    assert "summary" not in memory
    assert "complaint_factual_digest" not in memory
    assert json.loads(memory["ads_suggestions"]) == {}
    assert json.loads(memory["section_headers"]) == {"Facts": 2}
    assert json.loads(memory["outline"]) == [["Facts", None], ["Paragraph 1", 1]]
    assert json.loads(memory["paragraph_warnings"]) == answer_drafter._empty_answer_para_warnings()
    assert answer_drafter.ANSWER_ACTIVE_CONTINUE_TASK_KEY not in memory


def test_rfp_state0_reset_prevents_old_generated_restore(monkeypatch):
    from backend import rfp_drafter

    app = Flask(__name__)
    memory = Memory({
        "complaint_cover_text": "old cover",
        "complaint_text": '["old body"]',
        "line_metadata": '[{"line_number": 0}]',
        "drafted_sections/rfps": "old RFP section",
        "drafted_sections/defs": "old definitions",
        "drafted_sections/instructions": "old instructions",
        "drafted_sections/intro": "old intro",
        "inputs/generated_pdf_filename": "old-rfp.pdf",
        "inputs/side": "plaintiff",
        "inputs/plaintiff": "old plaintiff",
        "inputs/defendant": "old defendant",
        "inputs/venue": "old venue",
        "inputs/is_federal": "state",
        "temp/reduced_rfps": '[{"Request": "old reduced"}]',
        "rfp/active_parse_task_id": "old-parse-task",
        "rfp/active_generate_requests_task_id": "old-generate-task",
    })
    _patch_rfp_handler(monkeypatch, rfp_drafter, memory)

    with app.test_request_context(
        method="POST",
        data={"draftId": "draft-public-id", "side": "defendant", "file": "unused"},
    ):
        response = rfp_drafter.handle_state0(request)

    assert response == ({"result_id": "task-rfp"}, 200)
    assert memory["inputs/uploaded_filename"] == "new-rfp.pdf"
    assert "complaint_cover_text" not in memory
    assert "complaint_text" not in memory
    assert "line_metadata" not in memory
    assert "drafted_sections/rfps" not in memory
    assert "drafted_sections/defs" not in memory
    assert "drafted_sections/instructions" not in memory
    assert "drafted_sections/intro" not in memory
    assert "inputs/generated_pdf_filename" not in memory
    assert "inputs/side" not in memory
    assert "inputs/plaintiff" not in memory
    assert "inputs/defendant" not in memory
    assert "inputs/venue" not in memory
    assert "inputs/is_federal" not in memory
    assert "temp/reduced_rfps" not in memory
    assert "rfp/active_parse_task_id" not in memory
    assert "rfp/active_generate_requests_task_id" not in memory

    memory["temp/consolidated_rfps"] = '[{"Request": "new request"}]'
    assert memory["temp/consolidated_rfps"]


def test_rfp_parse_task_saves_parsed_text_and_clears_active_task(monkeypatch):
    from backend import rfp_drafter

    memory = Memory({
        rfp_drafter.RFP_ACTIVE_PARSE_TASK_KEY: "parse-task",
    })
    monkeypatch.setattr(rfp_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfp_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfp_drafter, "_rfp_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        rfp_drafter,
        "get_complaint_text_with_line_metadata_from_file",
        lambda path: (
            "Cover text\nFirst line\nSecond line",
            "case",
            False,
            _line_metadata_for(["Cover text", "First line", "Second line"]),
        ),
    )

    result = rfp_drafter.rfp_parse_complaint_task.run(
        filename="complaint.pdf",
        draftId="draft-public-id",
        side="defendant",
    )

    assert result["has_parsed_complaint"] is True
    assert memory["complaint_cover_text"] == "Cover text\nFirst line\nSecond line"
    assert json.loads(memory["complaint_text"]) == ["Cover text", "First line", "Second line"]
    assert json.loads(memory["line_metadata"]) == _line_metadata_for(["Cover text", "First line", "Second line"])
    assert memory["inputs/uploaded_filename"] == "complaint.pdf"
    assert memory["inputs/side"] == "defendant"
    assert rfp_drafter.RFP_ACTIVE_PARSE_TASK_KEY not in memory


def test_rfp_generate_requests_refuses_without_parsed_text(monkeypatch):
    from backend import rfp_drafter

    app = Flask(__name__)
    memory = Memory()
    monkeypatch.setattr(rfp_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfp_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfp_drafter, "get_active_rfp_task_id", lambda doc_id, key: None)

    with app.test_request_context(
        method="POST",
        json={"draftId": "draft-public-id"},
    ):
        response = rfp_drafter.handle_generate_requests(request)

    assert response == ({"message": "Please upload and parse a complaint before generating requests."}, 400)


def test_rfp_generate_requests_route_stores_active_task_id(monkeypatch):
    from backend import rfp_drafter

    app = Flask(__name__)
    memory = Memory({
        "complaint_cover_text": "Cover text",
        "complaint_text": '["First line"]',
    })
    monkeypatch.setattr(rfp_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfp_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfp_drafter, "get_active_rfp_task_id", lambda doc_id, key: None)
    monkeypatch.setattr(
        rfp_drafter.rfp_generate_requests_task,
        "delay",
        lambda payload: SimpleNamespace(id="generate-task"),
    )

    with app.test_request_context(
        method="POST",
        json={"draftId": "draft-public-id"},
    ):
        response = rfp_drafter.handle_generate_requests(request)

    assert response == ({"result_id": "generate-task"}, 200)
    assert memory[rfp_drafter.RFP_ACTIVE_GENERATE_REQUESTS_TASK_KEY] == "generate-task"


def test_rfp_generate_task_uses_saved_text_and_clears_active_task(monkeypatch):
    from backend import rfp_drafter

    memory = Memory({
        "complaint_cover_text": "Cover text",
        "complaint_text": json.dumps(["First line"]),
        "inputs/uploaded_filename": "complaint.pdf",
        "inputs/side": "defendant",
        rfp_drafter.RFP_ACTIVE_GENERATE_REQUESTS_TASK_KEY: "generate-task",
    })
    extracted_payloads = [
        [{"Topic": "Contracts", "Request": "Produce the contract.", "Justification": "Relevant."}],
        [{"Topic": "Contracts", "Request": "Produce the contract.", "Justification": "Relevant."}],
    ]

    monkeypatch.setattr(rfp_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfp_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfp_drafter, "_rfp_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(rfp_drafter, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        rfp_drafter,
        "get_complaint_text_from_file",
        lambda path: (_ for _ in ()).throw(AssertionError("parser should not be called")),
    )
    monkeypatch.setattr(
        rfp_drafter,
        "extract_parties_venue",
        lambda cover, **kwargs: (["Plaintiff"], ["Defendant"], "Superior Court"),
    )
    monkeypatch.setattr(rfp_drafter, "chat_completion", lambda prompt, **kwargs: "STATE")
    monkeypatch.setattr(
        rfp_drafter, "chat_completion_parse",
        lambda prompt, schema, **kwargs: schema.model_validate({"Requests for Production of Documents": extracted_payloads.pop(0)}),
    )
    monkeypatch.setattr(rfp_drafter, "split_into_chunks", lambda lines, chunk_len, overlap_lines: [lines])

    result = rfp_drafter.rfp_generate_requests_task.run({"draftId": "draft-public-id"})

    assert result["requests"] == [{"Topic": "Contracts", "Request": "Produce the contract.", "Justification": "Relevant."}]
    assert json.loads(memory["temp/consolidated_rfps"]) == result["requests"]
    assert memory["inputs/plaintiff"] == "Plaintiff"
    assert memory["inputs/defendant"] == "Defendant"
    assert rfp_drafter.RFP_ACTIVE_GENERATE_REQUESTS_TASK_KEY not in memory


def test_rog_state0_reset_prevents_old_generated_restore(monkeypatch):
    from backend import rog_drafter

    app = Flask(__name__)
    memory = Memory({
        "complaint_cover_text": "old cover",
        "complaint_text": '["old body"]',
        "line_metadata": '[{"line_number": 0}]',
        "drafted_sections/rogs": "old ROG section",
        "drafted_sections/defs": "old definitions",
        "drafted_sections/instructions": "old instructions",
        "drafted_sections/intro": "old intro",
        "inputs/uploaded_filename": "old-rog.pdf",
        "inputs/generated_pdf_filename": "old-rog-output.pdf",
        "inputs/side": "plaintiff",
        "inputs/plaintiff": "old plaintiff",
        "inputs/defendant": "old defendant",
        "inputs/venue": "old venue",
        "inputs/is_federal": "state",
        "temp/reduced_rogs": '[{"Interrogatory": "old reduced"}]',
        "rog/active_parse_task_id": "old-parse-task",
        "rog/active_continue_task_id": "old-continue-task",
    })
    _patch_rog_handler(monkeypatch, rog_drafter, memory)

    with app.test_request_context(
        method="POST",
        data={"draftId": "draft-public-id", "side": "defendant", "file": "unused"},
    ):
        response = rog_drafter.handle_state0(request)

    assert response == ({"result_id": "task-rog"}, 200)
    assert memory["inputs/uploaded_filename"] == "new-rog.pdf"
    assert "complaint_cover_text" not in memory
    assert "complaint_text" not in memory
    assert "line_metadata" not in memory
    assert "drafted_sections/rogs" not in memory
    assert "drafted_sections/defs" not in memory
    assert "drafted_sections/instructions" not in memory
    assert "drafted_sections/intro" not in memory
    assert "inputs/generated_pdf_filename" not in memory
    assert "inputs/side" not in memory
    assert "inputs/plaintiff" not in memory
    assert "inputs/defendant" not in memory
    assert "inputs/venue" not in memory
    assert "inputs/is_federal" not in memory
    assert "temp/reduced_rogs" not in memory
    assert "rog/active_parse_task_id" not in memory
    assert "rog/active_continue_task_id" not in memory

    memory["temp/consolidated_rogs"] = '[{"Interrogatory": "new rog"}]'
    assert memory["temp/consolidated_rogs"]


def test_rog_upload_route_stores_active_parse_task_id(monkeypatch):
    from backend import rog_drafter

    app = Flask(__name__)
    memory = Memory()
    monkeypatch.setattr(rog_drafter, "handle_file_upload", lambda request, folder: ("new-rog.pdf", None))
    monkeypatch.setattr(rog_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rog_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rog_drafter, "get_active_rog_task_id", lambda doc_id, key: None)
    monkeypatch.setattr(
        rog_drafter.rog_parse_complaint_task,
        "delay",
        lambda **kwargs: SimpleNamespace(id="parse-task"),
    )

    with app.test_request_context(
        method="POST",
        data={"draftId": "draft-public-id", "side": "defendant", "file": "unused"},
    ):
        response = rog_drafter.handle_upload_complaint(request)

    assert response == ({"result_id": "parse-task", "uploaded_filename": "new-rog.pdf"}, 200)
    assert memory[rog_drafter.ROG_ACTIVE_PARSE_TASK_KEY] == "parse-task"


def test_rog_parse_task_saves_parsed_text_and_clears_active_task(monkeypatch):
    from backend import rog_drafter

    memory = Memory({
        rog_drafter.ROG_ACTIVE_PARSE_TASK_KEY: "parse-task",
    })
    monkeypatch.setattr(rog_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rog_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rog_drafter, "_rog_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(rog_drafter, "get_last_ocr_warning", lambda: False)
    monkeypatch.setattr(
        rog_drafter,
        "get_complaint_text_with_line_metadata_from_file",
        lambda path: (
            "Cover text\nFirst line\nSecond line",
            "case",
            False,
            _line_metadata_for(["Cover text", "First line", "Second line"]),
        ),
    )

    result = rog_drafter.rog_parse_complaint_task.run(
        filename="complaint.pdf",
        draftId="draft-public-id",
        side="defendant",
    )

    assert result["has_parsed_complaint"] is True
    assert memory["complaint_cover_text"] == "Cover text\nFirst line\nSecond line"
    assert json.loads(memory["complaint_text"]) == ["Cover text", "First line", "Second line"]
    assert json.loads(memory["line_metadata"]) == _line_metadata_for(["Cover text", "First line", "Second line"])
    assert memory["inputs/uploaded_filename"] == "complaint.pdf"
    assert memory["inputs/side"] == "defendant"
    assert rog_drafter.ROG_ACTIVE_PARSE_TASK_KEY not in memory


def test_rog_continue_refuses_without_parsed_text(monkeypatch):
    from backend import rog_drafter

    app = Flask(__name__)
    memory = Memory()
    monkeypatch.setattr(rog_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rog_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rog_drafter, "get_active_rog_task_id", lambda doc_id, key: None)

    with app.test_request_context(
        method="POST",
        json={"draftId": "draft-public-id"},
    ):
        response = rog_drafter.handle_continue(request)

    assert response == ({"message": "Please upload and parse a complaint before continuing."}, 400)


def test_rog_continue_route_stores_active_task_id(monkeypatch):
    from backend import rog_drafter

    app = Flask(__name__)
    memory = Memory({
        "complaint_cover_text": "Cover text",
        "complaint_text": '["First line"]',
    })
    monkeypatch.setattr(rog_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rog_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rog_drafter, "get_active_rog_task_id", lambda doc_id, key: None)
    monkeypatch.setattr(
        rog_drafter.rog_continue_task,
        "delay",
        lambda payload: SimpleNamespace(id="continue-task"),
    )

    with app.test_request_context(
        method="POST",
        json={"draftId": "draft-public-id"},
    ):
        response = rog_drafter.handle_continue(request)

    assert response == ({"result_id": "continue-task"}, 200)
    assert memory[rog_drafter.ROG_ACTIVE_CONTINUE_TASK_KEY] == "continue-task"


def test_rog_continue_task_uses_saved_text_and_clears_active_task(monkeypatch):
    from backend import rog_drafter

    memory = Memory({
        "complaint_cover_text": "Cover text",
        "complaint_text": json.dumps(["First line"]),
        "inputs/uploaded_filename": "complaint.pdf",
        "inputs/side": "defendant",
        rog_drafter.ROG_ACTIVE_CONTINUE_TASK_KEY: "continue-task",
    })
    extracted_payloads = [
        [{"Topic": "Contracts", "Interrogatory": "Identify all contract witnesses.", "Rationale": "Relevant."}],
        [{"Topic": "Contracts", "Interrogatory": "Identify all contract witnesses.", "Rationale": "Relevant."}],
    ]

    monkeypatch.setattr(rog_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rog_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rog_drafter, "_rog_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(rog_drafter, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        rog_drafter,
        "get_complaint_text_from_file",
        lambda path: (_ for _ in ()).throw(AssertionError("parser should not be called")),
    )
    monkeypatch.setattr(
        rog_drafter,
        "extract_parties_venue",
        lambda cover, **kwargs: (["Plaintiff"], ["Defendant"], "Superior Court"),
    )
    monkeypatch.setattr(rog_drafter, "chat_completion", lambda prompt, **kwargs: "STATE")
    monkeypatch.setattr(
        rog_drafter, "chat_completion_parse",
        lambda prompt, schema, **kwargs: schema.model_validate({"INTERROGATORIES": extracted_payloads.pop(0)}),
    )
    monkeypatch.setattr(rog_drafter, "split_into_chunks", lambda lines, chunk_len, overlap_lines: [lines])

    result = rog_drafter.rog_continue_task.run({"draftId": "draft-public-id"})

    assert result["interrogatories"] == [{
        "Topic": "Contracts",
        "Interrogatory": "Identify all contract witnesses.",
        "Rationale": "Relevant.",
    }]
    assert json.loads(memory["temp/consolidated_rogs"]) == result["interrogatories"]
    assert memory["inputs/plaintiff"] == "Plaintiff"
    assert memory["inputs/defendant"] == "Defendant"
    assert rog_drafter.ROG_ACTIVE_CONTINUE_TASK_KEY not in memory


def test_rfa_state0_reset_prevents_old_generated_restore(monkeypatch):
    from backend import rfa_drafter

    app = Flask(__name__)
    memory = Memory({
        "complaint_cover_text": "old cover",
        "complaint_text": '["old body"]',
        "line_metadata": '[{"line_number": 0}]',
        "drafted_sections/rfas": "old RFA section",
        "drafted_sections/defs": "old definitions",
        "drafted_sections/instructions": "old instructions",
        "drafted_sections/intro": "old intro",
        "inputs/uploaded_filename": "old-rfa.pdf",
        "inputs/generated_pdf_filename": "old-rfa-output.pdf",
        "inputs/side": "plaintiff",
        "inputs/plaintiff": "old plaintiff",
        "inputs/defendant": "old defendant",
        "inputs/venue": "old venue",
        "inputs/is_federal": "state",
        "temp/reduced_rfas": '[{"Admission Request": "old reduced"}]',
        "rfa/active_parse_task_id": "old-parse-task",
        "rfa/active_continue_task_id": "old-continue-task",
    })
    _patch_rfa_handler(monkeypatch, rfa_drafter, memory)

    with app.test_request_context(
        method="POST",
        data={"draftId": "draft-public-id", "side": "defendant", "file": "unused"},
    ):
        response = rfa_drafter.handle_state0(request)

    assert response == ({"result_id": "task-rfa"}, 200)
    assert memory["inputs/uploaded_filename"] == "new-rfa.pdf"
    assert "complaint_cover_text" not in memory
    assert "complaint_text" not in memory
    assert "line_metadata" not in memory
    assert "drafted_sections/rfas" not in memory
    assert "drafted_sections/defs" not in memory
    assert "drafted_sections/instructions" not in memory
    assert "drafted_sections/intro" not in memory
    assert "inputs/generated_pdf_filename" not in memory
    assert "inputs/side" not in memory
    assert "inputs/plaintiff" not in memory
    assert "inputs/defendant" not in memory
    assert "inputs/venue" not in memory
    assert "inputs/is_federal" not in memory
    assert "temp/reduced_rfas" not in memory
    assert "rfa/active_parse_task_id" not in memory
    assert "rfa/active_continue_task_id" not in memory

    memory["temp/consolidated_rfas"] = '[{"Admission Request": "new rfa"}]'
    assert memory["temp/consolidated_rfas"]


def test_rfa_upload_route_stores_active_parse_task_id(monkeypatch):
    from backend import rfa_drafter

    app = Flask(__name__)
    memory = Memory()
    monkeypatch.setattr(rfa_drafter, "handle_file_upload", lambda request, folder: ("new-rfa.pdf", None))
    monkeypatch.setattr(rfa_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfa_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfa_drafter, "get_active_rfa_task_id", lambda doc_id, key: None)
    monkeypatch.setattr(
        rfa_drafter.rfa_parse_complaint_task,
        "delay",
        lambda **kwargs: SimpleNamespace(id="parse-task"),
    )

    with app.test_request_context(
        method="POST",
        data={"draftId": "draft-public-id", "side": "defendant", "file": "unused"},
    ):
        response = rfa_drafter.handle_upload_complaint(request)

    assert response == ({"result_id": "parse-task", "uploaded_filename": "new-rfa.pdf"}, 200)
    assert memory[rfa_drafter.RFA_ACTIVE_PARSE_TASK_KEY] == "parse-task"


def test_rfa_parse_task_saves_parsed_text_and_clears_active_task(monkeypatch):
    from backend import rfa_drafter

    memory = Memory({
        rfa_drafter.RFA_ACTIVE_PARSE_TASK_KEY: "parse-task",
    })
    monkeypatch.setattr(rfa_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfa_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfa_drafter, "_rfa_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(rfa_drafter, "get_last_ocr_warning", lambda: False)
    monkeypatch.setattr(
        rfa_drafter,
        "get_complaint_text_with_line_metadata_from_file",
        lambda path: (
            "Cover text\nFirst line\nSecond line",
            "case",
            False,
            _line_metadata_for(["Cover text", "First line", "Second line"]),
        ),
    )

    result = rfa_drafter.rfa_parse_complaint_task.run(
        filename="complaint.pdf",
        draftId="draft-public-id",
        side="defendant",
    )

    assert result["has_parsed_complaint"] is True
    assert memory["complaint_cover_text"] == "Cover text\nFirst line\nSecond line"
    assert json.loads(memory["complaint_text"]) == ["Cover text", "First line", "Second line"]
    assert json.loads(memory["line_metadata"]) == _line_metadata_for(["Cover text", "First line", "Second line"])
    assert memory["inputs/uploaded_filename"] == "complaint.pdf"
    assert memory["inputs/side"] == "defendant"
    assert rfa_drafter.RFA_ACTIVE_PARSE_TASK_KEY not in memory


def test_rfa_continue_refuses_without_parsed_text(monkeypatch):
    from backend import rfa_drafter

    app = Flask(__name__)
    memory = Memory()
    monkeypatch.setattr(rfa_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfa_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfa_drafter, "get_active_rfa_task_id", lambda doc_id, key: None)

    with app.test_request_context(
        method="POST",
        json={"draftId": "draft-public-id"},
    ):
        response = rfa_drafter.handle_continue(request)

    assert response == ({"message": "Please upload and parse a complaint before continuing."}, 400)


def test_rfa_continue_route_stores_active_task_id(monkeypatch):
    from backend import rfa_drafter

    app = Flask(__name__)
    memory = Memory({
        "complaint_cover_text": "Cover text",
        "complaint_text": '["First line"]',
    })
    monkeypatch.setattr(rfa_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfa_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfa_drafter, "get_active_rfa_task_id", lambda doc_id, key: None)
    monkeypatch.setattr(
        rfa_drafter.rfa_continue_task,
        "delay",
        lambda payload: SimpleNamespace(id="continue-task"),
    )

    with app.test_request_context(
        method="POST",
        json={"draftId": "draft-public-id"},
    ):
        response = rfa_drafter.handle_continue(request)

    assert response == ({"result_id": "continue-task"}, 200)
    assert memory[rfa_drafter.RFA_ACTIVE_CONTINUE_TASK_KEY] == "continue-task"


def test_rfa_continue_task_uses_saved_text_and_clears_active_task(monkeypatch):
    from backend import rfa_drafter

    memory = Memory({
        "complaint_cover_text": "Cover text",
        "complaint_text": json.dumps(["First line"]),
        "inputs/uploaded_filename": "complaint.pdf",
        "inputs/side": "defendant",
        rfa_drafter.RFA_ACTIVE_CONTINUE_TASK_KEY: "continue-task",
    })
    extracted_payloads = [
        [{"Topic": "Contracts", "Admission Request": "Admit the contract exists.", "Strategic Rationale": "Relevant."}],
        [{"Topic": "Contracts", "Admission Request": "Admit the contract exists.", "Strategic Rationale": "Relevant."}],
    ]

    monkeypatch.setattr(rfa_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfa_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfa_drafter, "_rfa_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(rfa_drafter, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        rfa_drafter,
        "get_complaint_text_from_file",
        lambda path: (_ for _ in ()).throw(AssertionError("parser should not be called")),
    )
    monkeypatch.setattr(
        rfa_drafter,
        "extract_parties_venue",
        lambda cover, **kwargs: (["Plaintiff"], ["Defendant"], "Superior Court"),
    )
    monkeypatch.setattr(rfa_drafter, "chat_completion", lambda prompt, **kwargs: "STATE")
    monkeypatch.setattr(
        rfa_drafter, "chat_completion_parse",
        lambda prompt, schema, **kwargs: schema.model_validate({"Requests for Admission": extracted_payloads.pop(0)}),
    )
    monkeypatch.setattr(rfa_drafter, "split_into_chunks", lambda lines, chunk_len, overlap_lines: [lines])

    result = rfa_drafter.rfa_continue_task.run({"draftId": "draft-public-id"})

    assert result["admissions"] == [{
        "Topic": "Contracts",
        "Admission Request": "Admit the contract exists.",
        "Strategic Rationale": "Relevant.",
    }]
    assert json.loads(memory["temp/consolidated_rfas"]) == result["admissions"]
    assert memory["inputs/plaintiff"] == "Plaintiff"
    assert memory["inputs/defendant"] == "Defendant"
    assert rfa_drafter.RFA_ACTIVE_CONTINUE_TASK_KEY not in memory


def test_rfp_response_state0_reset_prevents_old_generated_restore(monkeypatch):
    from backend import rfp_response_drafter

    app = Flask(__name__)
    memory = Memory({
        "cover_text": "old cover",
        "body_text": '["old body"]',
        "analysis/intro": "old intro",
        "analysis/general": "old general",
        "analysis/definition_objections": "old definitions",
        "analysis/instruction_objections": "old instructions",
        "drafted_sections/definitions": '[{"old": "definition"}]',
        "drafted_sections/instructions": '[{"old": "instruction"}]',
        "drafted_sections/intro": "old drafted intro",
        "drafted_sections/gen_obj": "old drafted general",
        "drafted_sections/requests": '[{"old": "request"}]',
        "drafted_sections/rfp_response_text": "old response",
        "inputs/uploaded_filename": "old-rfp-response.pdf",
        "inputs/case_number": "old case",
        "inputs/side": "plaintiff",
        "inputs/requests_start_end_i": "1,2",
        "inputs/generated_pdf_filename": "old-output.pdf",
        "inputs/plaintiff": "old plaintiff",
        "inputs/defendant": "old defendant",
        "inputs/venue": "old venue",
        "rfp_response/active_parse_task_id": "old-parse-task",
        "rfp_response/active_continue_task_id": "old-continue-task",
    })
    _patch_rfp_response_handler(monkeypatch, rfp_response_drafter, memory)

    with app.test_request_context(
        method="POST",
        data={"draftId": "draft-public-id", "side": "defendant", "file": "unused"},
    ):
        response = rfp_response_drafter.handle_state0(request)

    assert response == ({"result_id": "task-rfp-response"}, 200)
    assert memory["inputs/uploaded_filename"] == "new-rfp-response.pdf"
    assert "cover_text" not in memory
    assert "body_text" not in memory
    assert "analysis/intro" not in memory
    assert "analysis/general" not in memory
    assert "analysis/definition_objections" not in memory
    assert "analysis/instruction_objections" not in memory
    assert "drafted_sections/definitions" not in memory
    assert "drafted_sections/instructions" not in memory
    assert "drafted_sections/intro" not in memory
    assert "drafted_sections/gen_obj" not in memory
    assert "drafted_sections/requests" not in memory
    assert "drafted_sections/rfp_response_text" not in memory
    assert "inputs/case_number" not in memory
    assert "inputs/side" not in memory
    assert "inputs/requests_start_end_i" not in memory
    assert "inputs/generated_pdf_filename" not in memory
    assert "inputs/plaintiff" not in memory
    assert "inputs/defendant" not in memory
    assert "inputs/venue" not in memory
    assert "rfp_response/active_parse_task_id" not in memory
    assert "rfp_response/active_continue_task_id" not in memory


def test_rfp_response_upload_route_stores_active_parse_task_id(monkeypatch):
    from backend import rfp_response_drafter

    app = Flask(__name__)
    memory = Memory()
    monkeypatch.setattr(rfp_response_drafter, "handle_file_upload", lambda request, folder: ("new-rfp-response.pdf", None))
    monkeypatch.setattr(rfp_response_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfp_response_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfp_response_drafter, "get_active_rfp_response_task_id", lambda doc_id, key: None)
    monkeypatch.setattr(
        rfp_response_drafter.rfp_response_parse_document_task,
        "delay",
        lambda **kwargs: SimpleNamespace(id="parse-task"),
    )

    with app.test_request_context(
        method="POST",
        data={"draftId": "draft-public-id", "side": "defendant", "file": "unused"},
    ):
        response = rfp_response_drafter.handle_upload_rfp(request)

    assert response == ({"result_id": "parse-task", "uploaded_filename": "new-rfp-response.pdf"}, 200)
    assert memory[rfp_response_drafter.RFP_RESPONSE_ACTIVE_PARSE_TASK_KEY] == "parse-task"


def test_rfp_response_parse_task_saves_parsed_text_and_clears_active_task(monkeypatch):
    from backend import rfp_response_drafter

    memory = Memory({
        rfp_response_drafter.RFP_RESPONSE_ACTIVE_PARSE_TASK_KEY: "parse-task",
    })
    monkeypatch.setattr(rfp_response_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfp_response_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfp_response_drafter, "_rfp_response_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(rfp_response_drafter, "get_last_ocr_warning", lambda: False)
    monkeypatch.setattr(
        rfp_response_drafter,
        "get_complaint_text_from_file",
        lambda path: ("Cover text<CoverPage>Request No. 1\nProduce documents", "case", False),
    )

    result = rfp_response_drafter.rfp_response_parse_document_task.run(
        filename="rfp.pdf",
        draftId="draft-public-id",
        side="defendant",
    )

    assert result["has_parsed_document"] is True
    assert memory["cover_text"] == "Cover text"
    assert json.loads(memory["body_text"]) == ["Request No. 1", "Produce documents"]
    assert memory["inputs/uploaded_filename"] == "rfp.pdf"
    assert memory["inputs/case_number"] == "case"
    assert memory["inputs/side"] == "defendant"
    assert rfp_response_drafter.RFP_RESPONSE_ACTIVE_PARSE_TASK_KEY not in memory


def test_rfp_response_continue_refuses_without_parsed_text(monkeypatch):
    from backend import rfp_response_drafter

    app = Flask(__name__)
    memory = Memory()
    monkeypatch.setattr(rfp_response_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfp_response_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfp_response_drafter, "get_active_rfp_response_task_id", lambda doc_id, key: None)

    with app.test_request_context(
        method="POST",
        json={"draftId": "draft-public-id"},
    ):
        response = rfp_response_drafter.handle_continue_step0(request)

    assert response == ({"message": "Please upload and parse RFPs before continuing."}, 400)


def test_rfp_response_continue_route_stores_active_task_id(monkeypatch):
    from backend import rfp_response_drafter

    app = Flask(__name__)
    memory = Memory({
        "cover_text": "Cover text",
        "body_text": '["Request No. 1"]',
    })
    monkeypatch.setattr(rfp_response_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfp_response_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfp_response_drafter, "get_active_rfp_response_task_id", lambda doc_id, key: None)
    monkeypatch.setattr(
        rfp_response_drafter.rfp_response_continue_task,
        "delay",
        lambda payload: SimpleNamespace(id="continue-task"),
    )

    with app.test_request_context(
        method="POST",
        json={"draftId": "draft-public-id"},
    ):
        response = rfp_response_drafter.handle_continue_step0(request)

    assert response == ({"result_id": "continue-task"}, 200)
    assert memory[rfp_response_drafter.RFP_RESPONSE_ACTIVE_CONTINUE_TASK_KEY] == "continue-task"


def test_rfp_response_continue_task_uses_saved_text_and_clears_active_task(monkeypatch):
    from backend import rfp_response_drafter

    memory = Memory({
        "cover_text": "Cover text",
        "body_text": json.dumps(["Definitions", "Request No. 1", "Produce documents"]),
        "inputs/uploaded_filename": "rfp.pdf",
        "inputs/side": "defendant",
        rfp_response_drafter.RFP_RESPONSE_ACTIVE_CONTINUE_TASK_KEY: "continue-task",
    })

    monkeypatch.setattr(rfp_response_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rfp_response_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rfp_response_drafter, "_rfp_response_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        rfp_response_drafter,
        "get_complaint_text_from_file",
        lambda path: (_ for _ in ()).throw(AssertionError("parser should not be called")),
    )
    extraction_calls = []

    def extract_parties(cover, **kwargs):
        extraction_calls.append((cover, kwargs))
        return ["Plaintiff LLC"], ["Defendant Inc."], "Superior Court"

    monkeypatch.setattr(rfp_response_drafter, "extract_parties_venue", extract_parties)
    monkeypatch.setattr(rfp_response_drafter, "get_rfp_section_text", lambda lines, section: [1, 3])
    monkeypatch.setattr(rfp_response_drafter, "chat_completion_j", lambda prompt, **kwargs: "{}")
    monkeypatch.setattr(
        rfp_response_drafter,
        "extract_from_json",
        lambda key, output: [{"item": key, "objection": f"{key} objection."}],
    )

    result = rfp_response_drafter.rfp_response_continue_task.run({"draftId": "draft-public-id"})

    assert result["definition_objections"] == "definitions objection.\n"
    assert result["instruction_objections"] == "instructions objection.\n"
    assert result["intro"].startswith("Defendant Defendant")
    assert json.loads(memory["drafted_sections/definitions"]) == [{"item": "definitions", "objection": "definitions objection."}]
    assert memory["inputs/plaintiff"] == "Plaintiff LLC"
    assert memory["inputs/defendant"] == "Defendant"
    assert extraction_calls == [
        ("Cover text", {"model": "gpt-6-luna", "reasoning_effort": "medium"})
    ]
    assert rfp_response_drafter.RFP_RESPONSE_ACTIVE_CONTINUE_TASK_KEY not in memory


def test_rog_response_state0_reset_prevents_old_generated_restore(monkeypatch):
    from backend import rog_response_drafter

    app = Flask(__name__)
    memory = Memory({
        "cover_text": "old cover",
        "body_text": '["old body"]',
        "drafted_sections/intro": "old intro",
        "drafted_sections/gen_obj": "old general",
        "drafted_sections/interrogatories": '[{"old": "rog"}]',
        "drafted_sections/rog_response_text": "old response",
        "inputs/uploaded_filename": "old-rog-response.pdf",
        "inputs/case_number": "old case",
        "inputs/side_selection": "plaintiff",
        "inputs/side": "Plaintiffs",
        "inputs/plaintiff": "old plaintiff",
        "inputs/defendant": "old defendant",
        "inputs/title": "old title",
        "inputs/venue": "old venue",
        "inputs/is_federal": "state",
        "inputs/generated_pdf_filename": "old-output.pdf",
        "rog_response/active_parse_task_id": "old-parse-task",
        "rog_response/active_continue_task_id": "old-continue-task",
    })
    _patch_rog_response_handler(monkeypatch, rog_response_drafter, memory)

    with app.test_request_context(
        method="POST",
        data={"draftId": "draft-public-id", "side": "defendant", "file": "unused"},
    ):
        response = rog_response_drafter.handle_state0(request)

    assert response == ({"result_id": "task-rog-response"}, 200)
    assert memory["inputs/uploaded_filename"] == "new-rog-response.pdf"
    assert "cover_text" not in memory
    assert "body_text" not in memory
    assert "drafted_sections/intro" not in memory
    assert "drafted_sections/gen_obj" not in memory
    assert "drafted_sections/interrogatories" not in memory
    assert "drafted_sections/rog_response_text" not in memory
    assert "inputs/case_number" not in memory
    assert "inputs/side_selection" not in memory
    assert "inputs/side" not in memory
    assert "inputs/plaintiff" not in memory
    assert "inputs/defendant" not in memory
    assert "inputs/title" not in memory
    assert "inputs/venue" not in memory
    assert "inputs/is_federal" not in memory
    assert "inputs/generated_pdf_filename" not in memory
    assert "rog_response/active_parse_task_id" not in memory
    assert "rog_response/active_continue_task_id" not in memory


def test_rog_response_upload_route_stores_active_parse_task_id(monkeypatch):
    from backend import rog_response_drafter

    app = Flask(__name__)
    memory = Memory()
    monkeypatch.setattr(rog_response_drafter, "handle_file_upload", lambda request, folder: ("new-rog-response.pdf", None))
    monkeypatch.setattr(rog_response_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rog_response_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rog_response_drafter, "get_active_rog_response_task_id", lambda doc_id, key: None)
    monkeypatch.setattr(
        rog_response_drafter.rog_response_parse_document_task,
        "delay",
        lambda **kwargs: SimpleNamespace(id="parse-task"),
    )

    with app.test_request_context(
        method="POST",
        data={"draftId": "draft-public-id", "side": "defendant", "file": "unused"},
    ):
        response = rog_response_drafter.handle_upload_rog(request)

    assert response == ({"result_id": "parse-task", "uploaded_filename": "new-rog-response.pdf"}, 200)
    assert memory[rog_response_drafter.ROG_RESPONSE_ACTIVE_PARSE_TASK_KEY] == "parse-task"


def test_rog_response_parse_task_saves_parsed_text_and_clears_active_task(monkeypatch):
    from backend import rog_response_drafter

    memory = Memory({
        rog_response_drafter.ROG_RESPONSE_ACTIVE_PARSE_TASK_KEY: "parse-task",
    })
    monkeypatch.setattr(rog_response_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rog_response_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rog_response_drafter, "_rog_response_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(rog_response_drafter, "get_last_ocr_warning", lambda: False)
    monkeypatch.setattr(
        rog_response_drafter,
        "get_complaint_text_from_file",
        lambda path: ("Cover text<CoverPage>Interrogatory No. 1\nIdentify witnesses", "case", False),
    )

    result = rog_response_drafter.rog_response_parse_document_task.run(
        filename="rogs.pdf",
        draftId="draft-public-id",
        side="defendant",
    )

    assert result["has_parsed_document"] is True
    assert memory["cover_text"] == "Cover text"
    assert json.loads(memory["body_text"]) == ["Interrogatory No. 1", "Identify witnesses"]
    assert memory["inputs/uploaded_filename"] == "rogs.pdf"
    assert memory["inputs/case_number"] == "case"
    assert memory["inputs/side_selection"] == "defendant"
    assert rog_response_drafter.ROG_RESPONSE_ACTIVE_PARSE_TASK_KEY not in memory


def test_rog_response_continue_refuses_without_parsed_text(monkeypatch):
    from backend import rog_response_drafter

    app = Flask(__name__)
    memory = Memory()
    monkeypatch.setattr(rog_response_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rog_response_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rog_response_drafter, "get_active_rog_response_task_id", lambda doc_id, key: None)

    with app.test_request_context(
        method="POST",
        json={"draftId": "draft-public-id"},
    ):
        response = rog_response_drafter.handle_continue_step0(request)

    assert response == ({"message": "Please upload and parse interrogatories before continuing."}, 400)


def test_rog_response_continue_route_stores_active_task_id(monkeypatch):
    from backend import rog_response_drafter

    app = Flask(__name__)
    memory = Memory({
        "cover_text": "Cover text",
        "body_text": '["Interrogatory No. 1"]',
    })
    monkeypatch.setattr(rog_response_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rog_response_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rog_response_drafter, "get_active_rog_response_task_id", lambda doc_id, key: None)
    monkeypatch.setattr(
        rog_response_drafter.rog_response_continue_task,
        "delay",
        lambda payload: SimpleNamespace(id="continue-task"),
    )

    with app.test_request_context(
        method="POST",
        json={"draftId": "draft-public-id"},
    ):
        response = rog_response_drafter.handle_continue_step0(request)

    assert response == ({"result_id": "continue-task"}, 200)
    assert memory[rog_response_drafter.ROG_RESPONSE_ACTIVE_CONTINUE_TASK_KEY] == "continue-task"


def test_rog_response_continue_task_uses_saved_text_and_clears_active_task(monkeypatch):
    from backend import rog_response_drafter

    memory = Memory({
        "cover_text": "Cover text",
        "body_text": json.dumps(["Interrogatory No. 1", "Identify witnesses"]),
        "inputs/uploaded_filename": "rogs.pdf",
        "inputs/side_selection": "defendant",
        rog_response_drafter.ROG_RESPONSE_ACTIVE_CONTINUE_TASK_KEY: "continue-task",
    })

    monkeypatch.setattr(rog_response_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(rog_response_drafter, "DBMemory", lambda db, doc_id: memory)
    monkeypatch.setattr(rog_response_drafter, "_rog_response_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        rog_response_drafter,
        "get_complaint_text_from_file",
        lambda path: (_ for _ in ()).throw(AssertionError("parser should not be called")),
    )
    monkeypatch.setattr(
        rog_response_drafter,
        "extract_title_parties_venue",
        lambda cover, **kwargs: (["Plaintiff LLC"], ["Defendant Inc."], "Test Case", "Superior Court"),
    )
    monkeypatch.setattr(rog_response_drafter, "chat_completion", lambda prompt, *args, **kwargs: "STATE")
    monkeypatch.setattr(
        rog_response_drafter,
        "chat_completion_parse",
        lambda prompt, response_model, **kwargs: (
            rog_response_drafter.ROGIntroduction(introduction="Intro text")
            if response_model is rog_response_drafter.ROGIntroduction
            else rog_response_drafter.ROGInterrogatoryList(interrogatories=[
                rog_response_drafter.ROGInterrogatory(
                    interrogatory_id="Interrogatory No. 1", interrogatory="Identify witnesses"
                )
            ])
        ),
    )
    monkeypatch.setattr(rog_response_drafter, "num_tokens_from_string", lambda prompt: 100)

    result = rog_response_drafter.rog_response_continue_task.run({"draftId": "draft-public-id"})

    assert result["intro"] == "Intro text"
    assert result["interrogatories"] == [{
        "interrogatory_id": "Interrogatory No. 1\n",
        "interrogatory": "Identify witnesses\n",
        "objection": "",
        "response": "",
    }]
    assert json.loads(memory["drafted_sections/interrogatories"]) == result["interrogatories"]
    assert memory["inputs/plaintiff"] == "Plaintiff LLC"
    assert memory["inputs/defendant"] == "Defendant"
    assert memory["inputs/side"] == "Defendant"
    assert rog_response_drafter.ROG_RESPONSE_ACTIVE_CONTINUE_TASK_KEY not in memory
