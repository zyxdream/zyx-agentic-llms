import copy
import json

import pytest

from backend.models import Drafts
from backend.word_addin import document_workflow as document
from backend.word_addin import routes, service, tasks
from tests.test_word_addin import app, headers, payload  # noqa: F401


def document_payload(**changes):
    blocks = [
        {"id": "P1", "kind": "body", "text": "Defendant received notice in March.", "editable": True, "label": "Paragraph 1", "section": "Argument"},
        {"id": "P2", "kind": "body", "text": "The record needs review. [TODO]", "editable": True, "label": "Paragraph 2", "section": "Argument"},
        {"id": "F1", "kind": "footnote", "text": "See the March 14 email.", "editable": False, "parent_id": "P1", "label": "Footnote 1 (capture order)"},
    ]
    return payload(action="revise_document", scope="document", text="\n".join(b["text"] for b in blocks), blocks=blocks,
                   document_type="brief", playbook="advocacy", target_words=25, **changes)


def proposal():
    return {"edits": [{"block_id": "P1", "before": "Defendant received notice in March.",
                       "after": "Defendant had March notice.", "reason": "Tighten the statement."}],
            "findings": [{"block_id": "F1", "anchor": "March 14 email", "title": "Check the pinpoint",
                          "detail": "Confirm the exhibit reference.", "severity": "high", "category": "Record",
                          "status": "support", "evidence": [{"source_id": "S1", "quote": "invented quote"}]}]}


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(text="Different document"),
    lambda p: p["blocks"][1].update(id="P1"),
    lambda p: p["blocks"][2].update(parent_id="P99"),
    lambda p: p["blocks"][0].update(editable="yes"),
    lambda p: p["blocks"][0].update(kind="footnote"),
    lambda p: p.update(playbook="invented"),
    lambda p: p.update(target_words=True),
    lambda p: p.update(instructions=""),
    lambda p: p.update(scope="selection"),
])
def test_document_context_is_bound_and_bounded(mutation):
    value = document_payload()
    mutation(value)
    with pytest.raises(ValueError):
        service.validate_request(value)


@pytest.mark.parametrize("mutation", [
    lambda r: r["edits"][0].update(before="Other paragraph"),
    lambda r: r["edits"][0].update(block_id="P99"),
    lambda r: r["edits"][0].update(block_id="F1", before="See the March 14 email."),
    lambda r: r["edits"].append(copy.deepcopy(r["edits"][0])),
    lambda r: r["edits"][0].update(after="One\nTwo"),
    lambda r: r["findings"][0].update(anchor="Not in note"),
    lambda r: r["findings"][0].update(block_id="P99"),
])
def test_model_cannot_redirect_or_restructure_an_edit(mutation):
    raw = proposal()
    mutation(raw)
    with pytest.raises(ValueError):
        document.normalize_batch(raw, document_payload()["blocks"], [], True)


def test_read_only_review_cannot_return_edits():
    with pytest.raises(ValueError, match="review returned"):
        document.normalize_batch(proposal(), document_payload()["blocks"], [], False)


def test_document_job_returns_plan_note_findings_and_readable_saved_review(app, monkeypatch):
    seen = []
    def model(prompt, system):
        data = json.loads(prompt)
        seen.append(data)
        return {"approach": "Tighten the argument and inspect its support.", "global_rules": ["Preserve the notice date."]} if "document" in data else proposal()
    monkeypatch.setattr(service, "call_model", model)
    client = app.test_client()
    p = document_payload()
    p["file_ids"] = ["own-file"]
    op = client.post("/api/word/operations", headers=headers(), json=p)
    assert op.status_code == 202
    draft = Drafts.query.filter_by(public_id=op.json["operation_id"]).one()
    tasks.word_operation.run(draft.id)
    result = client.get(f"/api/word/operations/{draft.public_id}", headers=headers()).json["result"]
    assert result["kind"] == "plan" and len(result["edits"]) == 1
    assert result["coverage"]["footnotes"] == 1
    assert result["word_counts"]["body_proposed"] < result["word_counts"]["body_before"]
    assert seen[0]["document"][-1]["kind"] == "footnote"
    assert seen[1]["global_plan"]["rules"] == ["Preserve the notice date."]
    note = next(f for f in result["findings"] if f["block_id"] == "F1")
    assert note["status"] == "review" and note["evidence"] == []
    assert any(f["title"] == "Unresolved placeholder" for f in result["findings"])
    saved = routes.entry(draft.id, "state")["chat_history"][0][1]
    assert "Original: Defendant received notice" in saved and "Footnote 1" in saved


def test_cancellation_stops_before_the_next_document_model_call(app, monkeypatch):
    client = app.test_client()
    op = client.post("/api/word/operations", headers=headers(), json=document_payload()).json["operation_id"]
    draft = Drafts.query.filter_by(public_id=op).one()
    calls = []
    def model(*args):
        calls.append(1)
        client.post(f"/api/word/operations/{op}/cancel", headers=headers())
        return {"approach": "Cancelled plan", "global_rules": []}
    monkeypatch.setattr(service, "call_model", model)
    tasks.word_operation.run(draft.id)
    assert calls == [1]
    assert routes.entry(draft.id, "word.status")["state"] == "cancelled"
    assert routes.entry(draft.id, "word.result") is None


def test_every_block_is_processed_and_citation_notes_are_locatable(monkeypatch):
    p = document_payload()
    p["action"] = "citations"
    p["blocks"][0]["text"] = "A" * 8000
    p["blocks"][1]["text"] = "B" * 8000
    p["blocks"][2]["text"] = "Smith v. Jones, 100 F.3d 200."
    p["text"] = "\n".join(b["text"] for b in p["blocks"])
    request = service.validate_request(p)
    calls = []
    def guard(text):
        calls.append(text)
        return {"findings": [{"title": "Smith", "anchor": "Smith v. Jones", "detail": "Review", "status": "review", "evidence": []}] if "Smith" in text else [], "notes": ["No citator check."]}
    monkeypatch.setattr(service, "guard_result", guard)
    result = document.execute_document(request, 1)
    assert len(calls) == 2
    assert "\n".join(calls) == p["text"]
    assert result["findings"][0]["block_id"] == "F1"
    assert result["coverage"]["review_sections"] == 2


def test_long_document_limit_and_paragraph_limit_are_explicit():
    p = document_payload()
    p["blocks"] = [{"id": f"P{i+1}", "kind": "body", "text": "x" * 12000, "editable": True} for i in range(16)]
    p["text"] = "\n".join(b["text"] for b in p["blocks"])
    with pytest.raises(ValueError, match="180,000"):
        service.validate_request(p)
    p["blocks"] = [{"id": "P1", "kind": "body", "text": "x" * 12001, "editable": True}]
    p["text"] = p["blocks"][0]["text"]
    with pytest.raises(ValueError, match="too long"):
        service.validate_request(p)


def test_record_playbook_requires_explicit_sources():
    value = document_payload()
    value["playbook"] = "record_integrity"
    with pytest.raises(ValueError, match="Select matter sources"):
        service.validate_request(value)
    value["file_ids"] = ["own-file"]
    assert service.validate_request(value)["playbook"] == "record_integrity"


def test_note_reference_characters_cannot_be_removed_by_the_model():
    blocks = service.validate_request(document_payload())["blocks"]
    blocks[0]["text"] += "\x02"
    raw = proposal()
    raw["edits"][0]["before"] = blocks[0]["text"]
    with pytest.raises(ValueError, match="paragraph structure"):
        document.normalize_batch(raw, blocks, [], True)
    raw["edits"][0]["after"] += "\x02"
    assert document.normalize_batch(raw, blocks, [], True)[0][0]["after"].endswith("\x02")


def test_later_batch_failure_publishes_no_partial_plan(app, monkeypatch):
    value = document_payload()
    value["blocks"][0]["text"] = "A" * 8000
    value["blocks"][1]["text"] = "B" * 8000
    value["text"] = "\n".join(b["text"] for b in value["blocks"])
    calls = []
    def model(prompt, system):
        data = json.loads(prompt)
        calls.append(data)
        if "document" in data:
            return {"approach": "Review all sections", "global_rules": []}
        if len(calls) == 2:
            return {"edits": [{"block_id": "P1", "before": value["blocks"][0]["text"], "after": "Shorter argument.", "reason": "Tighten"}], "findings": []}
        return "truncated JSON {"
    monkeypatch.setattr(service, "call_model", model)
    client = app.test_client()
    op = client.post("/api/word/operations", headers=headers(), json=value).json["operation_id"]
    draft = Drafts.query.filter_by(public_id=op).one()
    tasks.word_operation.run(draft.id)
    assert len(calls) == 3
    assert routes.entry(draft.id, "word.status")["state"] == "failed"
    assert routes.entry(draft.id, "word.result") is None


def test_source_ids_remain_unique_across_review_batches_and_quotes_are_bound(monkeypatch):
    value = document_payload()
    value["blocks"][0]["text"] = "A" * 8000
    value["blocks"][1]["text"] = "B" * 8000
    value["text"] = "\n".join(b["text"] for b in value["blocks"])
    value["file_ids"] = ["own-file"]
    request = service.validate_request(value)
    monkeypatch.setattr(service, "retrieve_sources", lambda *_: ([{"id": "S1", "filename": "Email.txt", "chunk": 1, "excerpt": "Notice received March 14."}], []))
    def model(prompt, system):
        data = json.loads(prompt)
        if "document" in data:
            return {"approach": "Review all sections", "global_rules": []}
        block = data["target_blocks"][0]
        return {"edits": [], "findings": [{"block_id": block["id"], "anchor": block["text"][:10], "title": "Notice", "detail": "Check the record", "status": "support", "evidence": [{"source_id": data["record_excerpts"][0]["id"], "quote": "Notice received March 14."}]}]}
    monkeypatch.setattr(service, "call_model", model)
    result = document.execute_document(request, 1)
    assert [s["id"] for s in result["sources"]] == ["S1", "S2"]
    assert [f["evidence"][0]["source_id"] for f in result["findings"]] == ["S1", "S2"]
    assert all(f["status"] == "support" for f in result["findings"])



def test_document_research_uses_one_query_and_verifies_all_proposed_edits(monkeypatch):
    from backend.word_addin import research
    p = document_payload()
    p["instructions"] = "Add supporting cases"
    request = service.validate_request(p)
    source = {"id": "C1", "filename": "Example", "citations": ["123 F.3d 456"], "href": "/CaseViewer/123%20F.3d%20456&12", "kind": "authority", "excerpt": "A notice must state the defect.", "chunk": 1}
    searches, verified = [], []
    monkeypatch.setattr(research, "retrieve_authorities", lambda query: searches.append(query) or [source])
    monkeypatch.setattr(service, "retrieve_sources", lambda *args: ([], []))
    def model(prompt, system):
        data = json.loads(prompt)
        if '"query": string' in system:
            return {"query": "notice pleading defect"}
        assert data["authority_excerpts"] == [source]
        if "document" in data:
            return {"approach": "Add a supporting authority.", "global_rules": []}
        result = proposal()
        result["edits"][0]["after"] = "Defendant had March notice. See 123 F.3d 456."
        return result
    monkeypatch.setattr(service, "call_model", model)
    monkeypatch.setattr(service, "guard_result", lambda text: verified.append(text) or {"findings": [{"title": "123 F.3d 456", "status": "verified"}]})
    result = document.execute_document(request, 1)
    assert searches == ["notice pleading defect"]
    assert "Defendant had March notice. See 123 F.3d 456." in verified[0]
    assert result["authority_links"][0]["href"] == source["href"]
    assert result["sources"] == [source]


def test_document_research_failure_does_not_publish_partial_edits(monkeypatch):
    from backend.word_addin import research
    p = document_payload()
    p["instructions"] = "Add supporting cases"
    monkeypatch.setattr(service, "call_model", lambda *args: {"query": "notice pleading"})
    def fail(query):
        raise ValueError("CourtListener research is unavailable")
    monkeypatch.setattr(research, "retrieve_authorities", fail)
    with pytest.raises(ValueError, match="unavailable"):
        document.execute_document(service.validate_request(p), 1)


def test_stress_test_keeps_valid_extra_findings_and_prioritizes_them(monkeypatch):
    value = document_payload()
    value.update(action='review_document', instructions='Stress-test all the arguments as opposing counsel.')
    value['blocks'] = [{'id': f'P{i+1}', 'kind': 'body', 'text': f'Argument {i+1}: Defendant received notice, but the timing and supporting record require review.', 'editable': True, 'label': f'Paragraph {i+1}'} for i in range(90)]
    value['text'] = '\n'.join(block['text'] for block in value['blocks'])
    request = service.validate_request(value)
    monkeypatch.setattr(service, 'retrieve_sources', lambda *_: ([], []))
    def model(prompt, system):
        data = json.loads(prompt)
        if 'document' in data:
            return {'approach': 'Review the strongest counterarguments.', 'global_rules': []}
        assert len(data['target_blocks']) == 90
        return {'edits': [], 'findings': [
            {'block_id': f'P{i+1}', 'anchor': 'notice', 'title': f'Issue {i}', 'detail': f'Concrete improvement {i}',
             'severity': 'high' if i == 35 else 'medium', 'status': 'review', 'evidence': []}
            for i in range(40)]}
    monkeypatch.setattr(service, 'call_model', model)
    result = document.execute_document(request, 1)
    assert result['kind'] == 'report'
    assert len([f for f in result['findings'] if f['title'].startswith('Issue')]) == 40
    assert result['findings'][0]['title'] == 'Issue 35'
    assert result['edits'] == []
    assert result['coverage']['body_paragraphs'] == 90


@pytest.mark.parametrize('bad_findings', ['overflow', 'invalid_anchor'])
def test_review_repairs_a_section_once_without_relaxing_location_checks(monkeypatch, bad_findings):
    request = service.validate_request(document_payload())
    request['action'] = 'review_document'
    calls = []
    monkeypatch.setattr(service, 'retrieve_sources', lambda *_: ([], []))
    def model(prompt, system):
        data = json.loads(prompt)
        calls.append(data)
        if 'document' in data:
            return {'approach': 'Stress-test the brief.', 'global_rules': []}
        finding = {'block_id': 'P1', 'anchor': 'notice', 'title': 'Notice argument', 'detail': 'Check actual receipt.'}
        if len(calls) == 2:
            if bad_findings == 'overflow':
                return {'edits': [], 'findings': [finding] * 121}
            finding['anchor'] = 'invented anchor'
        return {'edits': [], 'findings': [finding]}
    monkeypatch.setattr(service, 'call_model', model)
    result = document.execute_document(request, 1)
    assert len(calls) == 3 and 'validation_feedback' in calls[2]
    assert next(f for f in result['findings'] if f['title'] == 'Notice argument')['anchor'] == 'notice'
    assert not result['edits']


def test_failed_repair_never_publishes_invalid_findings(monkeypatch):
    request = service.validate_request(document_payload())
    request['action'] = 'review_document'
    calls = []
    monkeypatch.setattr(service, 'retrieve_sources', lambda *_: ([], []))
    def model(prompt, system):
        data = json.loads(prompt)
        calls.append(data)
        if 'document' in data:
            return {'approach': 'Review', 'global_rules': []}
        return {'edits': [], 'findings': [{'block_id': 'P99', 'anchor': 'fabricated'}]}
    monkeypatch.setattr(service, 'call_model', model)
    with pytest.raises(ValueError, match='valid paragraph'):
        document.execute_document(request, 1)
    assert len(calls) == 3


def test_prioritization_preserves_distinct_evidence_for_the_same_issue(monkeypatch):
    request = service.validate_request(document_payload())
    request['action'] = 'review_document'
    request['file_ids'] = ['own-file']
    source = {'id': 'S1', 'filename': 'Record.txt', 'chunk': 1, 'excerpt': 'Notice arrived Monday. Notice was disputed Tuesday.'}
    monkeypatch.setattr(service, 'retrieve_sources', lambda *_: ([source], []))
    def model(prompt, system):
        data = json.loads(prompt)
        if 'document' in data:
            return {'approach': 'Compare the notice evidence.', 'global_rules': []}
        points = [{'block_id': 'P1', 'anchor': 'notice', 'title': 'Notice evidence', 'detail': 'Compare both statements.',
                   'status': 'support', 'evidence': [{'source_id': 'S1', 'quote': quote}]}
                  for quote in ['Notice arrived Monday.', 'Notice was disputed Tuesday.']]
        return {'edits': [], 'findings': points + [copy.deepcopy(points[0])]}
    monkeypatch.setattr(service, 'call_model', model)
    result = document.execute_document(request, 1)
    findings = [f for f in result['findings'] if f['title'] == 'Notice evidence']
    assert len(findings) == 2
    assert [f['evidence'][0]['quote'] for f in findings] == ['Notice arrived Monday.', 'Notice was disputed Tuesday.']
