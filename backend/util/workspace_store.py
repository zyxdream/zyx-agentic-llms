"""Canonical draft/turn storage and a compatible assembled workspace view.

Each writer names the domain it changes. Helpers stage SQL changes but never
commit, so publication can save a draft, turn and workflow outcome together.
Legacy state is imported once; subsequent reads never treat it as draft/history
authority. Its original bytes are retained for cutover reconciliation/rollback.
"""
import copy
from datetime import datetime
import json
import uuid

from sqlalchemy.orm import aliased

from backend.database import db
from backend.models import (WorkspaceRun, ConversationTurn, ChatThread,
                            TextEntry, DraftDocument)

UNSET = object()
CANONICAL_KEYS = frozenset({'draft', 'draft_before', 'draft_version', 'chat_history',
                            'chat_history_meta', '_history_to_archive',
                            'chat_thread_id', 'draft_document_id'})
DISPLAY_TURNS = 100


class WorkspaceConflict(ValueError):
    """The editor mutation was based on a superseded current-draft version."""
    def __init__(self, current_version):
        self.current_version = current_version
        super().__init__('The draft changed elsewhere. Your unsaved edits have been kept.')


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _decode(value, default=None):
    return json.loads(value) if value else copy.deepcopy(default)


def _blank_draft():
    return {'title': '', 'sections': []}


def _state_entries(draft_id):
    return TextEntry.query.filter_by(chat_thread_id=draft_id, textName='state').order_by(TextEntry.id).all()


def _stable_id(draft_id, identity, existing_turn_owners=None):
    identity = str(identity)
    owner = (existing_turn_owners.get(identity) if existing_turn_owners is not None else
             getattr(db.session.get(ConversationTurn, identity), 'chat_thread_id', None)) if len(identity) <= 80 else None
    if len(identity) <= 80 and (owner is None or owner == draft_id):
        return identity
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'zyx-agentic-llms:workspace:{draft_id}:turn:{identity}'))


def _legacy_records(draft_id, entries, state, *, workflows=None, archives=None, existing_turn_owners=None):
    """Keep source ordering and identities, then repair missing workflow answers."""
    # Explicit rows let the identity migration reuse this reconciliation on its
    # own SQL connection, without application-session commits or provider calls.
    if workflows is None:
        workflows = {row.id: row for row in WorkspaceRun.query.filter_by(
            chat_thread_id=draft_id).order_by(WorkspaceRun.created_at, WorkspaceRun.id).all()}
    records, by_id, ambiguities = [], {}, []
    counts = {'state_turns': 0, 'archived_turns': 0, 'completed_workflows': 0,
              'merged_by_identity': 0, 'recovered_workflow_turns': 0,
              'workflow_overlays_skipped': 0}

    def add(turn, meta, identity, origin, created_at=None):
        if not isinstance(turn, (list, tuple)) or len(turn) != 2 or not all(isinstance(v, str) for v in turn):
            raise ValueError(f'Workspace {draft_id} has an invalid conversation pair in {origin}.')
        meta = copy.deepcopy(meta) if isinstance(meta, dict) else {}
        workflow_id = meta.get('run_id')
        if workflow_id and workflow_id not in workflows:
            ambiguities.append({'source': origin, 'reason': 'Workflow association is not in this workspace.',
                                'run_id': workflow_id})
        valid_workflow = workflow_id if workflow_id in workflows else None
        if valid_workflow and workflows[valid_workflow].status != 'completed' and not meta.get('workflow_completed'):
            # Failed/cancelled/pending workflow text remains a workflow overlay;
            # importing it as a final turn would block a later successful Retry.
            counts['workflow_overlays_skipped'] += 1
            return None
        explicit = valid_workflow or meta.get('conversation_turn_id')
        uid = _stable_id(draft_id, explicit or str(uuid.uuid5(uuid.NAMESPACE_URL,
                         f'zyx-agentic-llms:workspace:{draft_id}:{identity}')), existing_turn_owners)
        if uid in by_id:
            existing = by_id[uid]
            existing['meta'].update(meta)
            existing['turn'] = list(turn)
            counts['merged_by_identity'] += 1
            return existing
        record = {'id': uid, 'turn': list(turn), 'meta': meta, 'workflow_id': valid_workflow,
                  'created_at': created_at or (workflows[valid_workflow].created_at if valid_workflow else datetime.utcnow())}
        records.append(record)
        by_id[uid] = record
        return record

    if archives is None:
        archives = TextEntry.query.filter_by(chat_thread_id=draft_id).filter(
            TextEntry.textName.like('conversation-turn:%')).order_by(TextEntry.id).all()
    for archive in archives:
        value = _decode(archive.content, {})
        add(value.get('turn'), value.get('meta'), f'archive:{archive.id}',
            f'archive:{archive.id}', archive.date)
        counts['archived_turns'] += 1
    history, metadata = state.get('chat_history', []), state.get('chat_history_meta', [])
    if not isinstance(history, list) or not isinstance(metadata, list):
        raise ValueError(f'Workspace {draft_id} has invalid legacy conversation arrays.')
    source_id = entries[0].id if entries else 'supplied'
    for index, turn in enumerate(history):
        add(turn, metadata[index] if index < len(metadata) else {},
            f'state:{source_id}:{index}', f'state:{source_id}:{index}')
        counts['state_turns'] += 1
    if len(metadata) != len(history):
        ambiguities.append({'source': f'state:{source_id}', 'reason': 'History and metadata array lengths differ.'})
    if len(entries) > 1:
        ambiguities.append({'source': 'state', 'reason': 'Multiple legacy state rows; preserved the first active row.',
                            'row_ids': [row.id for row in entries]})

    for workflow in workflows.values():
        result, data = _decode(workflow.result_json, {}), _decode(workflow.data_json, {})
        if workflow.status != 'completed' or not isinstance(result.get('response'), str) or not result['response']:
            continue
        counts['completed_workflows'] += 1
        uid = _stable_id(draft_id, workflow.id, existing_turn_owners)
        record = by_id.get(uid)
        if record is None:
            record = add([data.get('display_user_message') or data.get('query', ''), result['response']],
                         {'run_id': workflow.id}, workflow.id, f'workflow:{workflow.id}', workflow.created_at)
            records.remove(record)
            # Unknown legacy dates remain in their recorded source order. A
            # missing modern turn is inserted before its next known workflow.
            position = next((i for i, item in enumerate(records)
                if item['workflow_id'] and workflows[item['workflow_id']].created_at > workflow.created_at), len(records))
            records.insert(position, record)
            counts['recovered_workflow_turns'] += 1
        record['turn'] = [data.get('display_user_message') or data.get('query', ''), result['response']]
        metadata = data.get('prompt_turn_meta')
        record['meta'] = {**(metadata if isinstance(metadata, dict) else {}), **record['meta'],
                          'run_id': workflow.id, 'workflow_completed': True,
                          'agent_run': True}
        record['meta'].pop('workflow_failed', None)
        record['meta'].pop('workflow_cancelled', None)
        if data.get('promptAttachments') and 'prompt_attachments' not in record['meta']:
            record['meta']['prompt_attachments'] = data['promptAttachments']
        if data.get('document_snapshot') and 'document_scope' not in record['meta']:
            record['meta']['document_scope'] = data['document_snapshot']
        if data.get('confirmed_clarification') and 'clarification' not in record['meta']:
            record['meta']['clarification'] = data['confirmed_clarification']
        record['workflow_id'], record['created_at'] = workflow.id, workflow.created_at
    return records, counts, ambiguities


def initialize(draft_id, state=None):
    """Import one workspace once. Caller owns the transaction and final commit."""
    draft_id = ChatThread.resolve_id(draft_id)
    existing = DraftDocument.query.filter_by(chat_thread_id=draft_id).first()
    if existing is not None:
        return _identity_report(existing, already_initialized=True)
    # Only first-time initialization locks the parent; normal reads/writes use
    # the canonical state row. Deployment backfills before restarting writers.
    db.session.query(ChatThread).filter_by(id=draft_id).with_for_update().one()
    existing = DraftDocument.query.filter_by(chat_thread_id=draft_id).populate_existing().first()
    if existing is not None:
        return _identity_report(existing, already_initialized=True)
    entries = _state_entries(draft_id)
    legacy = copy.deepcopy(state) if state is not None else _decode(entries[0].content, {}) if entries else {}
    if not isinstance(legacy, dict):
        raise ValueError(f'Workspace {draft_id} has invalid legacy state.')
    records, counts, ambiguities = _legacy_records(draft_id, entries, legacy)
    draft = legacy.get('draft', _blank_draft())
    if not isinstance(draft, dict):
        raise ValueError(f'Workspace {draft_id} has invalid draft content.')
    report = {'draft_id': draft_id, 'turns_imported': len(records), **counts,
              'ambiguities': ambiguities, 'already_initialized': False}
    row = DraftDocument(chat_thread_id=draft_id, draft_json=_encode(draft),
        draft_before_json=_encode(legacy['draft_before']) if 'draft_before' in legacy else None,
        draft_version=1,
        legacy_snapshot_json=_encode([{'id': entry.id, 'content': entry.content} for entry in entries]),
        migration_report_json=_encode(report), updated_at=datetime.utcnow())
    db.session.add(row)
    chat = db.session.get(ChatThread, draft_id)
    chat.next_turn_position = len(records)
    for position, record in enumerate(records):
        meta = record['meta']
        meta.setdefault('conversation_turn_id', record['id'])
        db.session.add(ConversationTurn(id=record['id'], chat_thread_id=draft_id,
            run_id=record['workflow_id'], position=position,
            user_text=record['turn'][0], assistant_text=record['turn'][1],
            metadata_json=_encode(meta), created_at=record['created_at']))
    db.session.flush()
    return _identity_report(row, already_initialized=False)


def _identity_report(document, *, already_initialized):
    """Operational mapping adds identities without rewriting archived evidence."""
    chat = db.session.get(ChatThread, document.chat_thread_id)
    return {**_decode(document.migration_report_json, {}),
            'chat_thread_id': chat.id, 'chat_public_id': chat.public_id,
            'draft_document_id': document.id, 'document_public_id': document.public_id,
            'already_initialized': already_initialized}


def backfill_workspace(draft_id):
    return initialize(draft_id)


def _draft_row(draft_id, lock=False):
    draft_id = ChatThread.resolve_id(draft_id)
    initialize(draft_id)
    query = DraftDocument.query.filter_by(chat_thread_id=draft_id).populate_existing()
    return (query.with_for_update() if lock else query).one()


def document_for_chat(chat_id, document_id=None, lock=False):
    """Resolve an explicitly named document only within its owning chat.

    A legacy chat-only call selects that chat's single document. Public document
    UUIDs are never interpreted as chat UUIDs or used to select another owner.
    """
    chat_id = ChatThread.resolve_id(chat_id)
    initialize(chat_id)
    query = DraftDocument.query.filter_by(chat_thread_id=chat_id).populate_existing()
    if document_id is not None:
        if isinstance(document_id, bool) or not isinstance(document_id, (int, str)):
            raise ValueError('Invalid draft document identifier.')
        if isinstance(document_id, int):
            query = query.filter_by(id=document_id)
        else:
            query = query.filter_by(public_id=document_id)
    row = (query.with_for_update() if lock else query).first()
    if row is None:
        raise ValueError('The draft document does not belong to this chat.')
    return row


def turns(draft_id):
    row = _draft_row(draft_id)
    return ConversationTurn.query.filter_by(chat_thread_id=row.chat_thread_id).order_by(ConversationTurn.position).all()


def turn_for_workflow(workflow_id):
    return ConversationTurn.query.filter_by(run_id=workflow_id).first()


def load(draft_id, lock=False):
    """Compose a chat and its owned document; legacy draft_id means chat identity."""
    row = _draft_row(draft_id, lock=lock)
    # Read draft/version and its displayed turns in one statement snapshot.
    # Publication can commit between statements even where FOR UPDATE is absent.
    recent = ConversationTurn.query.filter_by(chat_thread_id=row.chat_thread_id).order_by(
        ConversationTurn.position.desc()).limit(DISPLAY_TURNS).subquery()
    turn_alias = aliased(ConversationTurn, recent)
    records = db.session.query(DraftDocument, turn_alias).outerjoin(
        turn_alias, DraftDocument.chat_thread_id == turn_alias.chat_thread_id).filter(
        DraftDocument.chat_thread_id == row.chat_thread_id).order_by(
        turn_alias.position).populate_existing().all()
    row = records[0][0]
    selected = [turn for _, turn in records if turn is not None]
    entries = _state_entries(row.chat_thread_id)
    state = _decode(entries[0].content, {}) if entries else {}
    state = {key: value for key, value in state.items() if key not in CANONICAL_KEYS}
    state.update(draft=_decode(row.draft_json, _blank_draft()), draft_version=row.draft_version,
                 chat_history=[[turn.user_text, turn.assistant_text] for turn in selected],
                 chat_history_meta=[_decode(turn.metadata_json, {}) for turn in selected])
    state['chat_thread_id'] = db.session.get(ChatThread, row.chat_thread_id).public_id
    state['draft_document_id'] = row.public_id
    if row.draft_before_json is not None:
        state['draft_before'] = _decode(row.draft_before_json)
    return state


def update_draft(draft_id, draft, expected_version, draft_before=UNSET):
    """Compatibility entry point: the legacy draft ID identifies a chat only."""
    return update_document(draft_id, None, draft, expected_version, draft_before)


def update_document(chat_id, document_id, draft, expected_version, draft_before=UNSET, *, source="manual", candidate_id=None):
    """Stage an owned document mutation after checking its concurrency token."""
    if not isinstance(draft, dict):
        raise ValueError('Draft content must be an object.')
    row = document_for_chat(chat_id, document_id, lock=True)
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version != row.draft_version:
        raise WorkspaceConflict(row.draft_version)
    from backend.util import draft_history
    previous_json, previous_time = row.draft_json, row.updated_at
    values = {'draft_json': _encode(draft), 'draft_version': expected_version + 1,
              'updated_at': datetime.utcnow()}
    if draft_before is not UNSET:
        values['draft_before_json'] = _encode(draft_before) if draft_before is not None else None
    # The predicate is also enforced by SQL, including SQLite where FOR UPDATE
    # is unavailable. A stale editor can never overwrite a newer version.
    changed = DraftDocument.query.filter_by(id=row.id, chat_thread_id=row.chat_thread_id,
        draft_version=expected_version).update(values, synchronize_session=False)
    db.session.expire(row)
    if changed != 1:
        raise WorkspaceConflict(row.draft_version)
    db.session.flush()
    draft_history.record(row, source='baseline', version=expected_version,
                         draft_json=previous_json, created_at=previous_time)
    draft_history.record(row, source=source, candidate_id=candidate_id)
    return row.draft_version


def append_turn(draft_id, user_text, assistant_text, meta=None, workflow_id=None, turn_id=None):
    """Stage one exchange; a workflow can publish at most one canonical turn."""
    if not isinstance(user_text, str) or not isinstance(assistant_text, str):
        raise ValueError('Conversation turn text must be strings.')
    chat_id = ChatThread.resolve_id(draft_id)
    # Admission and publication use chat -> workflow -> document lock ordering.
    # The caller already holds the chat lock when publishing a workflow result.
    ChatThread.query.filter_by(id=chat_id).populate_existing().with_for_update().one()
    row = _draft_row(chat_id, lock=True)
    if workflow_id:
        workflow = db.session.get(WorkspaceRun, workflow_id)
        if workflow is None or workflow.chat_thread_id != row.chat_thread_id:
            raise ValueError('Conversation workflow must belong to this workspace.')
        existing = turn_for_workflow(workflow_id)
        if existing:
            return existing
    meta = copy.deepcopy(meta) if isinstance(meta, dict) else {}
    identity = workflow_id or turn_id or meta.get('conversation_turn_id') or uuid.uuid4().hex
    uid = _stable_id(row.chat_thread_id, identity)
    existing = db.session.get(ConversationTurn, uid)
    if existing:
        if existing.chat_thread_id != row.chat_thread_id:
            raise ValueError('Conversation identity belongs to another workspace.')
        return existing
    if workflow_id:
        meta['run_id'] = workflow_id
    meta.setdefault('conversation_turn_id', uid)
    ChatThread.query.filter_by(id=row.chat_thread_id).update({
        'next_turn_position': ChatThread.next_turn_position + 1}, synchronize_session=False)
    chat = db.session.get(ChatThread, row.chat_thread_id)
    db.session.expire(chat, ['next_turn_position'])
    turn = ConversationTurn(id=uid, chat_thread_id=row.chat_thread_id, run_id=workflow_id,
        position=chat.next_turn_position - 1, user_text=user_text, assistant_text=assistant_text,
        metadata_json=_encode(meta), created_at=datetime.utcnow())
    db.session.add(turn)
    db.session.flush()
    return turn


def patch_auxiliary(draft_id, changes, remove=(), merge=()):
    """Stage only named noncanonical fields, preserving unrelated current metadata."""
    if not isinstance(changes, dict) or (set(changes) | set(remove)) & CANONICAL_KEYS:
        raise ValueError('Use draft and conversation helpers for canonical workspace fields.')
    # Preserve legacy core fields before the first auxiliary write strips them
    # from the residual blob. Subsequent writes never import that blob again.
    row = _draft_row(draft_id)
    entries = TextEntry.query.filter_by(chat_thread_id=row.chat_thread_id, textName='state').order_by(
        TextEntry.id).populate_existing().with_for_update().all()
    entry = entries[0] if entries else TextEntry(chat_thread_id=row.chat_thread_id, textName='state', content='{}')
    state = _decode(entry.content, {})
    state = {key: value for key, value in state.items() if key not in CANONICAL_KEYS}
    for key, value in changes.items():
        if key in merge:
            if not isinstance(value, dict):
                raise ValueError('Merged auxiliary fields must be objects.')
            current = state.get(key)
            state[key] = {**(current if isinstance(current, dict) else {}), **copy.deepcopy(value)}
        else:
            state[key] = copy.deepcopy(value)
    for key in remove:
        state.pop(key, None)
    entry.content = _encode(state)
    if not entries:
        db.session.add(entry)
    db.session.flush()
    return state
