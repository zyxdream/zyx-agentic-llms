"""Exercise the real identity migration against canonical and legacy SQL data."""
from datetime import datetime
import importlib.util
import json
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa

from backend.models import CaseAssistantWorkflow, DraftDocument


def migration(name):
    path = Path(__file__).resolve().parents[1] / 'migrations/versions' / name
    spec = importlib.util.spec_from_file_location(name.split('.')[0], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def old_schema(connection):
    metadata = sa.MetaData()
    sa.Table('drafts', metadata, sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('public_id', sa.String(36), nullable=False), sa.Column('date', sa.DateTime()),
        sa.Column('docCategory', sa.Integer(), nullable=False), sa.Column('display_name', sa.String(250)),
        sa.Column('location', sa.String(250)), sa.Column('projectId', sa.Integer()))
    columns = []
    for column in CaseAssistantWorkflow.__table__.columns:
        if column.name in ('draft_document_id', 'base_draft_version'):
            continue
        name = {'chat_thread_id': 'draft_id', 'active_chat_thread_id': 'active_draft_id'}.get(column.name, column.name)
        refs = [sa.ForeignKey('drafts.id', ondelete='CASCADE')] if name == 'draft_id' else []
        columns.append(sa.Column(name, column.type, *refs, primary_key=column.primary_key,
                                 nullable=True if not column.primary_key else False))
    sa.Table('case_assistant_workflows', metadata, *columns,
             sa.UniqueConstraint('active_draft_id', name='uq_test_active_draft'))
    sa.Table('textEntries', metadata, sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('draftId', sa.Integer(), sa.ForeignKey('drafts.id')),
        sa.Column('date', sa.DateTime()), sa.Column('textName', sa.String(250)), sa.Column('content', sa.Text()))
    for name in ('folder_uploads', 'uploaded_files', 'document_chunks', 'document_upload_groups'):
        sa.Table(name, metadata, sa.Column('id', sa.Integer(), primary_key=True),
                 sa.Column('draft_id', sa.Integer(), sa.ForeignKey('drafts.id')),
                 sa.Column('scope_key', sa.String(64)))
    for name in ('stepAnalytics', 'taskAnalytics'):
        sa.Table(name, metadata, sa.Column('id', sa.Integer(), primary_key=True), sa.Column('draftId', sa.Integer()))
    sa.Table('case_assistant_agent_steps', metadata, sa.Column('id', sa.String(36), primary_key=True),
             sa.Column('workflow_run_id', sa.String(80), sa.ForeignKey('case_assistant_workflows.id', ondelete='CASCADE')))
    sa.Table('case_assistant_agent_artifacts', metadata, sa.Column('id', sa.String(36), primary_key=True),
             sa.Column('step_id', sa.String(36), sa.ForeignKey('case_assistant_agent_steps.id', ondelete='CASCADE')))
    metadata.create_all(connection)
    initial = migration('b9c0d1e2f3a4_separate_workspace_draft_and_turns.py')
    with Operations.context(MigrationContext.configure(connection)):
        initial.upgrade()


def table(connection, name):
    return sa.Table(name, sa.MetaData(), autoload_with=connection)


def seed(connection):
    date = datetime(2026, 11, 1, 12, 30)
    parent = table(connection, 'drafts')
    for uid, kind in ((10, 69), (20, 69), (30, 6), (40, 70)):
        connection.execute(parent.insert().values(id=uid, public_id=f'chat-{uid}',
            date=date, projectId=3, docCategory=kind, display_name=f'Container {uid}'))
    workflow = table(connection, 'case_assistant_workflows')
    snapshot = json.dumps({'query': 'Original request', 'document_snapshot': {'draft_id': 10, 'files': []}})
    for uid, chat_id in (('canonical-workflow', 10), ('missing-answer', 20)):
        connection.execute(workflow.insert().values(id=uid, user_id=1, draft_id=chat_id,
            status='completed', execution_version=1, phase='finalize', data_json=snapshot,
            result_json=json.dumps({'response': 'Authoritative saved answer'}),
            draft_hash='unchanged-hash', created_at=date, updated_at=date))
    states = table(connection, 'workspace_draft_states')
    content = json.dumps({'title': 'Saved draft', 'sections': [], 'source': 'α\r\nβ'}, ensure_ascii=False)
    original_report = json.dumps({'draft_id': 10, 'turns_imported': 1, 'ambiguities': []})
    connection.execute(states.insert().values(draft_id=10, draft_json=content,
        draft_before_json='{"title":"Before"}', draft_version=9, next_turn_position=25,
        legacy_snapshot_json='[{"content":"original bytes"}]', migration_report_json=original_report, updated_at=date))
    turns = table(connection, 'conversation_turns')
    connection.execute(turns.insert().values(id='canonical-turn', draft_id=10,
        workflow_run_id='canonical-workflow', position=24, user_text='Question', assistant_text='Canonical answer',
        metadata_json='{"attachment":"kept"}', created_at=date))
    legacy = json.dumps({'draft': {'title': 'Legacy draft'},
        'chat_history': [['Same question', 'Same answer'], ['Same question', 'Same answer']],
        'chat_history_meta': [{}, {}], 'uploaded_files': {'f': {'name': 'kept'}}})
    connection.execute(table(connection, 'textEntries').insert().values(id=50, draftId=20,
        date=date, textName='state', content=legacy))
    for name in ('folder_uploads', 'uploaded_files', 'document_chunks', 'document_upload_groups'):
        connection.execute(table(connection, name).insert().values(id=1, draft_id=10, scope_key='chat:10'))
    for name in ('stepAnalytics', 'taskAnalytics'):
        connection.execute(table(connection, name).insert().values(id=1, draftId=10))
    connection.execute(table(connection, 'case_assistant_agent_steps').insert().values(id='step-kept', workflow_run_id='canonical-workflow'))
    connection.execute(table(connection, 'case_assistant_agent_artifacts').insert().values(id='revision-kept', step_id='step-kept'))
    return {'content': content, 'report': original_report, 'legacy': legacy, 'snapshot': snapshot, 'date': date}


def test_identity_migration_preserves_data_reconciles_legacy_and_downgrades():
    engine = sa.create_engine('sqlite://')
    current = migration('c0d1e2f3a4b5_separate_chat_and_document_identities.py')
    with engine.begin() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        old_schema(connection)
        saved = seed(connection)
        with Operations.context(MigrationContext.configure(connection)):
            current.upgrade()
        inspector = sa.inspect(connection)
        assert 'drafts' not in inspector.get_table_names()
        assert set(inspector.get_table_names()) >= {'chat_threads', 'draft_documents', 'workspace_draft_states'}
        docs = table(connection, 'draft_documents')
        values = connection.execute(sa.select(docs).order_by(docs.c.chat_thread_id)).mappings().all()
        assert len(values) == len({row['public_id'] for row in values}) == 4
        canonical = values[0]
        assert canonical['chat_thread_id'] == 10 and canonical['id'] != 10
        assert canonical['public_id'] != 'chat-10'
        assert canonical['draft_json'] == saved['content'] and canonical['draft_version'] == 9
        assert canonical['migration_report_json'] == saved['report'] and canonical['updated_at'] == saved['date']
        assert connection.execute(sa.text('select next_turn_position from chat_threads where id=10')).scalar_one() == 25
        assert connection.execute(sa.text('select content from textEntries where id=50')).scalar_one() == saved['legacy']
        assert connection.execute(sa.text('select data_json from case_assistant_workflows where id="canonical-workflow"')).scalar_one() == saved['snapshot']
        assert connection.execute(sa.text('select id from case_assistant_agent_artifacts')).scalar_one() == 'revision-kept'
        assert connection.execute(sa.text('select count(*) from conversation_turns where chat_thread_id=20')).scalar_one() == 3
        assert connection.execute(sa.text('select count(*) from conversation_turns where chat_thread_id=20 and user_text="Same question"')).scalar_one() == 2
        assert connection.execute(sa.text('select assistant_text from conversation_turns where workflow_run_id="missing-answer"')).scalar_one() == 'Authoritative saved answer'
        assert connection.execute(sa.text('select count(*) from case_assistant_workflows where draft_document_id is not null and base_draft_version is null')).scalar_one() == 2
        assert connection.execute(sa.text('PRAGMA foreign_key_check')).all() == []
        assert {c['name'] for c in inspector.get_columns('draft_documents')} == set(DraftDocument.__table__.columns.keys())
        # Downgrade restores the previous storage contract, including later work.
        connection.execute(docs.update().where(docs.c.chat_thread_id == 10).values(draft_json='{"title":"Later"}', draft_version=10))
        with Operations.context(MigrationContext.configure(connection)):
            current.downgrade()
        assert connection.execute(sa.text('select draft_json,draft_version from workspace_draft_states where draft_id=10')).one() == ('{"title":"Later"}', 10)
        assert connection.execute(sa.text('select count(*) from conversation_turns where draft_id=20')).scalar_one() == 3
        assert connection.execute(sa.text('PRAGMA foreign_key_check')).all() == []
    engine.dispose()


@pytest.mark.parametrize('problem', ['malformed-legacy', 'invalid-ordering'])
def test_identity_migration_rejects_unresolved_data_before_ddl(problem):
    engine = sa.create_engine('sqlite://')
    current = migration('c0d1e2f3a4b5_separate_chat_and_document_identities.py')
    with engine.begin() as connection:
        old_schema(connection)
        saved = seed(connection)
        if problem == 'malformed-legacy':
            connection.execute(sa.text("update textEntries set content='{broken' where id=50"))
        else:
            connection.execute(sa.text('update workspace_draft_states set next_turn_position=24 where draft_id=10'))
        with Operations.context(MigrationContext.configure(connection)), pytest.raises(ValueError):
            current.upgrade()
        assert 'drafts' in sa.inspect(connection).get_table_names()
        assert 'chat_threads' not in sa.inspect(connection).get_table_names()
        expected = '{broken' if problem == 'malformed-legacy' else saved['legacy']
        assert connection.execute(sa.text('select content from textEntries where id=50')).scalar_one() == expected
    engine.dispose()
