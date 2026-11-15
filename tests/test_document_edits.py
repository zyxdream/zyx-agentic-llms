"""Workspace edit generation, atomic application and run integration; no live models."""
import copy
import json

import pytest

from backend import agent_tasks as tasks
from backend.ai import base
from backend.database import db
from backend.models import AgentArtifact, PublishedDraftVersion
from backend.util import document_drafts as drafts, workspace_service as service, workspace_store
from tests.test_agent_controller import agent, operation, tick, last_result, state
from tests.test_document_preparation import app


BODY = '# Payroll — résumé\n\nRegular rate: $20.\n\n## Findings\n\nCorrected explanation.\n\n| ID | Pay |\n| --- | ---: |\n| 0007 | 200.00 |\n\n```python\nrate = 20\n```\n'


def replacement(old, new, all=False):
    return {'old_text': old, 'new_text': new, 'replace_all': all}


def patch(*edits, title=None, update=False):
    return drafts.MarkdownRevisionResult(mode='patch', title=title, markdown=None,
        replacements=list(edits), update_court_format=update)


def rewrite(text, title=None):
    return drafts.MarkdownRevisionResult(mode='rewrite', title=title, markdown=text,
        replacements=[], update_court_format=False)


@pytest.mark.parametrize('before, edits, expected, count', [
    ('Start. Middle. End.', [replacement('Middle.', 'Changed.')], 'Start. Changed. End.', 1),
    ('Anchor.', [replacement('Anchor.', 'Before. Anchor. After.')], 'Before. Anchor. After.', 1),
    ('Delete me.', [replacement('Delete me.', '')], '', 1),
    ('A B C', [replacement('A', 'Longer'), replacement('C', 'Last')], 'Longer B Last', 2),
    ('A B', [replacement('A', 'B'), replacement('B', 'C')], 'B C', 2),
    ('Same. Same.', [replacement('Same.', 'New.', True)], 'New. New.', 2),
    ('aaaa', [replacement('aa', 'x', True)], 'xx', 2),
    ('First: Same. Second: Same.', [replacement('Second: Same.', 'Second: New.')], 'First: Same. Second: New.', 1),
    ('Unchanged.', [], 'Unchanged.', 0),
    ('Unchanged.', [replacement('Unchanged.', 'Unchanged.')], 'Unchanged.', 0),
    (BODY, [replacement('| 0007 | 200.00 |', '| 0007 | 220.00 |'), replacement('rate = 20', 'rate = 22')],
     BODY.replace('| 0007 | 200.00 |', '| 0007 | 220.00 |').replace('rate = 20', 'rate = 22'), 2),
])
def test_exact_edits_preserve_every_other_character(before, edits, expected, count):
    assert drafts.apply_text_replacements(before, edits) == (expected, count)


@pytest.mark.parametrize('body, edits, message', [
    ('Original.', [replacement('Missing.', 'New.')], 'was not found'),
    ('Same. Same.', [replacement('Same.', 'New.')], 'matched 2'),
    ('abcdef', [replacement('abcd', 'x'), replacement('cdef', 'y')], 'overlap'),
    ('ababa', [replacement('aba', 'x')], 'matched 2'),
    ('A B', [replacement('A', 'X'), replacement('X', 'Y')], 'was not found'),
    ('A B', [replacement('A', 'X'), replacement('B', 'Y'), replacement('missing', 'Z')], 'was not found'),
    ('Exact\nspacing.', [replacement('Exact spacing.', 'New.')], 'was not found'),
    ('Body', [replacement('', 'New')], 'at least 1'),
])
def test_invalid_batch_never_applies_a_partial_change(body, edits, message):
    with pytest.raises(ValueError, match=message):
        drafts.apply_text_replacements(body, edits)


@pytest.mark.parametrize('values', [
    {'mode': 'patch', 'markdown': 'Unexpected full text', 'replacements': []},
    {'mode': 'rewrite', 'markdown': None, 'replacements': []},
    {'mode': 'rewrite', 'markdown': ' ', 'replacements': []},
    {'mode': 'rewrite', 'markdown': 'Text', 'replacements': [replacement('A', 'B')]},
])
def test_revision_result_requires_a_consistent_response_form(values):
    with pytest.raises(ValueError):
        drafts.MarkdownRevisionResult(title=None, update_court_format=False, **values)


def test_global_replacement_handles_many_occurrences_without_changing_other_text():
    body = 'Acme: 0007 — résumé.\n' * 3000
    changed, count = drafts.apply_text_replacements(body, [replacement('Acme', 'New Company', True)])
    assert count == 3000 and changed == body.replace('Acme', 'New Company')


def create(agent, monkeypatch, body=BODY):
    with monkeypatch.context() as scoped:
        scoped.setattr(drafts, 'write_document', lambda **kw:
            drafts.MarkdownDraftResult(title='Payroll', markdown=body, update_court_format=False))
        operation(agent, monkeypatch, 'create_draft', {'instructions': 'Create the requested report.', 'material_ids': []})
    return last_result(agent)['artifact_id']


def provider(monkeypatch, *results, callback=None):
    pending, calls = list(results), []
    def parse(prompt, schema, **options):
        if schema is tasks.EditorWriteCheck:
            return schema(decision='not_forbidden', reason='No explicit restriction.', supporting_wording='')
        assert schema is drafts.MarkdownRevisionResult
        calls.append((json.loads(prompt), options))
        if callback:
            callback()
        result = pending.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(base, 'chat_completion_parse', parse)
    return calls


def revise(agent, monkeypatch, instructions='Apply the requested change.', materials=None):
    return operation(agent, monkeypatch, 'revise_draft', {'instructions': instructions,
        'target': 'working_draft', 'revision_mode': 'general_revision', 'material_ids': materials or []})


def saved(uid):
    return json.loads(db.session.get(AgentArtifact, uid).content_json)


def head(agent):
    return json.loads(agent.payload_json)['agent_context']['working_candidate_id']


def test_payroll_patch_correction_then_citation_preserves_exact_document_and_publication(agent, monkeypatch):
    initial_editor = copy.deepcopy(state(agent)['draft'])
    first = create(agent, monkeypatch)
    raw_first = db.session.get(AgentArtifact, first).content_json
    calls = provider(monkeypatch,
        patch(replacement('Regular rate: $20.', 'Regular rate: $22.')),
        patch(replacement('Corrected explanation.', 'Corrected explanation. [Official source](https://example.test/rule)')))
    revise(agent, monkeypatch, 'Correct the regular rate.')
    corrected = last_result(agent)['artifact_id']
    assert last_result(agent)['editing_method'] == 'patch' and last_result(agent)['replacement_count'] == 1
    db.session.expire_all()
    revise(agent, monkeypatch, 'Add the citation without changing the calculation.')
    final = last_result(agent)['artifact_id']
    expected = BODY.replace('Regular rate: $20.', 'Regular rate: $22.').replace(
        'Corrected explanation.', 'Corrected explanation. [Official source](https://example.test/rule)')
    assert saved(final)['draft']['markdown'] == expected
    assert saved(final)['draft']['parent_revision_id'] == corrected
    assert saved(final)['operations'][0]['old_text'] == 'Corrected explanation.'
    assert calls[1][0]['target_document']['markdown'] == saved(corrected)['draft']['markdown']
    assert len(calls) == 2 and all('Default to patch' in options['sys_prompt'] for _, options in calls)
    assert state(agent)['draft'] == initial_editor and db.session.get(AgentArtifact, first).content_json == raw_first
    assert operation(agent, monkeypatch, 'complete_task', {'response': 'Updated.', 'candidate_id': 'working_draft'}) == {'completed': True}
    assert state(agent)['draft']['markdown'] == expected and PublishedDraftVersion.query.count() == 2


def test_patch_rejection_can_retry_without_a_rewrite_or_extra_candidate(agent, monkeypatch):
    first = create(agent, monkeypatch)
    calls = provider(monkeypatch,
        patch(replacement('Regular rate: $20.', 'Rate changed.'), replacement('Missing passage', 'Wrong')),
        patch(replacement('Regular rate: $20.', 'Regular rate: $22.')))
    revise(agent, monkeypatch)
    assert last_result(agent)['error'] and 'Replacement 2' in last_result(agent)['message']
    assert head(agent) == first and saved(first)['draft']['markdown'] == BODY
    assert AgentArtifact.query.filter_by(kind='draft').count() == 1
    revise(agent, monkeypatch)
    assert head(agent) != first and len(calls) == 2
    assert AgentArtifact.query.filter_by(kind='draft').count() == 2
    assert saved(head(agent))['editing_method'] == 'patch'


@pytest.mark.parametrize('result', [patch(), patch(replacement('Regular rate: $20.', 'Regular rate: $20.')), rewrite(BODY)])
def test_unchanged_result_saves_no_candidate_or_version(agent, monkeypatch, result):
    first = create(agent, monkeypatch)
    provider(monkeypatch, result)
    monkeypatch.setattr(drafts, 'resolve_court_settings', lambda *a, **kw: pytest.fail('No layout change requested'))
    revise(agent, monkeypatch)
    assert last_result(agent)['unchanged'] and 'artifact_id' not in last_result(agent)
    assert head(agent) == first and AgentArtifact.query.filter_by(kind='draft').count() == 1
    assert PublishedDraftVersion.query.count() == 0


def test_patch_preserves_metadata_and_supports_title_only_change(agent, monkeypatch):
    first = create(agent, monkeypatch)
    value = saved(first)
    value['draft'].update(custom_metadata={'author': 'Owner'}, authority_research_ids=['existing-research'])
    db.session.get(AgentArtifact, first).content_json = service.encode(value)
    db.session.commit()
    provider(monkeypatch, patch(title='Updated title'))
    revise(agent, monkeypatch, 'Rename the document only.')
    result = saved(head(agent))['draft']
    assert result['title'] == 'Updated title' and result['markdown'] == BODY
    assert result['custom_metadata'] == {'author': 'Owner'}
    assert result['export_settings'] == value['draft']['export_settings']
    assert result['authority_research_ids'] == ['existing-research']


def test_layout_only_change_uses_existing_resolver_and_preserves_body(agent, monkeypatch):
    first = create(agent, monkeypatch)
    provider(monkeypatch, patch(update=True))
    requests = []
    monkeypatch.setattr(drafts, 'resolve_court_settings', lambda draft, **kw: requests.append(draft) or {'layout': {'font_size_pt': 14}})
    revise(agent, monkeypatch, 'Change the court page layout.')
    result = saved(head(agent))['draft']
    assert len(requests) == 1 and result['markdown'] == BODY
    assert result['export_settings']['court'] == {'layout': {'font_size_pt': 14}}
    assert head(agent) != first


def test_full_translation_remains_available_and_next_edit_is_a_patch(agent, monkeypatch):
    create(agent, monkeypatch)
    calls = provider(monkeypatch, rewrite('# Informe\n\nImporte: 200.'),
        patch(replacement('Importe: 200.', 'Importe: 220.')))
    revise(agent, monkeypatch, 'Translate the entire document into Spanish.')
    translated = head(agent)
    assert last_result(agent)['editing_method'] == 'rewrite'
    assert saved(translated)['operations'] == []
    revise(agent, monkeypatch, 'Correct the amount only.')
    assert saved(head(agent))['draft']['markdown'] == '# Informe\n\nImporte: 220.'
    assert saved(head(agent))['draft']['parent_revision_id'] == translated and len(calls) == 2


def test_uploaded_first_and_followup_revisions_use_one_engine(agent, monkeypatch):
    uid = json.loads(agent.data_json)['document_snapshot']['files'][0]['file_id']
    written = provider(monkeypatch, patch(replacement('middle event', 'updated event')),
        patch(replacement('updated event', 'final event')))
    operation(agent, monkeypatch, 'revise_draft', {'target':uid, 'revision_mode':'preserve_original',
        'instructions':'Correct the uploaded draft.', 'material_ids':[]})
    uploaded = head(agent)
    raw = db.session.get(AgentArtifact, uploaded).content_json
    revise(agent, monkeypatch, 'Correct that phrase again.')
    assert len(written) == 2
    assert written[0][0]['target_document']['source_format'] == 'markdown'
    assert written[0][0]['target_document']['markdown'] == 'A contract about the middle event.'
    assert saved(head(agent))['draft']['markdown'] == 'A contract about the final event.'
    assert saved(head(agent))['draft']['parent_revision_id'] == uploaded
    assert db.session.get(AgentArtifact, uploaded).content_json == raw


def test_rollback_followed_by_patch_keeps_restored_snapshot_immutable(agent, monkeypatch):
    operation(agent, monkeypatch, 'list_draft_versions', {})
    baseline = last_result(agent)['versions'][0]['version_id']
    create(agent, monkeypatch)
    operation(agent, monkeypatch, 'restore_draft', {'version_id': baseline})
    restored = head(agent)
    raw = db.session.get(AgentArtifact, restored).content_json
    provider(monkeypatch, patch(replacement('Original facts.', 'Corrected facts.')))
    revise(agent, monkeypatch, 'Correct the restored facts.')
    assert saved(head(agent))['draft']['markdown'].endswith('Corrected facts.')
    assert saved(head(agent))['draft']['parent_revision_id'] == restored
    assert db.session.get(AgentArtifact, restored).content_json == raw


@pytest.mark.parametrize('interruption', ['cancel', 'manual_save'])
def test_late_patch_cannot_overwrite_cancellation_or_manual_save(agent, monkeypatch, interruption):
    first = create(agent, monkeypatch)
    def interrupt():
        if interruption == 'cancel':
            service.stop(agent, cancelled=True)
        else:
            doc = workspace_store.document_for_chat(agent.chat_thread_id)
            workspace_store.update_document(agent.chat_thread_id, doc.id,
                {'title': 'Manual winner', 'sections': []}, doc.draft_version)
            db.session.commit()
    provider(monkeypatch, patch(replacement('Regular rate: $20.', 'Regular rate: $22.')), callback=interrupt)
    result = revise(agent, monkeypatch)
    if interruption == 'cancel':
        assert result == {'stopped': True}
        assert head(agent) == first and AgentArtifact.query.filter_by(kind='draft').count() == 1
        assert state(agent)['draft']['title'] == 'Original'
    else:
        # Existing editor conflicts are enforced at publication. A private
        # candidate may finish, but cannot replace a newer manual editor save.
        assert result == {'continued': True} and head(agent) != first
        assert state(agent)['draft']['title'] == 'Manual winner'
        assert operation(agent, monkeypatch, 'complete_task', {'response': 'Done.',
            'candidate_id': 'working_draft'}) == {'stopped': True}
        assert state(agent)['draft']['title'] == 'Manual winner'


def test_explicit_retry_after_provider_failure_uses_same_patch_base(agent, monkeypatch):
    first = create(agent, monkeypatch)
    calls = provider(monkeypatch, tasks.ControllerError('Temporary interruption'),
        patch(replacement('Regular rate: $20.', 'Regular rate: $22.')))
    assert revise(agent, monkeypatch) == {'stopped': True}
    assert head(agent) == first
    service.retry(agent)
    assert tick(agent) == {'continued': True}
    assert head(agent) != first and len(calls) == 2
    assert calls[0][0]['target_document'] == calls[1][0]['target_document']
    assert AgentArtifact.query.filter_by(kind='draft').count() == 2


def test_new_documents_keep_full_result_creation_schema(monkeypatch):
    target = None
    calls = []
    def parse(prompt, schema, **kw):
        calls.append(schema)
        return drafts.MarkdownDraftResult(title='New', markdown='# New', update_court_format=False)
    monkeypatch.setattr(base, 'chat_completion_parse', parse)
    result = drafts.write_document(assignment='Write.', instructions='Write.', conversation={}, materials=[],
        target=target, answers=[], model='mock-model', reasoning_effort='medium')
    assert calls == [drafts.MarkdownDraftResult] and result.markdown == '# New'


def test_noop_on_initial_snapshot_does_not_create_a_working_candidate(agent, monkeypatch):
    before = copy.deepcopy(state(agent))
    provider(monkeypatch, patch())
    revise(agent, monkeypatch, 'Check that the requested wording is already present.')
    assert last_result(agent)['unchanged'] and head(agent) is None
    assert AgentArtifact.query.filter_by(kind='draft').count() == 0 and state(agent) == before


def test_patch_can_remove_the_last_sentence_without_creating_replacement_text(agent, monkeypatch):
    create(agent, monkeypatch, 'Only sentence.')
    provider(monkeypatch, patch(replacement('Only sentence.', '')))
    revise(agent, monkeypatch, 'Remove the sentence.')
    assert saved(head(agent))['draft']['markdown'] == ''
    assert operation(agent, monkeypatch, 'complete_task', {'response': 'Removed.', 'candidate_id': 'working_draft'}) == {'completed': True}
    assert state(agent)['draft']['markdown'] == ''
