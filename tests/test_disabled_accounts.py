"""Account revocation at real HTTP identity and legacy worker boundaries."""
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest
from sqlalchemy.orm.attributes import set_committed_value

from backend.ai import base
from backend.database import db
from backend.models import ConversationTurn, TextEntry, User
from backend.util import analytics, workspace_store
from backend.util.account_access import (AccountUnavailable, account_execution,
                                         require_enabled_account)
from backend.util.db import DBMemory
from tests.test_document_preparation import app


def disable_owner():
    cached = db.session.get(User, 1)
    User.query.filter_by(id=1).update({'is_disabled': True}, synchronize_session=False)
    db.session.commit()
    set_committed_value(cached, 'is_disabled', False)
    return cached


@pytest.fixture
def http_identity_probe():
    # Importing backend.app creates the default Celery application. Keep that
    # initialization out of the parent process used by isolated worker tests.
    script = textwrap.dedent('''
        import sys
        from flask import Flask, jsonify
        from flask_cognito import CognitoAuth, cognito_auth_required
        from sqlalchemy.orm.attributes import set_committed_value
        from backend.app import lookup_cognito_user
        from backend.database import db
        from backend.models import User

        app = Flask(__name__)
        app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://',
                          COGNITO_REGION='us-east-1', COGNITO_USERPOOL_ID='test-pool',
                          COGNITO_APP_CLIENT_ID='test-client')
        db.init_app(app)
        auth = CognitoAuth(app)
        auth.identity_handler(lookup_cognito_user)
        claims = {'username': 'existing-user'}
        # Only the external JWT/JWKS boundary is mocked; use the real callback
        # and decorator for an otherwise valid token.
        auth.decode_token = lambda token: claims
        calls = []

        @app.post('/account-test')
        @cognito_auth_required
        def protected():
            calls.append('admitted')
            return jsonify(ok=True)

        with app.app_context():
            db.create_all()
            db.session.add_all([
                User(id=1, email='existing@example.test', cognito_username='existing-user', is_disabled=False),
                User(id=2, email='other@example.test', is_disabled=False)])
            db.session.commit()
            client = app.test_client()
            headers = {'Authorization': 'Bearer valid-token'}
            if sys.argv[1] == 'disabled':
                assert client.post('/account-test', headers=headers).status_code == 200
                cached = db.session.get(User, 1)
                User.query.filter_by(id=1).update({'is_disabled': True}, synchronize_session=False)
                db.session.commit()
                set_committed_value(cached, 'is_disabled', False)
                denied = client.post('/account-test', headers=headers)
                assert denied.status_code == 403, denied.json
                assert denied.json['error'] == 'Account unavailable'
                assert calls == ['admitted']
                assert User.query.count() == 2
            else:
                claims.update(username='new-user', email='new@example.test')
                assert client.post('/account-test', headers=headers).status_code == 200
                user = User.query.filter_by(cognito_username='new-user').one()
                assert not user.is_disabled
                assert calls == ['admitted']
    ''')
    def run(scenario):
        result = subprocess.run([sys.executable, '-c', script, scenario],
                                cwd=Path(__file__).resolve().parents[1],
                                text=True, capture_output=True, timeout=45)
        assert result.returncode == 0, result.stdout + result.stderr
    return run


def test_valid_token_requires_enabled_local_account(http_identity_probe):
    http_identity_probe('disabled')


def test_identity_still_initializes_new_enabled_local_account(http_identity_probe):
    http_identity_probe('new')


def test_guard_does_not_flush_commit_or_discard_caller_work(app):
    disable_owner()
    staged = TextEntry(chat_thread_id=1, textName='staged', content='Not yet committed')
    db.session.add(staged)
    with pytest.raises(AccountUnavailable):
        require_enabled_account(1)
    assert staged in db.session.new and staged.id is None
    db.session.rollback()
    assert TextEntry.query.filter_by(textName='staged').count() == 0
    with pytest.raises(AccountUnavailable):
        require_enabled_account(999)


def test_queued_guided_work_cannot_run_for_disabled_owner(app):
    disable_owner()

    @analytics.guided_step_operation('account-test', 1, lambda: 1)
    def run():
        pytest.fail('Disabled guided work executed')

    with pytest.raises(AccountUnavailable):
        run()


def test_legacy_provider_rechecks_account_and_context_is_released(app, monkeypatch):
    fake_client = object()
    monkeypatch.setattr(base, 'client', fake_client)
    with pytest.raises(AccountUnavailable):
        with account_execution(1):
            assert base.get_openai_client() is fake_client
            disable_owner()
            base.get_openai_client()
    # The account context must not leak into the next request or task.
    assert base.get_openai_client() is fake_client


def test_guided_returned_result_denied_after_account_revocation(app):
    @analytics.guided_step_operation('account-test', 1, lambda: 1)
    def run():
        disable_owner()
        return {'response': 'Late provider output'}

    with pytest.raises(AccountUnavailable):
        run()


@pytest.mark.parametrize('decorator', [analytics.guided_step_operation, analytics.attach_step_usage])
def test_auxiliary_and_guided_work_share_owner_authorization(app, decorator):
    disable_owner()

    @decorator('account-test', 1, lambda: 1)
    def run():
        pytest.fail('Disabled work reached the operation')

    with pytest.raises(AccountUnavailable):
        run()


def test_guided_output_denied_after_revocation_and_existing_output_retained(app):
    memory = DBMemory(db, 1)
    memory['saved-analysis'] = 'Previously completed work'

    @analytics.guided_step_operation('account-test', 1, lambda: 1)
    def run():
        disable_owner()
        memory['saved-analysis'] = 'Late provider output'

    with pytest.raises(AccountUnavailable):
        run()
    assert memory['saved-analysis'] == 'Previously completed work'
    del memory['saved-analysis']  # Cleanup remains available after revocation.


def test_legacy_publication_rejects_disabled_owner_without_changing_draft_or_turns(app):
    from backend import everything
    memory = DBMemory(db, 1)
    workspace_store.initialize(1)
    db.session.commit()
    before = workspace_store.load(1)
    state = everything.create_empty_state()
    state.update(before)
    state['draft'] = {'title': 'Late draft', 'sections': []}
    state['_workspace_draft_write'] = True
    everything.add_chat_message(state, ('Request', 'Late answer'))
    disable_owner()

    with pytest.raises(AccountUnavailable):
        everything.save_state_to_memory(state, memory)
    after = workspace_store.load(1)
    assert (after['draft'], after['draft_version']) == (before['draft'], before['draft_version'])
    assert ConversationTurn.query.count() == 0


@pytest.mark.parametrize('task_name', ['smart_search_task', 'smart_search_phase1', 'smart_search_phase2'])
def test_disabled_queued_search_denied_without_touching_history(app, monkeypatch, task_name):
    from backend import smart_search
    from tests.test_word_addin import MemoryRedis
    monkeypatch.setattr(smart_search, '_redis', MemoryRedis())
    monkeypatch.setattr(smart_search, 'multichat_completion_text', lambda *a, **kw: pytest.fail('Provider called'))
    disable_owner()
    with pytest.raises(AccountUnavailable):
        getattr(smart_search, task_name).run({'project_id': 1, 'session_id': 'search', 'query': 'Question'})
    assert smart_search.load_state('search')['chat_history'] == []


def test_search_revocation_blocks_late_answer_and_clears_active_task(app, monkeypatch):
    from backend import smart_search
    from tests.test_word_addin import MemoryRedis
    monkeypatch.setattr(smart_search, '_redis', MemoryRedis())
    monkeypatch.setattr(smart_search.smart_search_task, 'update_state', lambda **kw: None)
    monkeypatch.setattr(smart_search, 'classify_intent', lambda *a, **kw:
                        (False, False, 'all_matter_and_chat', 'relevant', [], [], False, False, []))
    monkeypatch.setattr(smart_search, 'update_conversation_brief', lambda *a, **kw: ('', {}))
    monkeypatch.setattr(smart_search, 'retrieve_relevant_chat_turns', lambda *a, **kw: '')
    def provider(*args, **kwargs):
        disable_owner()
        return 'Late answer'
    monkeypatch.setattr(smart_search, 'multichat_completion_text', provider)
    smart_search.smart_search_task.push_request(id='search-task')
    try:
        with pytest.raises(AccountUnavailable):
            smart_search.smart_search_task.run({'project_id': 1, 'session_id': 'search', 'query': 'Question'})
    finally:
        smart_search.smart_search_task.pop_request()
    state = smart_search.load_state('search')
    assert state['chat_history'] == [] and not state.get('active_task_id')
    from backend.models import SmartSearchMessage
    assert SmartSearchMessage.query.count() == 0


@pytest.mark.parametrize('during_provider', [False, True])
def test_legacy_research_cannot_run_or_return_late_result_for_disabled_owner(app, monkeypatch, during_provider):
    from backend import research
    calls = []
    def provider(*args, **kwargs):
        calls.append('provider')
        disable_owner()
        return 'Late research'
    monkeypatch.setattr(research, 'multichat_completion_text', provider)
    monkeypatch.setattr(research, 'filter_raw_citations', lambda text: [])
    if not during_provider:
        disable_owner()
    with pytest.raises(AccountUnavailable):
        research.research_task.run({'user_id': 1, 'conversation': [{'input': 'Question', 'response': ''}]})
    assert len(calls) == int(during_provider)


def test_research_admission_binds_authenticated_owner(app, monkeypatch):
    from types import SimpleNamespace
    from backend import views
    calls = []
    monkeypatch.setattr(views, 'current_user', SimpleNamespace(id=1))
    monkeypatch.setattr(views, 'analytics_record_research', lambda *a: None)
    monkeypatch.setattr(views.research_task, 'delay', lambda data: calls.append(data.copy()) or SimpleNamespace(id='task'))
    with app.test_request_context('/research', method='POST', json={'query': 'Question', 'user_id': 2}):
        assert views.research_api.__wrapped__()[1] == 200
    assert calls[0]['user_id'] == 1
