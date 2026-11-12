import pytest
import inspect
import json
from types import SimpleNamespace

from backend import dismiss_drafter
from backend.ai import base as ai_base


fa_cases_from_llm = {
    "COUNT I": {
        "facts": [
            "Plaintiff alleges that Defendant made false statements about the product (Compl. ¶12).",
            "Plaintiff claims economic damages resulted from reliance on these statements (Compl. ¶18).",
            "Plaintiff alleges the statements were made with intent to deceive (Compl. ¶25)."
        ],
        "cases": [
            "Ashcroft v. Iqbal, 556 U.S. 662 (2009) (holding that conclusory allegations without factual enhancement are insufficient to state a claim).",
            "Bell Atl. Corp. v. Twombly, 550 U.S. 544 (2007) (requiring a claim to have sufficient factual matter to be plausible on its face).",
            "McDonnell Douglas Corp. v. Green, 411 U.S. 792 (1973) (establishing a burden-shifting framework for discrimination claims, relevant here for Plaintiff's reliance arguments)."
        ],
        "laws": [
            "Federal Rule of Civil Procedure 12(b)(6) (motion to dismiss for failure to state a claim).",
            "Restatement (Second) of Torts § 525 (defining fraudulent misrepresentation).",
            "15 U.S.C. § 45 (FTC Act, addressing unfair or deceptive acts or practices)."
        ]
    },
    "COUNT II": {
        "facts": [
            "Plaintiff asserts Defendant breached a contract by failing to deliver goods (Compl. ¶32).",
            "Plaintiff acknowledges no written contract exists but claims an oral agreement (Compl. ¶38).",
            "Plaintiff alleges detrimental reliance on Defendant's promise (Compl. ¶41)."
        ],
        "cases": [
            "Walker v. KFC Corp., 728 F.2d 1215 (9th Cir. 1984) (holding that oral agreements may be unenforceable under the statute of frauds).",
            "Hamer v. Sidway, 124 N.Y. 538 (1891) (establishing that consideration is necessary to enforce a contract).",
            "Dura Pharms., Inc. v. Broudo, 544 U.S. 336 (2005) (holding that conclusory allegations of reliance without economic loss are insufficient)."
        ],
        "laws": [
            "Uniform Commercial Code § 2-201 (statute of frauds for contracts for the sale of goods).",
            "Restatement (Second) of Contracts § 90 (promissory estoppel doctrine).",
            "Federal Rule of Civil Procedure 12(b)(6) (motion to dismiss for failure to state a claim)."
        ]
    }
}


def test_fas_cases_2_str():
    pytest.skip("fas_cases_2_str only exists in dismiss_drafter.ipynb, not in dismiss_drafter.py")


def test_factual_background_synthesis_uses_sol(monkeypatch):
    captured = {}

    def fake_chat_completion_j(prompt, model, reasoning_effort):
        captured.update(prompt=prompt, model=model, reasoning_effort=reasoning_effort)
        return '{"background": []}'

    monkeypatch.setattr(dismiss_drafter, "chat_completion_j", fake_chat_completion_j)

    assert dismiss_drafter._synthesize_factual_background("factual background prompt") == '{"background": []}'
    assert captured == {
        "prompt": "factual background prompt",
        "model": "gpt-6.1-sol",
        "reasoning_effort": "high",
    }


def test_updated_background_uses_typed_parse_and_preserves_response(monkeypatch):
    memory = {
        "inputs/fa_sums": "Complaint summary",
        "inputs/case_info": "Case information",
    }
    captured = {}

    monkeypatch.setattr(dismiss_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(dismiss_drafter, "DBMemory", lambda **kwargs: memory)

    def parse(**kwargs):
        captured.update(kwargs)
        parsed = dismiss_drafter.UpdatedFactualBackground.model_validate({
            "background": [
                {"sub-heading": "The Parties", "paragraphs": ["First paragraph.", "Second paragraph."]},
            ],
        })
        return SimpleNamespace(
            id="background-route", model=kwargs["model"], status="completed",
            output_parsed=parsed, output=[], usage=None,
        )

    monkeypatch.setattr(ai_base, "get_openai_client", lambda: SimpleNamespace(
        responses=SimpleNamespace(parse=parse),
    ))
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(ai_base, "record_guided_llm_response", lambda *_a, **_kw: None)

    result, status = dismiss_drafter.route_motion_dismiss_update_background.__wrapped__(
        SimpleNamespace(form={
            "draftId": "public-draft",
            "newfas": "Additional fact",
            "background": "Original background",
        })
    )

    assert status == 200
    assert result == {
        "background": "\n**The Parties**\nFirst paragraph.\nSecond paragraph.\n",
        "message": "Updated background from LLM",
    }
    assert memory["drafted_sections/fa_background"] == result["background"]
    assert "Original background" in captured["input"][-2]["content"]
    assert "Complaint summary" in captured["input"][-3]["content"]
    assert "Additional fact" in captured["input"][-1]["content"]
    assert captured["text_format"] is dismiss_drafter.UpdatedFactualBackground
    assert captured["model"] == "gpt-6.1-sol"
    assert captured["reasoning"] == {"effort": "high"}


def test_failed_background_parse_does_not_overwrite_draft(monkeypatch):
    memory = {
        "inputs/fa_sums": "Complaint summary",
        "inputs/case_info": "Case information",
        "drafted_sections/fa_background": "Saved background",
    }
    monkeypatch.setattr(dismiss_drafter.Drafts, "resolve_id", lambda draft_id: 42)
    monkeypatch.setattr(dismiss_drafter, "DBMemory", lambda **kwargs: memory)
    monkeypatch.setattr(dismiss_drafter, "multichat_completion_parse", lambda *args, **kwargs: None)

    result, status = dismiss_drafter.route_motion_dismiss_update_background.__wrapped__(
        SimpleNamespace(form={"draftId": "public-draft", "background": "Current background"})
    )

    assert status == 502
    assert result == {"message": "Failed to update background from LLM"}
    assert memory["drafted_sections/fa_background"] == "Saved background"


@pytest.mark.parametrize("stage,expected_efforts", [
    ("setup", ["medium", "medium", "high", "high"]),
    ("analysis", ["high", "high"]),
    ("draft", ["high"]),
    ("expand", ["medium", "high"]),
    ("introduction", ["high"]),
    ("findings", ["high"]),
])
def test_motion_workflows_preserve_extraction_medium_and_send_writing_high_to_provider(
    monkeypatch, stage, expected_efforts,
):
    class Memory(dict):
        user = SimpleNamespace(contact_info="Test attorney")

    claim = {"title": "Count I", "description": "Complaint allegations.",
             "dismissal_heading": "Pleading defects", "grounds": [{
                 "legal_ground": "Failure to state a claim", "description": "Missing facts.",
                 "citations": [], "selected_citations": [],
             }]}
    memory = Memory({
        "complaint_text": json.dumps(["Fact.", "COUNT I"]),
        "inputs/coa_sums": json.dumps({"0": claim}), "coa_dict": json.dumps({"0": claim}),
        "inputs/fa_sums": json.dumps(["Alleged fact."]), "inputs/case_info": "Case information",
        "drafted_sections/fa_background": "Facts.", "drafted_sections/legal_standard": "Standard.",
        "drafted_sections/batches": json.dumps({"0": {"id": [0, 0], "sub_heading": "Pleading defects"}}),
    })
    response_body = {
        "output": [{"cause of action": "Count I", "description": "Complaint allegations."}],
        "summary": ["Alleged fact."], "background": [{"sub-heading": "Facts", "paragraphs": ["Facts."]}],
        "legal standard": ["Standard."], "citations": [], "legal elements": ["Element."],
        "dismissal_heading": "Pleading defects", "legal grounds": claim["grounds"],
        "section arguments": ["Drafted argument."], "Relevant Facts": ["Alleged fact."], "Findings": [],
        "introduction": ["Introduction."], "conclusion": "Conclusion.",
    }
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            id="dismiss-route", model=kwargs["model"], status="completed",
            output_text=json.dumps(response_body), output=[], usage=SimpleNamespace(total_tokens=5),
        )

    monkeypatch.setattr(ai_base, "get_openai_client", lambda: SimpleNamespace(
        responses=SimpleNamespace(create=create),
    ))
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(ai_base, "record_guided_llm_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(dismiss_drafter, "DBMemory", lambda **_kwargs: memory)
    monkeypatch.setattr(dismiss_drafter.Drafts, "resolve_id", lambda _: 1)
    monkeypatch.setattr(dismiss_drafter.Drafts, "pdf_filename", lambda _: "test.pdf")
    monkeypatch.setattr(dismiss_drafter, "sleep", lambda _: None)
    monkeypatch.setattr(dismiss_drafter, "num_tokens_from_string", lambda _: 1)
    monkeypatch.setattr(dismiss_drafter, "filter_raw_citations", lambda _: [])
    monkeypatch.setattr(dismiss_drafter, "create_dismiss_pdf_no_toc", lambda **_: "test.pdf")
    monkeypatch.setattr(dismiss_drafter, "create_dismiss_pdf", lambda **_: "test.pdf")
    monkeypatch.setattr(dismiss_drafter, "get_text_from_pdf_fitz", lambda _: ("Draft text.", [], False))
    monkeypatch.setattr(dismiss_drafter, "insert_toa_into_brief", lambda *_a: (0, 0))
    monkeypatch.setattr(dismiss_drafter, "apply_frame_and_footers", lambda *_a, **_kw: None)

    data = {"draftId": "draft-1"}
    if stage == "setup":
        monkeypatch.setattr(dismiss_drafter, "get_complaint_text_with_line_metadata_from_file",
                            lambda _: ("Fact.\nCOUNT I", "25-cv-1", False, []))
        monkeypatch.setattr(dismiss_drafter, "get_last_ocr_warning", lambda: None)
        monkeypatch.setattr(dismiss_drafter, "prepare_complaint_text_for_memory",
                            lambda *_a, **_kw: ("Caption", ["Fact.", "COUNT I"], []))
        monkeypatch.setattr(dismiss_drafter, "log_complaint_text_metadata_preview", lambda *_a: None)
        monkeypatch.setattr(dismiss_drafter, "extract_parties_venue", lambda _: (["Alice"], ["Bob"], "Court"))
        monkeypatch.setattr(dismiss_drafter, "chat_completion", lambda _: "FEDERAL")
        monkeypatch.setattr(dismiss_drafter, "get_coa_loc", lambda _: [1, 2])
        result = inspect.unwrap(dismiss_drafter.state0.run)(SimpleNamespace(), "test.pdf", "draft-1")
        assert result["standard"] == "Standard."
    elif stage == "analysis":
        result = inspect.unwrap(dismiss_drafter.state1.run)(data)
        assert result["coa_dict_list"][0]["elements"] == ["Element."]
        assert json.loads(memory["coa_dict"])["0"]["grounds"] == claim["grounds"]
    elif stage == "draft":
        result = inspect.unwrap(dismiss_drafter.state2.run)(data)
        assert result["all_batches_done"] is True
        assert json.loads(memory["drafted_sections/batches"])["0"]["argument"] == ["Drafted argument."]
    elif stage == "expand":
        result = inspect.unwrap(dismiss_drafter.motion_dismiss_expand_argument.run)(
            {**data, "batchIndex": "0", "argument": "Original argument."},
        )
        assert result["expanded_argument"] == ["Drafted argument."]
    elif stage == "introduction":
        result = inspect.unwrap(dismiss_drafter.state3.run)(data)
        assert result["intro"] == "Introduction."
        assert result["conclusion"] == "Conclusion."
    else:
        result = inspect.unwrap(dismiss_drafter.state4.run)(data)
        assert result["halls"] == []

    assert len(calls) == len(expected_efforts)
    assert all(call["model"] == "gpt-6.1-sol" for call in calls)
    assert [call["reasoning"]["effort"] for call in calls] == expected_efforts
