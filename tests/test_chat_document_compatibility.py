"""Distinct identities retain existing chat, Word and deletion behavior."""
from datetime import datetime, timedelta
import json

import pytest

from backend.database import db
from backend.models import (AgentArtifact, AgentStep, CaseAssistantWorkflow,
                            ChatThread, ConversationTurn, DraftDocument)
from backend.util import workspace_store as store
from backend.util.db import DBMemory
from backend.word_addin.routes import publish_workspace_result
from tests.test_document_preparation import app


def distinct_documents():
    store.initialize(1)
    store.initialize(2)
    first = DraftDocument.query.filter_by(chat_thread_id=1).one()
    second = DraftDocument.query.filter_by(chat_thread_id=2).one()
    # Arrange IDs so a legacy primary-key lookup would return another chat's
    # document. Ownership queries must work independently of numeric coincidence.
    first.id = 701
    db.session.flush()
    second.id = 1
    db.session.commit()
    return first, second


def test_word_publication_resolves_owned_document_not_chat_primary_key(app):
    first, second = distinct_documents()
    other_content, other_version = second.draft_json, second.draft_version
    publish_workspace_result(1, {'draft': {'title': 'Owned Word result', 'sections': []},
        'chat_history': [['Improve the passage', 'Saved the result.']],
        'chat_history_meta': [{'entry_point': 'word_addin'}]}, expected_version=1)
    db.session.commit()
    restored = store.load(1)
    assert restored['draft_document_id'] == first.public_id
    assert restored['chat_thread_id'] == 'draft-1'
    assert restored['draft']['title'] == 'Owned Word result' and restored['draft_version'] == 2
    assert restored['chat_history_meta'][0]['draft_document_id'] == first.public_id
    assert (second.draft_json, second.draft_version) == (other_content, other_version)
    assert db.session.get(ChatThread, 1).next_turn_position == 1
    with pytest.raises(ValueError):
        store.document_for_chat(1, second.public_id)
    with pytest.raises(ValueError):
        ChatThread.resolve_id(first.public_id)
    memory = DBMemory(db, chat_thread_id=1)
    assert memory.doc_id == memory.chat_thread_id == 1
    assert json.loads(memory['state'])['draft_document_id'] == first.public_id


def test_deleting_chat_removes_owned_document_execution_and_turns_only(app):
    first, second = distinct_documents()
    now = datetime.utcnow()
    run = CaseAssistantWorkflow(id='wf_delete_chat', user_id=1, chat_thread_id=1,
        draft_document_id=first.id, base_draft_version=first.draft_version,
        status='completed', phase='finalize', execution_version=1, data_json='{}',
        result_json='{}', draft_hash='unchanged', created_at=now, updated_at=now,
        last_progress_at=now, deadline=now + timedelta(hours=1))
    db.session.add(run)
    db.session.flush()
    step = AgentStep(id='delete-step', workflow_run_id=run.id, sequence=0,
        kind='tool', name='create_draft', state='ready', created_at=now, updated_at=now)
    db.session.add(step)
    db.session.flush()
    db.session.add(AgentArtifact(id='delete-artifact', workflow_run_id=run.id,
        step_id=step.id, kind='draft', content_json='{}', created_at=now))
    store.append_turn(1, 'Question', 'Answer', workflow_id=run.id)
    store.append_turn(2, 'Other question', 'Other answer')
    db.session.commit()
    db.session.delete(db.session.get(ChatThread, 1))
    db.session.commit()
    assert DraftDocument.query.filter_by(chat_thread_id=1).count() == 0
    assert ConversationTurn.query.filter_by(chat_thread_id=1).count() == 0
    assert db.session.get(CaseAssistantWorkflow, run.id) is None
    assert db.session.get(AgentStep, step.id) is None
    assert db.session.get(AgentArtifact, 'delete-artifact') is None
    assert DraftDocument.query.filter_by(chat_thread_id=2).one().public_id == second.public_id
    assert store.load(2)['chat_history'] == [['Other question', 'Other answer']]


def test_alembic_autogeneration_preserves_the_unmapped_rollback_archive(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    # Flask app creation selects a default Celery app, and Alembic configures
    # process logging. Isolate those globals from the remaining backend tests.
    script = '''
import sys
from backend.app import create_app
from backend.database import db
from flask_migrate import check, stamp
from sqlalchemy import text
application = create_app({'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + sys.argv[1]})
with application.app_context():
    db.create_all()
    db.session.execute(text('CREATE TABLE workspace_draft_states (draft_id INTEGER PRIMARY KEY)'))
    db.session.commit()
    stamp(revision='head')
    check()
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'schema.db')],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
