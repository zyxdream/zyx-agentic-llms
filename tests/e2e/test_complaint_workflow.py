import io
import json
from pathlib import Path

import pytest

from backend import complaint_drafter
from backend.ai.complaint_schemas import (
    ComplaintDraftedCauses,
    ComplaintFindings,
    ComplaintRegeneratedCitations,
)
from backend.models import Drafts
from backend.util.db import DBMemory
from tests.e2e.helpers import ArtifactCollector
from tests.e2e.validators import validate_complaint_pdf, validate_complaint_word


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "complaints.json"


def _load_complaint_fixtures():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


COMPLAINT_VENUE_MATRIX = _load_complaint_fixtures()


class _CompletedProcess:
    def __init__(self, payload):
        self.stdout = io.BytesIO(json.dumps({"output": payload, "usage": []}).encode("utf-8"))
        self.stderr = io.BytesIO()

    def poll(self):
        return 0


def _replayed_state0_responses(case):
    llm = case["llm"]
    typed_causes = [
        {"name": name, **details}
        for cause in llm["causes_of_action_suggestions"]
        for name, details in cause.items()
    ]
    return [
        {"Factual Allegations": llm["factual_allegations"]},
        {"Suggested Prayers for Relief": llm["suggested_prayers"]},
        {"Introduction": llm["introduction"]},
        {"Suggested Causes of Action": typed_causes},
        {
            "Parties": llm["parties"],
            "Jurisdiction": llm["jurisdiction"],
            "Venue": llm["venue"],
        },
    ]


@pytest.mark.parametrize(
    "case",
    COMPLAINT_VENUE_MATRIX,
    ids=lambda case: case["slug"],
)
def test_replayed_complaint_venue_matrix_exports_pdf_and_word(
    monkeypatch, tmp_path, e2e_database, case
):
    """Replay the complaint workflow and verify each venue's document formats."""
    app, draft = e2e_database
    replayed_processes = _replayed_state0_responses(case)
    real_create_pdf = complaint_drafter.create_complaint_pdf
    real_pdf_parser = complaint_drafter.get_text_from_pdf_fitz
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir()
    artifact_collector = ArtifactCollector(tmp_path / "artifacts")

    schema_names = []

    def launch_replayed_llm(_prompt, schema_name):
        schema_names.append(schema_name)
        if schema_name == "complaint_facts":
            # Numbering belongs to the exporters, not the factual narrative call.
            assert (
                "Do not add paragraph numbers or ordered-list markers. "
                "The application adds paragraph numbering when generating the document. "
                "Preserve numbers within the factual content, including dates, amounts, and patent numbers."
            ) in _prompt
            assert '"Factual Allegations": "...."' in _prompt
        return _CompletedProcess(replayed_processes.pop(0))

    def create_pdf_in_tmp(path, **kwargs):
        # The production state tasks use backend/static; keep replay output isolated.
        target = generated_dir / Path(path).name
        return real_create_pdf(str(target), **kwargs)

    def parse_pdf_in_tmp(path):
        return real_pdf_parser(str(generated_dir / Path(path).name))

    causes_response_used = False

    def replay_typed_output(prompt, response_model, *, model=None, reasoning_effort=None):
        nonlocal causes_response_used
        if response_model is ComplaintDraftedCauses:
            assert model == "gpt-6.1-sol"
            assert reasoning_effort == "high"
            causes_response_used = True
            return {"Causes of Action": case["causes_of_action"]}
        assert response_model is ComplaintFindings
        assert model == "gpt-6.1-sol"
        assert reasoning_effort == "high"
        return {"Findings": [{
            "label": "correct",
            "description": "The citation to 35 U.S.C. § 271 is appropriate.",
        }]}

    monkeypatch.setattr(complaint_drafter, "launch_llm_call_process_j", launch_replayed_llm)
    monkeypatch.setattr(complaint_drafter, "create_complaint_pdf", create_pdf_in_tmp)
    monkeypatch.setattr(complaint_drafter, "get_text_from_pdf_fitz", parse_pdf_in_tmp)
    monkeypatch.setattr(complaint_drafter, "_parse_complaint_output", replay_typed_output)
    # Keep this replay offline; the production tokenizer can download its encoding.
    monkeypatch.setattr(
        complaint_drafter,
        "num_tokens_from_string",
        lambda text: len((text or "").split()),
    )
    def write_prayer(_prompt, *, model, reasoning_effort):
        assert model == "gpt-6.1-sol"
        assert reasoning_effort == "high"
        return case["prayer_for_relief"]

    monkeypatch.setattr(complaint_drafter, "chat_completion", write_prayer)

    base_data = {
        "draftId": draft.public_id,
        "venue": case["venue"],
        "factualAllegations": case["factual_allegations_input"],
        "plaintiff-0": case["plaintiff"],
        "defendant-0": case["defendant"],
        "plaintiffAddress-0": "1 Test Street",
        "defendantAddress-0": "2 Test Street",
    }

    with app.app_context():
        state0_result = complaint_drafter.state0.apply(args=[base_data], throw=True)
        assert state0_result.successful()

        state1_data = {"draftId": draft.public_id}
        state1_result = complaint_drafter.state1.apply(args=[state1_data], throw=True)
        assert state1_result.successful()

        state2_data = {
            "draftId": draft.public_id,
            "pfr-0": case["llm"]["suggested_prayers"][0],
            "pfr-1": case["llm"]["suggested_prayers"][1],
        }
        state2_result = complaint_drafter.state2.apply(args=[state2_data], throw=True)
        assert state2_result.successful()

        state3_data = {
            "draftId": draft.public_id,
            "useIntro": "yes",
            "introBody": case["llm"]["introduction"],
        }
        state3_result = complaint_drafter.state3.apply(args=[state3_data], throw=True)
        assert state3_result.successful()
        assert state3_result.result["halls"][0][:2] == (
            "correct", "The citation to 35 U.S.C. § 271 is appropriate."
        )

        memory = DBMemory(db=complaint_drafter.db, doc_id=draft.id)
        final_pdf = generated_dir / Drafts.pdf_filename(draft.public_id)
        final_word = generated_dir / f"{case['slug']}.docx"
        complaint_drafter.draft_full_complaint(memory, str(final_pdf))
        complaint_drafter.draft_full_complaint_word(memory, str(final_word))

        assert memory.get("drafted_sections/intro") == case["llm"]["introduction"]
        assert memory.get("drafted_sections/causes", "").startswith("FIRST CAUSE OF ACTION")
        assert memory.get("drafted_sections/prayer") == case["prayer_for_relief"]

    pdf_artifact = artifact_collector.collect_file(
        final_pdf,
        f"{case['slug']}.pdf",
    )
    word_artifact = artifact_collector.collect_file(
        final_word,
        f"{case['slug']}.docx",
    )
    validate_complaint_pdf(pdf_artifact, case)
    validate_complaint_word(word_artifact, case)
    assert replayed_processes == []
    assert schema_names == [
        "complaint_facts", "complaint_relief", "complaint_introduction",
        "complaint_causes", "complaint_parties_venue",
    ]
    assert causes_response_used is True


def test_regenerated_citations_keep_filtering_and_legacy_storage(
    monkeypatch, e2e_database
):
    app, draft = e2e_database
    seen_models = []

    def typed_citations(prompt, response_model, *, model, reasoning_effort):
        seen_models.append(response_model)
        assert model == "gpt-6.1-sol"
        assert reasoning_effort == "high"
        assert "Breach of Contract" in prompt
        return {"citations": [{
            "case_name": "",
            "citation": "Cal. Civ. Code § 1632",
            "parenthetical": "(written contracts)",
        }]}

    monkeypatch.setattr(complaint_drafter, "_parse_complaint_output", typed_citations)
    monkeypatch.setattr(
        complaint_drafter, "filter_raw_citations",
        lambda citation: [{"start_index": 0, "citation": citation}],
    )
    monkeypatch.setattr(
        complaint_drafter, "filter_on_casename", lambda _raw, matches: matches
    )
    monkeypatch.setattr(
        complaint_drafter,
        "insert_tags",
        lambda citation, _match, _offset: (f"<a href=\"test\">{citation}</a>", 0),
    )

    with app.app_context():
        memory = DBMemory(db=complaint_drafter.db, doc_id=draft.id)
        memory["inputs/venue"] = "California"
        memory["analysis/coa_suggestions"] = json.dumps([{
            "Breach of Contract": {
                "description": "Agreement breached.",
                "citations": [],
                "citations_html": [],
                "filtered_citations": [],
                "filtered_citations_info": [],
            }
        }])
        result = complaint_drafter.regenerate_citations_complaint.apply(args=[{
            "draftId": draft.public_id,
            "coa_index": 0,
            "coa_name": "Breach of Contract",
        }], throw=True)
        assert result.successful()
        assert result.result["message"] == "Successfully regenerated citations"
        assert result.result["filtered_citations"] == [
            "Cal. Civ. Code § 1632 (written contracts)"
        ]
        saved = json.loads(memory["analysis/coa_suggestions"])
        assert saved[0]["Breach of Contract"]["filtered_citations"] == result.result[
            "filtered_citations"
        ]

    assert seen_models == [ComplaintRegeneratedCitations]
