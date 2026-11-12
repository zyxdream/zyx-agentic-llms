"""Canonical storage uses real SQL, including legacy reconciliation and rollback."""
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path

import pytest

from backend.database import db
from backend.models import CaseAssistantWorkflow, ConversationTurn, Drafts, TextEntry, DraftDocument, ChatThread
from backend.util import workspace_store as store
from tests.test_document_preparation import app


def seed(state):
    entry = TextEntry(draftId=1, textName='state', content=json.dumps(state, ensure_ascii=False))
    db.session.add(entry)
    db.session.commit()
    return entry


def saved_workflow(uid, position, *, status='completed', response=None):
    date = datetime(2026, 11, 1) + timedelta(minutes=position)
    row = CaseAssistantWorkflow(id=uid, user_id=1, draft_id=1, status=status,
        phase='finalize' if status == 'completed' else 'agent', execution_version=1,
        data_json=json.dumps({'query': f'Question {position}', 'promptAttachments': [{'file_id': 'original'}]}),
        result_json=json.dumps({'response': response or f'Answer {position}'}) if status == 'completed' else None,
        draft_hash='original', created_at=date, updated_at=date, last_progress_at=date,
        deadline=date + timedelta(days=1))
    db.session.add(row)
    db.session.commit()
    return row


def test_backfill_preserves_identical_legacy_positions_and_all_history(app):
    state = {'draft': {'title': 'Original', 'sections': []}, 'draft_before': {'title': 'Before'},
             'chat_history': [['Same question', 'Same answer'] for _ in range(130)],
             'chat_history_meta': [{} for _ in range(130)], 'constraints': {'tone': 'short'}}
    entry = seed(state)
    original_bytes = entry.content
    report = store.backfill_workspace(1)
    assert report['turns_imported'] == report['state_turns'] == 130
    assert report['merged_by_identity'] == 0
    assert len(store.turns(1)) == len({turn.id for turn in store.turns(1)}) == 130
    projection = store.load(1)
    assert len(projection['chat_history']) == len(projection['chat_history_meta']) == 100
    assert projection['draft'] == state['draft'] and projection['draft_before'] == state['draft_before']
    assert projection['constraints'] == state['constraints'] and projection['draft_version'] == 1
    db.session.commit()
    assert db.session.get(TextEntry, entry.id).content == original_bytes
    snapshot = json.loads(DraftDocument.query.filter_by(chat_thread_id=1).first().legacy_snapshot_json)
    assert snapshot == [{'id': entry.id, 'content': original_bytes}]
    assert store.backfill_workspace(1)['already_initialized']
    assert ConversationTurn.query.count() == 130


def test_completed_workflows_repair_missing_answers_and_preserve_metadata(app):
    first = saved_workflow('wf_first', 1)
    middle = saved_workflow('wf_middle', 2)
    last = saved_workflow('wf_last', 3)
    seed({'draft': {}, 'chat_history': [['Question 1', 'Stale answer'], ['Question 3', 'Answer 3']],
          'chat_history_meta': [{'workflow_run_id': first.id, 'prompt_attachments': [{'file_name': 'Brief'}],
                                'workflow_failed': True, 'workflow_cancelled': True},
                                {'workflow_run_id': last.id}]})
    report = store.initialize(1)
    records = store.turns(1)
    assert report['recovered_workflow_turns'] == 1 and report['turns_imported'] == 3
    assert [record.workflow_run_id for record in records] == [first.id, middle.id, last.id]
    assert records[0].assistant_text == 'Answer 1'
    metadata = json.loads(records[0].metadata_json)
    assert metadata['prompt_attachments'] == [{'file_name': 'Brief'}]
    assert metadata['workflow_completed'] and metadata['agent_run']
    assert 'workflow_failed' not in metadata and 'workflow_cancelled' not in metadata
    assert store.turn_for_workflow(middle.id).assistant_text == 'Answer 2'


def test_explicit_ids_merge_archive_copies_but_equal_text_without_ids_does_not(app):
    db.session.add(TextEntry(draftId=1, textName='conversation-turn:old', content=json.dumps({
        'turn': ['Repeated', 'Repeated'], 'meta': {'conversation_turn_id': 'stable-old', 'source': 'archive'}})))
    db.session.commit()
    seed({'draft': {}, 'chat_history': [['Repeated', 'Repeated'], ['Repeated', 'Repeated']],
          'chat_history_meta': [{'conversation_turn_id': 'stable-old', 'attachment': 'kept'}, {}]})
    report = store.initialize(1)
    assert report['turns_imported'] == 2 and report['merged_by_identity'] == 1
    old = next(turn for turn in store.turns(1) if turn.id == 'stable-old')
    assert json.loads(old.metadata_json)['attachment'] == 'kept'


def test_stale_editor_snapshot_cannot_remove_a_later_answer(app):
    seed({'draft': {'title': 'Original'}, 'chat_history': [], 'chat_history_meta': []})
    stale = store.load(1)
    db.session.commit()
    store.append_turn(1, 'Question', 'Completed answer')
    db.session.commit()
    version = store.update_draft(1, {'title': 'Manual edit'}, stale['draft_version'])
    db.session.commit()
    current = store.load(1)
    assert current['draft'] == {'title': 'Manual edit'} and current['draft_version'] == version == 2
    assert current['chat_history'] == [['Question', 'Completed answer']]
    with pytest.raises(store.WorkspaceConflict) as conflict:
        store.update_draft(1, {'title': 'Wrong stale write'}, stale['draft_version'])
    assert conflict.value.current_version == 2
    db.session.rollback()
    assert store.load(1)['draft']['title'] == 'Manual edit'


def test_workspace_load_reads_draft_and_turns_from_one_publication_snapshot(app, monkeypatch):
    store.initialize(1, {'draft': {'title': 'Before'}, 'chat_history': [], 'chat_history_meta': []})
    db.session.commit()
    read_row = store._draft_row
    published = []

    def publish_after_initial_read(draft_id, lock=False):
        row = read_row(draft_id, lock=lock)
        if not published:
            # A different transaction publishes after initialization/locking but
            # before the assembled read. SQLite intentionally has no FOR UPDATE.
            with db.engine.begin() as connection:
                connection.execute(DraftDocument.__table__.update().where(
                    DraftDocument.chat_thread_id == 1).values(
                    draft_json=json.dumps({'title': 'Published'}), draft_version=2))
                connection.execute(ChatThread.__table__.update().where(
                    ChatThread.id == 1).values(next_turn_position=1))
                connection.execute(ConversationTurn.__table__.insert().values(
                    id='published-turn', chat_thread_id=1, position=0, user_text='Question',
                    assistant_text='Published answer', metadata_json='{}', created_at=datetime.utcnow()))
            published.append(True)
        return row

    monkeypatch.setattr(store, '_draft_row', publish_after_initial_read)
    result = store.load(1)
    assert result['draft'] == {'title': 'Published'} and result['draft_version'] == 2
    assert result['chat_history'] == [['Question', 'Published answer']]


def test_publication_stages_draft_turn_and_workflow_without_independent_commit(app):
    workflow = saved_workflow('wf_atomic', 1, status='running')
    seed({'draft': {'title': 'Original'}, 'chat_history': [], 'chat_history_meta': []})
    store.initialize(1)
    db.session.commit()
    store.update_draft(1, {'title': 'Candidate'}, 1, draft_before={'title': 'Original'})
    store.append_turn(1, 'Question', 'Answer', workflow_id=workflow.id,
                      meta={'workflow_completed': True})
    workflow.status = 'completed'
    db.session.flush()
    db.session.rollback()  # Equivalent to a failure at any point before publication commit.
    assert store.load(1)['draft'] == {'title': 'Original'}
    assert store.turns(1) == [] and db.session.get(CaseAssistantWorkflow, workflow.id).status == 'running'
    assert store.update_draft(1, {'title': 'Candidate'}, 1) == 2
    turn = store.append_turn(1, 'Question', 'Answer', workflow_id=workflow.id)
    workflow.status = 'completed'
    db.session.commit()
    again = store.append_turn(1, 'Different question', 'Different answer', workflow_id=workflow.id)
    assert again.id == turn.id and again.assistant_text == 'Answer'
    assert len(store.turns(1)) == 1 and store.load(1)['draft_version'] == 2


def test_metadata_patches_preserve_canonical_fields_and_merge_named_maps(app):
    seed({'draft': {'title': 'Original'}, 'chat_history': [], 'chat_history_meta': [],
          'constraints': {'tone': 'short'}, 'uploaded_files': {'first': {'name': 'first'}}})
    store.initialize(1)
    db.session.commit()
    store.update_draft(1, {'title': 'New'}, 1)
    store.append_turn(1, 'Question', 'Answer')
    store.patch_auxiliary(1, {'uploaded_files': {'second': {'name': 'second'}}}, merge=('uploaded_files',))
    store.patch_auxiliary(1, {'validated_cases': {'case-a': {'cluster': 1}}}, merge=('validated_cases',))
    store.patch_auxiliary(1, {'validated_cases': {'case-b': {'cluster': 2}}}, merge=('validated_cases',))
    db.session.commit()
    result = store.load(1)
    assert result['draft'] == {'title': 'New'} and result['chat_history'] == [['Question', 'Answer']]
    assert set(result['uploaded_files']) == {'first', 'second'}
    assert set(result['validated_cases']) == {'case-a', 'case-b'}
    assert result['constraints'] == {'tone': 'short'}
    raw = json.loads(TextEntry.query.filter_by(draftId=1, textName='state').first().content)
    assert not set(raw) & store.CANONICAL_KEYS
    with pytest.raises(ValueError):
        store.patch_auxiliary(1, {'draft': {}})
    with pytest.raises(ValueError):
        store.patch_auxiliary(1, {}, remove=('chat_history',))


def test_invalid_legacy_state_stops_import_instead_of_replacing_it(app):
    entry = TextEntry(draftId=1, textName='state', content='{invalid JSON')
    db.session.add(entry)
    db.session.commit()
    with pytest.raises(ValueError):
        store.initialize(1)
    db.session.rollback()
    assert DraftDocument.query.filter_by(chat_thread_id=1).first() is None
    assert db.session.get(TextEntry, entry.id).content == '{invalid JSON'


def test_append_requires_workflow_from_same_workspace(app):
    workflow = saved_workflow('wf_other', 1)
    with pytest.raises(ValueError, match='belong'):
        store.append_turn(2, 'Question', 'Answer', workflow_id=workflow.id)
    db.session.rollback()


def test_chat_and_document_identity_are_distinct_and_owner_checked(app):
    db.session.add(DraftDocument(id=701, public_id='document-one', chat_thread_id=1,
        draft_json='{"title":"One"}', draft_version=3))
    db.session.add(DraftDocument(id=1, public_id='document-two', chat_thread_id=2,
        draft_json='{"title":"Two"}', draft_version=8))
    db.session.commit()
    projection = store.load('draft-1')
    assert projection['chat_thread_id'] == 'draft-1'
    assert projection['draft_document_id'] == 'document-one'
    assert projection['draft']['title'] == 'One'
    assert store.document_for_chat(1, 701).public_id == 'document-one'
    for wrong in (1, 'document-two', 'draft-1'):
        with pytest.raises(ValueError, match='does not belong'):
            store.update_document(1, wrong, {'title': 'Wrong'}, 3)
        db.session.rollback()
    assert store.update_document('draft-1', 'document-one', {'title': 'Changed'}, 3) == 4
    db.session.commit()
    assert store.load(2)['draft']['title'] == 'Two'
    assert store.load(1)['draft']['title'] == 'Changed'
    with pytest.raises(ValueError):
        store.load('document-one')  # Public document IDs are never chat aliases.


def test_empty_slot_identity_and_conversation_counter_belong_to_chat(app):
    first = store.initialize(1)
    db.session.commit()
    second = store.initialize(1)
    assert second['already_initialized']
    assert first['document_public_id'] == second['document_public_id']
    assert DraftDocument.query.filter_by(chat_thread_id=1).count() == 1
    store.append_turn(1, 'Question', 'Answer')
    db.session.commit()
    assert db.session.get(ChatThread, 1).next_turn_position == 1
    assert 'next_turn_position' not in DraftDocument.__table__.columns
    assert store.document_for_chat(1).draft_version == 1
    for key in ('chat_thread_id', 'draft_document_id'):
        with pytest.raises(ValueError, match='canonical'):
            store.patch_auxiliary(1, {key: 'wrong'})


def test_all_chat_owner_columns_use_canonical_database_names(app):
    from backend import models
    for model in (models.TextEntry, models.ConversationTurn, models.FolderUpload,
                  models.UploadedFiles, models.DocumentChunk, models.UploadGroup,
                  models.CaseAssistantWorkflow, models.StepAnalytics, models.TaskAnalytics):
        assert 'chat_thread_id' in model.__table__.columns
        assert 'draft_id' not in model.__table__.columns and 'draftId' not in model.__table__.columns
    assert models.Drafts is models.ChatThread


def test_deleting_workspace_removes_its_canonical_records(app):
    store.initialize(1, {'draft': {}, 'chat_history': [['Question', 'Answer']], 'chat_history_meta': [{}]})
    db.session.commit()
    db.session.delete(db.session.get(Drafts, 1))
    db.session.commit()
    assert DraftDocument.query.filter_by(chat_thread_id=1).first() is None
    assert ConversationTurn.query.filter_by(draft_id=1).count() == 0


def test_first_auxiliary_write_imports_legacy_core_before_cleaning_blob(app):
    seed({'draft': {'title': 'Kept'}, 'chat_history': [['Question', 'Answer']],
          'chat_history_meta': [{}], 'uploaded_files': {}})
    store.patch_auxiliary(1, {'uploaded_files': {'new': {'name': 'new'}}}, merge=('uploaded_files',))
    db.session.commit()
    result = store.load(1)
    assert result['draft']['title'] == 'Kept'
    assert result['chat_history'] == [['Question', 'Answer']]
    assert result['uploaded_files'] == {'new': {'name': 'new'}}


def test_reconciliation_reports_duplicate_state_rows_and_retains_originals(app):
    first = seed({'draft': {'title': 'Active first row'}, 'chat_history': [], 'chat_history_meta': [{}]})
    second = seed({'draft': {'title': 'Other row'}, 'chat_history': [], 'chat_history_meta': []})
    report = store.initialize(1)
    assert len(report['ambiguities']) == 2
    assert store.load(1)['draft']['title'] == 'Active first row'
    snapshot = json.loads(DraftDocument.query.filter_by(chat_thread_id=1).first().legacy_snapshot_json)
    assert [item['id'] for item in snapshot] == [first.id, second.id]


def test_pending_or_failed_workflow_display_is_not_imported_as_final_turn(app):
    workflow = saved_workflow('wf_retryable', 1, status='failed')
    seed({'draft': {}, 'chat_history': [['Question', 'Worker failed.']],
          'chat_history_meta': [{'workflow_run_id': workflow.id, 'workflow_failed': True}]})
    report = store.initialize(1)
    assert report['workflow_overlays_skipped'] == 1 and store.turns(1) == []
    turn = store.append_turn(1, 'Question', 'Successful answer', workflow_id=workflow.id)
    assert turn.assistant_text == 'Successful answer'


def test_published_marker_survives_bookkeeping_crash_during_backfill(app):
    workflow = saved_workflow('wf_committed', 1, status='running')
    seed({'draft': {}, 'chat_history': [['Question', 'Already published']],
          'chat_history_meta': [{'workflow_run_id': workflow.id, 'workflow_completed': True}]})
    report = store.initialize(1)
    assert report['workflow_overlays_skipped'] == 0
    assert store.turn_for_workflow(workflow.id).assistant_text == 'Already published'


def test_additive_migration_creates_matching_tables_and_can_downgrade():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    import sqlalchemy as sa
    path = Path(__file__).resolve().parents[1] / 'migrations/versions/b9c0d1e2f3a4_separate_workspace_draft_and_turns.py'
    spec = importlib.util.spec_from_file_location('workspace_storage_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE drafts (id INTEGER PRIMARY KEY)'))
        connection.execute(sa.text('CREATE TABLE case_assistant_workflows (id VARCHAR(80) PRIMARY KEY)'))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert set(sa.inspect(connection).get_table_names()) >= {'workspace_draft_states', 'conversation_turns'}
            assert {column['name'] for column in sa.inspect(connection).get_columns('workspace_draft_states')} == {'draft_id', 'draft_json', 'draft_before_json', 'draft_version', 'next_turn_position', 'legacy_snapshot_json', 'migration_report_json', 'updated_at'}
            assert {column['name'] for column in sa.inspect(connection).get_columns('conversation_turns')} == {'id', 'draft_id', 'workflow_run_id', 'position', 'user_text', 'assistant_text', 'metadata_json', 'created_at'}
            migration.downgrade()
            assert set(sa.inspect(connection).get_table_names()) == {'drafts', 'case_assistant_workflows'}
    engine.dispose()
