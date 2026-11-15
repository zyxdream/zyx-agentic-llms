"""Confirmed document snapshots and proven legacy publications, scoped to a chat.

No commits here: snapshots share the editor/publication transaction. Candidate
history alone is not evidence that a draft was ever published to Output.
"""
import copy
import json
import uuid
from datetime import datetime

from backend.database import db
from backend.models import AgentArtifact, ConversationTurn, PublishedDraftVersion, WorkspaceRun


def version_id(document, version):
    # A not-yet-snapshotted current version uses this same stable identity in
    # read-only listings and in its eventual confirmed snapshot.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'zyx-agentic-llms:document:{document.public_id}:version:{version}'))


def record(document, *, source, candidate_id=None, version=None, draft_json=None, created_at=None):
    version = document.draft_version if version is None else version
    existing = PublishedDraftVersion.query.filter_by(document_id=document.id, draft_version=version).first()
    if existing:
        return existing
    saved = PublishedDraftVersion(id=version_id(document, version), document_id=document.id,
        draft_version=version, draft_json=document.draft_json if draft_json is None else draft_json,
        source=source, candidate_id=candidate_id, created_at=created_at or datetime.utcnow())
    db.session.add(saved)
    db.session.flush()
    return saved


def entries(document):
    result = []
    known_candidates = set()
    for saved in PublishedDraftVersion.query.filter_by(document_id=document.id).order_by(PublishedDraftVersion.draft_version.desc()):
        draft = json.loads(saved.draft_json)
        result.append({'version_id': saved.id, 'draft_version': saved.draft_version,
            'title': draft.get('title'), 'created_at': saved.created_at.isoformat(),
            'source': saved.source, 'candidate_id': saved.candidate_id,
            'current': saved.draft_version == document.draft_version, '_draft': draft})
        if saved.candidate_id:
            known_candidates.add(saved.candidate_id)
        elif draft.get('revision_id'):
            known_candidates.add(draft['revision_id'])
    if not any(entry['current'] for entry in result):
        current = json.loads(document.draft_json)
        result.insert(0, {'version_id': version_id(document, document.draft_version),
            'draft_version': document.draft_version, 'title': current.get('title'),
            'created_at': document.updated_at.isoformat(), 'source': 'baseline',
            'candidate_id': current.get('revision_id'), 'current': True, '_draft': current})
        if current.get('revision_id'):
            known_candidates.add(current['revision_id'])
    # Completed conversation turns prove publication. Never offer arbitrary
    # old artifacts, failed runs, or another conversation's drafts for rollback.
    turns = ConversationTurn.query.filter_by(chat_thread_id=document.chat_thread_id).order_by(ConversationTurn.position.desc())
    for turn in turns:
        meta = json.loads(turn.metadata_json or '{}')
        uid = meta.get('candidate_id')
        if not uid or uid in known_candidates or not meta.get('workflow_completed') or not turn.run_id:
            continue
        run = db.session.get(WorkspaceRun, turn.run_id)
        if not run or run.chat_thread_id != document.chat_thread_id or run.draft_document_id not in (None, document.id):
            continue
        saved = AgentArtifact.query.filter_by(id=uid, run_id=turn.run_id, kind='draft').first()
        if not saved:
            continue
        value = json.loads(saved.content_json)
        if value.get('draft_document_id') not in (None, document.public_id) or not isinstance(value.get('draft'), dict):
            continue
        result.append({'version_id': uid, 'draft_version': None,
            'title': value['draft'].get('title'), 'created_at': turn.created_at.isoformat(),
            'source': 'legacy_agent', 'candidate_id': uid, 'current': False, '_draft': value['draft']})
        known_candidates.add(uid)
    return result


def restore_content(document, version_id):
    for entry in entries(document):
        if entry['version_id'] == version_id:
            return copy.deepcopy(entry['_draft'])
    raise ValueError('That confirmed version is unavailable for this document. Use list_draft_versions for its exact version_id.')


def same_content(left, right):
    # Revision identities describe ancestry, not authored content. Everything
    # else, including title and export settings, participates in equality.
    ignore = {'revision_id', 'parent_revision_id', 'restored_from_version_id'}
    return {k: v for k, v in left.items() if k not in ignore} == {k: v for k, v in right.items() if k not in ignore}
