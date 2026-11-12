import copy
import json
import uuid

import pytest

from backend.database import db
from backend.models import Drafts, Project
from backend.word_addin import routes, service, tasks, refinement
from tests.test_word_addin import app, headers, payload  # noqa: F401
from tests.test_word_document_workflow import document_payload, proposal


def complete(client, value):
    response = client.post("/api/word/operations", headers=headers(), json=value)
    assert response.status_code == 202, response.json
    draft = Drafts.query.filter_by(public_id=response.json["operation_id"]).one()
    tasks.word_operation.run(draft.id)
    assert routes.entry(draft.id, "word.status")["state"] == "complete"
    return draft


def followup(value, parent, instruction="Address the service-ticket evidence."):
    return {**value, "request_id": str(uuid.uuid4()), "parent_operation_id": parent.public_id, "followup_instructions": instruction}


def test_refinement_uses_server_result_preserves_baseline_and_saves_lineage(app, monkeypatch):
    calls = []
    def model(prompt, system):
        calls.append((json.loads(prompt), system))
        return {"text": "Narrowed notice argument." if len(calls) > 1 else "Initial notice argument.", "findings": [], "notes": []}
    monkeypatch.setattr(service, "call_model", model)
    c = app.test_client()
    original = payload(file_ids=["own-file"])
    parent = complete(c, original)
    value = followup(original, parent)
    value["refinement_context"] = {"previous_output": "CLIENT INJECTED OUTPUT"}
    child = complete(c, value)
    context = calls[1][0]["refinement_context"]
    assert context["previous_output"]["text"] == "Initial notice argument."
    assert "CLIENT INJECTED" not in json.dumps(calls[1])
    assert calls[1][0]["word_text"] == original["text"]
    assert context["lawyer_followups"] == [value["followup_instructions"]]
    assert "untrusted prior AI work" in calls[1][1]
    result = routes.entry(child.id, "word.result")
    assert result["lineage"] == {"parent_operation_id": parent.public_id, "round": 1, "instruction": value["followup_instructions"]}
    assert parent.public_id in result["review_text"]
    assert routes.entry(child.id, "state")["chat_history"][0][0] == value["followup_instructions"]
    assert routes.entry(parent.id, "word.result")["text"] == "Initial notice argument."
    assert c.post("/api/word/operations", headers=headers(), json=value).json["operation_id"] == child.public_id


@pytest.mark.parametrize("mutation", [
    {"text": "Changed document"}, {"instructions": "Different initial instructions"},
    {"file_ids": ["own-file"]}, {"scope": "document", "action": "ask"},
    {"party": "Other party"}, {"jurisdiction": "Other jurisdiction"},
])
def test_refinement_cannot_switch_its_baseline_or_legal_context(app, monkeypatch, mutation):
    monkeypatch.setattr(service, "call_model", lambda *_, **__: {"text": "Proposed text"})
    c = app.test_client(); value = payload(); parent = complete(c, value)
    response = c.post("/api/word/operations", headers=headers(), json={**followup(value, parent), **mutation})
    assert response.status_code == 400
    assert Drafts.query.count() == 1


def test_parent_is_owned_complete_and_in_the_same_matter(app, monkeypatch):
    monkeypatch.setattr(service, "call_model", lambda *_, **__: {"text": "Proposed text"})
    c = app.test_client(); value = payload(); parent = complete(c, value)
    assert c.post("/api/word/operations", headers=headers(2), json={**followup(value, parent), "project_id": 2}).status_code == 404
    db.session.add(Project(id=3, userId=1, name="Another owned matter")); db.session.commit()
    assert c.post("/api/word/operations", headers=headers(), json={**followup(value, parent), "project_id": 3}).status_code == 404
    routes.set_entry(parent.id, "word.status", {"state": "cancelled"}); db.session.commit()
    assert c.post("/api/word/operations", headers=headers(), json=followup(value, parent)).status_code == 400


@pytest.mark.parametrize("change", [
    {"parent_operation_id": "invalid", "followup_instructions": "Narrow it"},
    {"parent_operation_id": str(uuid.uuid4())},
    {"followup_instructions": "Missing parent"},
    {"parent_operation_id": str(uuid.uuid4()), "followup_instructions": "x" * 4001},
    {"parent_operation_id": str(uuid.uuid4()), "followup_instructions": " ", "action": "citations"},
])
def test_invalid_refinement_fields_are_rejected(change):
    with pytest.raises(ValueError): service.validate_request(payload(**change))


def test_history_and_context_size_are_bounded():
    request = service.validate_request(payload())
    request.update(parent_operation_id=str(uuid.uuid4()), followup_instructions="Narrow it")
    prior = copy.deepcopy(request)
    prior["refinement_context"] = {"lawyer_followups": ["Preserve citations", "Address the counterargument"]}
    context = refinement.bind_context(request, prior, {"kind": "revision", "text": "Proposed text"})
    assert context["lawyer_followups"] == ["Preserve citations", "Address the counterargument", "Narrow it"]
    prior["refinement_context"]["lawyer_followups"] *= 4
    with pytest.raises(ValueError, match="eight refinements"):
        refinement.bind_context(request, prior, {"kind": "revision", "text": "Proposed text"})
    with pytest.raises(ValueError, match="too large"):
        refinement.bind_context(request, request, {"kind": "report", "text": "x" * 180001})


def test_document_refinement_still_validates_edits_against_original_paragraphs(app, monkeypatch):
    seen = []
    def model(prompt, system):
        data = json.loads(prompt); seen.append(data)
        if "document" in data: return {"approach": "Narrow the argument", "global_rules": []}
        result = proposal()
        if data.get("refinement_context"):
            result["edits"][0]["after"] = "Defendant received March notice."
        return result
    monkeypatch.setattr(service, "call_model", model)
    c = app.test_client(); value = document_payload(); parent = complete(c, value)
    child = complete(c, followup(value, parent))
    result = routes.entry(child.id, "word.result")
    assert result["edits"][0]["before"] == value["blocks"][0]["text"]
    assert seen[-1]["refinement_context"]["previous_output"]["edits"][0]["after"] == "Defendant had March notice."
    assert result["lineage"]["round"] == 1


def test_followup_terms_inform_fresh_record_retrieval(app, monkeypatch):
    captured = []
    monkeypatch.setattr(service, "compute_bm25_scores", lambda chunks, keywords: captured.extend(keywords) or chunks)
    request = service.validate_request(payload(file_ids=["own-file"]))
    request["followup_instructions"] = "Inspect service-ticket investigations"
    service.retrieve_sources(1, request)
    assert captured[:4] == ["inspect", "service", "ticket", "investigations"]
