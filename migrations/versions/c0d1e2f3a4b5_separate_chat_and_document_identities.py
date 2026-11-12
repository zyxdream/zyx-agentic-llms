"""Give chat containers and their owned documents distinct identities.

The old state table remains archival rollback evidence. All runtime ownership
uses chat_threads and draft_documents after this coordinated writer cutover.
"""
from datetime import datetime
import json
from types import SimpleNamespace
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

revision = 'c0d1e2f3a4b5'
down_revision = 'b9c0d1e2f3a4'
branch_labels = None
depends_on = None

OWNER_COLUMNS = (
    ('textEntries', 'draftId', True),
    ('conversation_turns', 'draft_id', False),
    ('folder_uploads', 'draft_id', True),
    ('uploaded_files', 'draft_id', True),
    ('document_chunks', 'draft_id', True),
    ('document_upload_groups', 'draft_id', True),
    ('case_assistant_workflows', 'draft_id', False),
    ('stepAnalytics', 'draftId', True),
    ('taskAnalytics', 'draftId', False),
)


def _rename_column(table, old, new, nullable):
    # Modern SQLite supports native renames, preserving inbound foreign keys
    # without rebuilding parent tables or invoking their delete cascades.
    op.alter_column(table, old, new_column_name=new,
                    existing_type=sa.Integer(), existing_nullable=nullable)


def _owner_indexes(connection, *, reverse=False):
    for table in ('conversation_turns', 'case_assistant_workflows'):
        old = f'ix_{table}_draft_id'
        new = f'ix_{table}_chat_thread_id'
        source, target = (new, old) if reverse else (old, new)
        names = {index['name'] for index in sa.inspect(connection).get_indexes(table)}
        if source in names:
            # Create the replacement before dropping an index that MySQL may
            # currently use to support the chat ownership foreign key.
            if target not in names:
                op.create_index(target, table, ['chat_thread_id'])
            op.drop_index(source, table_name=table)


def _table(connection, name):
    return sa.Table(name, sa.MetaData(), autoload_with=connection)


def _rows(connection, table, predicate=None, order=None):
    query = sa.select(table)
    if predicate is not None:
        query = query.where(predicate)
    if order:
        query = query.order_by(*order)
    return [SimpleNamespace(**dict(row)) for row in connection.execute(query).mappings()]


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _preflight(connection):
    """Reject unresolved source records before nontransactional DDL starts."""
    from backend.util.workspace_store import _legacy_records

    chats = _table(connection, 'drafts')
    states = _table(connection, 'workspace_draft_states')
    turns = _table(connection, 'conversation_turns')
    workflows = _table(connection, 'case_assistant_workflows')
    entries = _table(connection, 'textEntries')
    owners = dict(connection.execute(sa.select(turns.c.id, turns.c.draft_id)).all())
    for chat in _rows(connection, chats, order=[chats.c.id]):
        saved = connection.execute(sa.select(states).where(states.c.draft_id == chat.id)).mappings().first()
        if saved is not None:
            if not isinstance(json.loads(saved['draft_json']), dict):
                raise ValueError(f'Chat {chat.id} has malformed canonical draft content.')
            last = connection.execute(sa.select(sa.func.max(turns.c.position)).where(
                turns.c.draft_id == chat.id)).scalar_one()
            if last is not None and saved['next_turn_position'] <= last:
                raise ValueError(f'Chat {chat.id} has an inconsistent conversation ordering counter.')
            continue
        if connection.execute(sa.select(turns.c.id).where(turns.c.draft_id == chat.id).limit(1)).first():
            raise ValueError(f'Chat {chat.id} has canonical turns without its canonical draft state.')
        source = _rows(connection, entries,
            sa.and_(entries.c.draftId == chat.id, entries.c.textName == 'state'), [entries.c.id])
        legacy = json.loads(source[0].content) if source else {}
        if not isinstance(legacy, dict) or not isinstance(legacy.get('draft', {}), dict):
            raise ValueError(f'Chat {chat.id} has malformed legacy state.')
        workflow_rows = _rows(connection, workflows, workflows.c.draft_id == chat.id,
                             [workflows.c.created_at, workflows.c.id])
        archives = _rows(connection, entries,
            sa.and_(entries.c.draftId == chat.id, entries.c.textName.like('conversation-turn:%')), [entries.c.id])
        _legacy_records(chat.id, source, legacy, workflows={row.id: row for row in workflow_rows},
                        archives=archives, existing_turn_owners=owners)


def _import_documents(connection):
    # This is the same occurrence/identity reconciliation used by the original
    # canonical-storage backfill, supplied explicit rows on this transaction's
    # connection. It makes no application-session or provider calls.
    from backend.util.workspace_store import _legacy_records

    chats = _table(connection, 'chat_threads')
    documents = _table(connection, 'draft_documents')
    states = _table(connection, 'workspace_draft_states')
    turns = _table(connection, 'conversation_turns')
    workflows = _table(connection, 'case_assistant_workflows')
    entries = _table(connection, 'textEntries')
    owners = dict(connection.execute(sa.select(turns.c.id, turns.c.chat_thread_id)).all())
    for chat in _rows(connection, chats, order=[chats.c.id]):
        saved = connection.execute(sa.select(states).where(states.c.draft_id == chat.id)).mappings().first()
        if saved is not None:
            values = {key: saved[key] for key in ('draft_json', 'draft_before_json', 'draft_version',
                'legacy_snapshot_json', 'migration_report_json', 'updated_at')}
            position = saved['next_turn_position']
        else:
            if connection.execute(sa.select(turns.c.id).where(turns.c.chat_thread_id == chat.id).limit(1)).first():
                raise ValueError(f'Chat {chat.id} has canonical turns without its canonical draft state.')
            source = _rows(connection, entries,
                sa.and_(entries.c.chat_thread_id == chat.id, entries.c.textName == 'state'), [entries.c.id])
            legacy = json.loads(source[0].content) if source else {}
            if not isinstance(legacy, dict) or not isinstance(legacy.get('draft', {}), dict):
                raise ValueError(f'Chat {chat.id} has malformed legacy state.')
            workflow_rows = _rows(connection, workflows, workflows.c.chat_thread_id == chat.id,
                                 [workflows.c.created_at, workflows.c.id])
            archives = _rows(connection, entries,
                sa.and_(entries.c.chat_thread_id == chat.id, entries.c.textName.like('conversation-turn:%')), [entries.c.id])
            records, counts, ambiguities = _legacy_records(chat.id, source, legacy,
                workflows={row.id: row for row in workflow_rows}, archives=archives,
                existing_turn_owners=owners)
            report = {'draft_id': chat.id, 'turns_imported': len(records), **counts,
                      'ambiguities': ambiguities, 'already_initialized': False}
            values = {'draft_json': _encode(legacy.get('draft', {'title': '', 'sections': []})),
                'draft_before_json': _encode(legacy['draft_before']) if 'draft_before' in legacy else None,
                'draft_version': 1, 'updated_at': datetime.utcnow(),
                'legacy_snapshot_json': _encode([{'id': row.id, 'content': row.content} for row in source]),
                'migration_report_json': _encode(report)}
            position = len(records)
            for index, record in enumerate(records):
                metadata = record['meta']
                metadata.setdefault('conversation_turn_id', record['id'])
                connection.execute(turns.insert().values(id=record['id'], chat_thread_id=chat.id,
                    workflow_run_id=record['workflow_id'], position=index,
                    user_text=record['turn'][0], assistant_text=record['turn'][1],
                    metadata_json=_encode(metadata), created_at=record['created_at']))
                owners[record['id']] = chat.id
        result = connection.execute(documents.insert().values(public_id=str(uuid.uuid4()),
            chat_thread_id=chat.id, **values))
        document_id = result.inserted_primary_key[0]
        connection.execute(chats.update().where(chats.c.id == chat.id).values(next_turn_position=position))
        connection.execute(workflows.update().where(workflows.c.chat_thread_id == chat.id).values(
            draft_document_id=document_id))


def upgrade():
    connection = op.get_bind()
    _preflight(connection)
    op.rename_table('drafts', 'chat_threads')
    for table, old, nullable in OWNER_COLUMNS:
        _rename_column(table, old, 'chat_thread_id', nullable)
    _owner_indexes(connection)
    _rename_column('case_assistant_workflows', 'active_draft_id', 'active_chat_thread_id', True)
    op.add_column('chat_threads', sa.Column('next_turn_position', sa.Integer(), nullable=False, server_default='0'))
    text = sa.Text().with_variant(LONGTEXT(), 'mysql')
    op.create_table('draft_documents',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('public_id', sa.String(36), nullable=False, unique=True),
        sa.Column('chat_thread_id', sa.Integer(), sa.ForeignKey('chat_threads.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('draft_json', text, nullable=False),
        sa.Column('draft_before_json', text),
        sa.Column('draft_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('legacy_snapshot_json', text), sa.Column('migration_report_json', text),
        sa.Column('updated_at', sa.DateTime(), nullable=False))
    if connection.dialect.name == 'sqlite':
        # A nullable inline REFERENCES column can be added natively. Alembic's
        # two-step ADD COLUMN/ADD CONSTRAINT would rebuild this parent table.
        op.execute(sa.text('ALTER TABLE case_assistant_workflows ADD COLUMN draft_document_id INTEGER '
                           'REFERENCES draft_documents(id) ON DELETE SET NULL'))
    else:
        op.add_column('case_assistant_workflows', sa.Column('draft_document_id', sa.Integer(), nullable=True))
        op.create_foreign_key('fk_workflow_draft_document', 'case_assistant_workflows',
                              'draft_documents', ['draft_document_id'], ['id'], ondelete='SET NULL')
    op.add_column('case_assistant_workflows', sa.Column('base_draft_version', sa.Integer(), nullable=True))
    _import_documents(connection)


def downgrade():
    connection = op.get_bind()
    states = _table(connection, 'workspace_draft_states')
    documents = _table(connection, 'draft_documents')
    chats = _table(connection, 'chat_threads')
    # Preserve subsequent editor work when returning to the preceding schema.
    for document in _rows(connection, documents):
        position = connection.execute(sa.select(chats.c.next_turn_position).where(
            chats.c.id == document.chat_thread_id)).scalar_one()
        values = {key: getattr(document, key) for key in ('draft_json', 'draft_before_json',
            'draft_version', 'legacy_snapshot_json', 'migration_report_json', 'updated_at')}
        values['next_turn_position'] = position
        if connection.execute(sa.select(states.c.draft_id).where(states.c.draft_id == document.chat_thread_id)).first():
            connection.execute(states.update().where(states.c.draft_id == document.chat_thread_id).values(**values))
        else:
            connection.execute(states.insert().values(draft_id=document.chat_thread_id, **values))
    if connection.dialect.name != 'sqlite':
        op.drop_constraint('fk_workflow_draft_document', 'case_assistant_workflows', type_='foreignkey')
    op.drop_column('case_assistant_workflows', 'draft_document_id')
    op.drop_column('case_assistant_workflows', 'base_draft_version')
    op.drop_table('draft_documents')
    op.drop_column('chat_threads', 'next_turn_position')
    _owner_indexes(connection, reverse=True)
    _rename_column('case_assistant_workflows', 'active_chat_thread_id', 'active_draft_id', True)
    for table, old, nullable in reversed(OWNER_COLUMNS):
        _rename_column(table, 'chat_thread_id', old, nullable)
    op.rename_table('chat_threads', 'drafts')
