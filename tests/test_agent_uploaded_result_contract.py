"""Uploaded-first revisions use the shared engine; provider boundaries are mocked."""
import json

import pytest

from backend import agent_tasks
from backend.ai import base
from backend.database import db
from backend.models import AgentArtifact, UploadedFiles
from backend.util import document_drafts, workspace_service as service, source_context
from tests.test_agent_controller import agent, last_result, operation, state
from tests.test_document_preparation import app


def uploaded_revision(agent, monkeypatch, revision, mode='preserve_original'):
    uid = json.loads(agent.data_json)['document_snapshot']['files'][0]['file_id']
    calls = []
    def parse(prompt, schema, **kwargs):
        assert schema is document_drafts.MarkdownRevisionResult
        calls.append(json.loads(prompt))
        return revision
    monkeypatch.setattr(base, 'chat_completion_parse', parse)
    operation(agent, monkeypatch, 'revise_draft', {'instructions': 'Correct the source text as requested.',
        'material_ids': [], 'target': uid, 'revision_mode': mode})
    assert len(calls) == 1
    return last_result(agent)


@pytest.mark.parametrize('mode', ['general_revision', 'preserve_original'])
def test_uploaded_modes_use_same_atomic_revision_and_preserve_source(agent, monkeypatch, mode):
    from backend.documents import revisions
    monkeypatch.setattr(revisions, 'run_preserve_original_uploaded_revision',
        lambda **kw: pytest.fail('Retired agent path was called'))
    before = state(agent)
    snapshot = json.loads(agent.data_json)['document_snapshot']
    uid = snapshot['files'][0]['file_id']
    original = db.session.query(UploadedFiles).filter_by(unique_id=uid).one().content
    prepared = source_context.document(snapshot, uid)
    revision = document_drafts.MarkdownRevisionResult(mode='patch', title=None, markdown=None,
        replacements=[{'old_text':'middle event','new_text':'corrected event','replace_all':False}], update_court_format=False)
    result = uploaded_revision(agent, monkeypatch, revision, mode)
    value = json.loads(db.session.get(AgentArtifact, result['artifact_id']).content_json)
    assert result['editing_method'] == 'patch' and result['base_candidate_id'] == uid
    assert value['draft']['markdown'] == prepared['manifest']['text'].replace('middle event','corrected event')
    assert '[Source:' not in value['draft']['markdown'] and '[Original extraction:' not in value['draft']['markdown']
    assert value['draft']['source_format'] == 'markdown' and value['draft']['parent_revision_id'] is None
    assert value['references'] == [{'file_id':uid,'filename':prepared['filename'],'prepared_artifact_id':prepared['artifact_id']}]
    assert db.session.query(UploadedFiles).filter_by(unique_id=uid).one().content == original
    assert state(agent) == before and AgentArtifact.query.filter_by(kind='draft').count() == 1


def test_uploaded_invalid_batch_saves_nothing_and_returns_recoverable_error(agent, monkeypatch):
    before = state(agent)
    result = uploaded_revision(agent, monkeypatch, document_drafts.MarkdownRevisionResult(
        mode='patch', title=None, markdown=None, update_court_format=False,
        replacements=[{'old_text':'middle event','new_text':'changed','replace_all':False},
                      {'old_text':'missing passage','new_text':'new','replace_all':False}]))
    assert result['error'] and 'Replacement 2' in result['message']
    assert AgentArtifact.query.filter_by(kind='draft').count() == 0 and state(agent) == before
    assert agent.status == 'running'


def test_unchanged_upload_is_imported_once_as_the_result_candidate(agent, monkeypatch):
    result = uploaded_revision(agent, monkeypatch, document_drafts.MarkdownRevisionResult(
        mode='patch', title=None, markdown=None, replacements=[], update_court_format=False))
    assert result.get('artifact_id') and not result.get('unchanged')
    assert AgentArtifact.query.filter_by(kind='draft').count() == 1


def test_cancelled_first_uploaded_revision_cannot_advance_working_draft(agent, monkeypatch):
    uid = json.loads(agent.data_json)['document_snapshot']['files'][0]['file_id']
    def parse(prompt, schema, **kwargs):
        service.stop(agent, cancelled=True)
        return document_drafts.MarkdownRevisionResult(mode='rewrite', title=None,
            markdown='Late content.', replacements=[], update_court_format=False)
    monkeypatch.setattr(base, 'chat_completion_parse', parse)
    assert operation(agent, monkeypatch, 'revise_draft', {'target':uid,'revision_mode':'preserve_original',
        'instructions':'Revise.','material_ids':[]}) == {'stopped':True}
    assert AgentArtifact.query.filter_by(kind='draft').count() == 0
    assert json.loads(agent.payload_json)['agent_context']['working_candidate_id'] is None
