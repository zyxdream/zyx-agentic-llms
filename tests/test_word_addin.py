"""Word routes with real Flask/SQLAlchemy and mocked identity/model boundaries."""
import base64
import hashlib
import json
import time
import uuid

import jwt
import pytest
from flask import Flask
from flask_cognito import CognitoAuth

from backend.database import db
from backend.models import DocumentChunk, Drafts, Project, UploadedFiles, User
from backend.word_addin import routes, service, tasks


class MemoryRedis:
    """Deterministic Redis boundary double; Lua is separately exercised in CI."""
    def __init__(self):
        self.values = {}
    def get(self, key):
        return self.values.get(key)
    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True
    def setex(self, key, seconds, value):
        self.values[key] = value
    def incr(self, key):
        value = int(self.values.get(key, 0)) + 1
        self.values[key] = value
        return value
    def expire(self, key, seconds):
        return True
    def eval(self, script, number, key, *args):
        if script == routes.CONSUME:
            return self.values.pop(key, None)
        if script == routes.RELEASE:
            if self.values.get(key) == args[0]:
                self.values.pop(key, None)
                return 1
            return 0
        if script == routes.RENEW:
            return int(self.values.get(key) == args[0] and self.expire(key, args[1]))
        raise AssertionError("Unexpected Redis script")


@pytest.fixture
def app(monkeypatch):
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True, WORD_ADDIN_ENABLED=True,
                      WORD_ADDIN_ORIGINS=["https://test.lextext.ai"], WORD_ADDIN_EMIT_CORS=True,
                      COGNITO_REGION="us-east-1",
                      COGNITO_USERPOOL_ID="test-pool", COGNITO_APP_CLIENT_ID="test-client")
    db.init_app(app)
    auth = CognitoAuth(app)
    # Only the external JWKS boundary is mocked. Routes retain the real decorator.
    monkeypatch.setattr(auth, "decode_token", lambda token: jwt.decode(token, options={"verify_signature": False}))
    auth.identity_handler(lambda claims: db.session.get(User, int(claims["sub"])))
    app.extensions["word_redis"] = MemoryRedis()
    routes.register_word_addin(app)
    monkeypatch.setattr(routes, "enqueue", lambda draft_id: None)
    with app.app_context():
        db.create_all()
        db.session.add_all([User(id=1, email="lawyer@example.com", is_disabled=False), User(id=2, email="other@example.com", is_disabled=False)])
        db.session.add_all([Project(id=1, userId=1, name="Own matter"), Project(id=2, userId=2, name="Other matter")])
        db.session.add_all([UploadedFiles(id=1, unique_id="own-file", project_id=1, original_filename="Email.txt", content="March notice", file_size=12),
                           UploadedFiles(id=2, unique_id="other-file", project_id=2, original_filename="Secret.txt", content="other matter", file_size=12)])
        db.session.add(DocumentChunk(uploaded_file_id=1, project_id=1, chunk_index=0, content="Defendant received notice on March 14.", token_count=8, char_count=38, source_filename="Email.txt", source_unique_id="own-file"))
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


def headers(user=1, exp=None):
    token = jwt.encode({"sub": str(user), "exp": exp if exp is not None else time.time() + 600,
                        "token_use": "access"}, "test-key-only-at-least-thirty-two-characters", algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def payload(**changes):
    return {"request_id": str(uuid.uuid4()), "project_id": 1, "action": "revise", "text": "Defendant received notice in March.",
            "scope": "selection", "instructions": "Tighten.", "document_name": "Motion.docx", "file_ids": [], **changes}


def test_opt_in_and_auth(app):
    c = app.test_client()
    assert c.get("/api/word/workspace").status_code == 401
    assert c.get("/api/word/workspace", headers=headers(exp=time.time() - 20)).status_code == 401
    assert c.get("/api/word/workspace", headers=headers()).json["matters"] == [{"id": 1, "name": "Own matter"}]
    app.config["WORD_ADDIN_ENABLED"] = False
    assert c.get("/api/word/workspace", headers=headers()).status_code == 404


def test_word_request_size_limit_is_compatible_and_scoped(app):
    c = app.test_client()
    assert c.get("/api/word/workspace").status_code == 401
    response = c.post(
        "/api/word/operations",
        headers={**headers(), "Content-Type": "application/json"},
        data=b"x" * (routes.MAX_REQUEST_BYTES + 1),
    )
    assert response.status_code == 413
    assert response.json == {"error": "The Word request is too large."}


def test_disabled_and_unmapped_users_denied(app):
    user = db.session.get(User, 1); user.is_disabled = True; db.session.commit()
    assert app.test_client().get("/api/word/workspace", headers=headers()).status_code == 403
    assert app.test_client().get("/api/word/workspace", headers=headers(user=100)).status_code == 403


def test_cross_matter_sources_and_jobs_are_denied(app):
    c = app.test_client()
    assert c.get("/api/word/matters/2/sources", headers=headers()).status_code == 404
    assert c.post("/api/word/operations", headers=headers(), json=payload(project_id=2)).status_code == 404
    assert c.post("/api/word/operations", headers=headers(), json=payload(file_ids=["other-file"])).status_code == 400
    assert c.get("/api/word/matters/1/sources", headers=headers()).json == {"sources": [{"id": "own-file", "name": "Email.txt"}]}
    operation = c.post("/api/word/operations", headers=headers(), json=payload()).json["operation_id"]
    assert c.get(f"/api/word/operations/{operation}", headers=headers(2)).status_code == 404
    assert c.post(f"/api/word/operations/{operation}/cancel", headers=headers(2)).status_code == 404


def test_cors_is_exact_and_responses_are_not_cached(app):
    c = app.test_client()
    response = c.options("/api/word/auth/exchange", headers={"Origin": "https://test.lextext.ai"})
    assert response.headers["Access-Control-Allow-Origin"] == "https://test.lextext.ai"
    assert response.headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in c.options("/api/word/auth/exchange", headers={"Origin": "https://attacker.example"}).headers
    app.config["WORD_ADDIN_EMIT_CORS"] = False
    assert "Access-Control-Allow-Origin" not in c.get(
        "/api/word/workspace", headers={"Origin": "https://test.lextext.ai"}
    ).headers


def test_pkce_grant_is_bound_single_use_and_expiring(app):
    c = app.test_client(); verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    code = c.post("/api/word/auth/issue", headers=headers(), json={"challenge": challenge}).json["code"]
    result = c.post("/api/word/auth/exchange", json={"code": code, "verifier": verifier})
    assert result.status_code == 200 and result.json["access_token"]
    assert c.post("/api/word/auth/exchange", json={"code": code, "verifier": verifier}).status_code == 401
    code = c.post("/api/word/auth/issue", headers=headers(), json={"challenge": challenge}).json["code"]
    assert c.post("/api/word/auth/exchange", json={"code": code, "verifier": "x" * 43}).status_code == 401
    assert c.post("/api/word/auth/exchange", json={"code": code, "verifier": verifier}).status_code == 401
    assert c.post("/api/word/auth/issue", headers=headers(exp=time.time()-1), json={"challenge": challenge}).status_code == 401


@pytest.mark.parametrize("change", [
    {"text": "x" * 60001}, {"text": ""}, {"scope": "all"}, {"scope": "document"},
    {"action": "unknown"}, {"action": "record"}, {"action": "answer"},
    {"action": "answer", "stance": "custom", "instructions": ""},
    {"action": "discovery", "instructions": ""}, {"file_ids": ["x"] * 31},
    {"instructions": {}}, {"project_id": True}, {"request_id": "invalid"},
])
def test_invalid_requests_do_not_enqueue(app, monkeypatch, change):
    calls = []; monkeypatch.setattr(routes, "enqueue", calls.append)
    response = app.test_client().post("/api/word/operations", headers=headers(), json=payload(**change))
    assert response.status_code == 400
    assert calls == []


def test_idempotency_and_concurrency_limit(app, monkeypatch):
    c = app.test_client(); calls = []; monkeypatch.setattr(routes, "enqueue", calls.append)
    p = payload(); a = c.post("/api/word/operations", headers=headers(), json=p)
    b = c.post("/api/word/operations", headers=headers(), json=p)
    assert a.status_code == 202 and a.json == b.json and len(calls) == 1
    from backend.models import DraftDocument
    draft = Drafts.query.filter_by(public_id=a.json['operation_id']).one()
    canonical = DraftDocument.query.filter_by(chat_thread_id=draft.id).one()
    assert canonical is not None and canonical.draft_version == 1
    assert c.post("/api/word/operations", headers=headers(), json={**p, "text": "Different"}).status_code == 409
    assert c.post("/api/word/operations", headers=headers(), json=payload()).status_code == 409


def test_worker_reuses_lextext_model_and_saves_own_task(app, monkeypatch):
    seen = []
    def model(prompt, system):
        seen.append(json.loads(prompt))
        return {"text": "Notice arrived in March.", "notes": [], "findings": []}
    monkeypatch.setattr(service, "call_model", model)
    c = app.test_client(); operation = c.post("/api/word/operations", headers=headers(), json=payload(file_ids=["own-file"])).json["operation_id"]
    draft = Drafts.query.filter_by(public_id=operation).one()
    tasks.word_operation.run(draft.id)
    response = c.get(f"/api/word/operations/{operation}", headers=headers()).json
    assert response["state"] == "complete"
    assert response["result"]["text"] == "Notice arrived in March."
    assert seen[0]["record_excerpts"][0]["filename"] == "Email.txt"
    assert routes.entry(draft.id, "state")["draft"]["sections"][0]["section_content"] == "Notice arrived in March."
    assert app.extensions["word_redis"].get("word:active:1") is None
    assert tasks.word_operation.ignore_result is True


def test_cancellation_wins_over_late_output(app, monkeypatch):
    c = app.test_client(); operation = c.post("/api/word/operations", headers=headers(), json=payload()).json["operation_id"]
    draft = Drafts.query.filter_by(public_id=operation).one()
    def delayed_model(*args):
        routes.transition(draft.id, {"running"}, "cancelled", message="Cancelled")
        return {"kind": "revision", "text": "Too late", "findings": [], "notes": [], "sources": []}
    monkeypatch.setattr(tasks, "execute", delayed_model)
    tasks.word_operation.run(draft.id)
    assert c.get(f"/api/word/operations/{operation}", headers=headers()).json["state"] == "cancelled"
    assert routes.entry(draft.id, "word.result") is None


def test_passage_progress_is_persisted_and_cancellation_stops_before_model(app, monkeypatch):
    c = app.test_client()
    operation = c.post('/api/word/operations', headers=headers(), json=payload()).json['operation_id']
    draft = Drafts.query.filter_by(public_id=operation).one()
    stages = []
    def read_sources(*_):
        stages.append(routes.entry(draft.id, 'word.status')['message'])
        routes.transition(draft.id, {'running'}, 'cancelled', message='Cancelled')
        return [], []
    monkeypatch.setattr(service, 'retrieve_sources', read_sources)
    monkeypatch.setattr(service, 'call_model', lambda *_, **__: pytest.fail('Cancelled work must not call the model'))
    tasks.word_operation.run(draft.id)
    assert stages == ['Preparing your task…']
    assert routes.entry(draft.id, 'word.status')['state'] == 'cancelled'
    assert routes.entry(draft.id, 'word.result') is None
    assert app.extensions['word_redis'].get('word:active:1') is None


def test_duplicate_worker_does_not_release_another_execution(app, monkeypatch):
    c = app.test_client(); operation = c.post("/api/word/operations", headers=headers(), json=payload()).json["operation_id"]
    draft = Drafts.query.filter_by(public_id=operation).one()
    routes.transition(draft.id, {"queued"}, "running")
    tasks.word_operation.run(draft.id)
    assert app.extensions["word_redis"].get("word:active:1") == operation


@pytest.mark.parametrize("newer_operation", [None, "newer-operation"])
def test_expired_queued_task_cannot_run_or_release_a_newer_slot(app, monkeypatch, newer_operation):
    c = app.test_client()
    operation = c.post("/api/word/operations", headers=headers(), json=payload()).json["operation_id"]
    draft = Drafts.query.filter_by(public_id=operation).one()
    r = app.extensions["word_redis"]
    r.values.pop("word:active:1")
    if newer_operation:
        r.setex("word:active:1", routes.TTL, newer_operation)
    calls = []
    monkeypatch.setattr(tasks, "execute", lambda *args: calls.append(args))
    tasks.word_operation.run(draft.id)
    assert calls == []
    assert routes.entry(draft.id, "word.status")["state"] == "failed"
    assert routes.entry(draft.id, "word.result") is None
    assert r.get("word:active:1") == newer_operation


def test_unknown_or_fabricated_source_quotes_never_get_support_status():
    req = service.validate_request(payload(action="record", file_ids=["own-file"]))
    raw = {"text": "Review", "notes": [], "findings": [
        {"title": "Invented", "anchor": "Not in document", "status": "support", "evidence": [{"source_id": "S1", "quote": "invented quote"}]},
        {"title": "Unknown", "status": "support", "evidence": [{"source_id": "S99", "quote": "real quote"}]},
        {"title": "Located", "status": "support", "evidence": [{"source_id": "S1", "quote": "real quote"}]},
    ]}
    result = service.normalize_result(raw, req, [{"id": "S1", "excerpt": "A real quote exists."}])
    assert [f["status"] for f in result["findings"]] == ["review", "review", "support"]
    assert result["findings"][0]["anchor"] == ""


def test_unindexed_source_blocks_instead_of_silently_omitting_it(app):
    db.session.query(DocumentChunk).delete(); db.session.commit()
    with pytest.raises(ValueError, match="not indexed"):
        service.retrieve_sources(1, service.validate_request(payload(file_ids=["own-file"])))


def test_malformed_model_output_is_not_insertable():
    req = service.validate_request(payload())
    for value in [None, 'not-json', {"text": ""}, {"text": "ok", "findings": {}}]:
        with pytest.raises(ValueError):
            service.normalize_result(value, req, [])


def test_real_redis_grant_scripts_when_available():
    import os
    from redis import Redis
    url = os.environ.get("WORD_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set WORD_TEST_REDIS_URL for the Redis script integration test")
    r = Redis.from_url(url, decode_responses=True)
    key = "word:test:" + str(uuid.uuid4())
    r.setex(key, 30, "lease")
    assert r.eval(routes.RELEASE, 1, key, "wrong") == 0
    assert r.eval(routes.RENEW, 1, key, "wrong", routes.TTL) == 0
    assert r.ttl(key) <= 30
    assert r.eval(routes.RENEW, 1, key, "lease", routes.TTL) == 1
    assert r.ttl(key) > routes.TTL - 10
    assert r.eval(routes.CONSUME, 1, key) == "lease"
    assert r.eval(routes.CONSUME, 1, key) is None


def test_general_question_has_no_document_and_can_finish(app, monkeypatch):
    c = app.test_client()
    seen = []
    def model(prompt, system):
        seen.append(json.loads(prompt))
        return {'text': 'Clarify the jurisdiction and the relief requested.', 'findings': [], 'notes': []}
    monkeypatch.setattr(service, 'call_model', model)
    response = c.post('/api/word/operations', headers=headers(), json=payload(
        action='ask', scope='none', text='', document_name='', instructions='What is a preliminary injunction?'))
    assert response.status_code == 202
    draft = Drafts.query.filter_by(public_id=response.json['operation_id']).one()
    tasks.word_operation(draft.id)
    result = c.get(f"/api/word/operations/{draft.public_id}", headers=headers()).json
    assert result['state'] == 'complete'
    assert result['result']['kind'] == 'report'
    assert seen[0]['word_text'] == ''


@pytest.mark.parametrize('extra', [
    {'action': 'revise'}, {'text': 'secret text'}, {'document_name': 'secret.docx'},
    {'blocks': [{'text': 'secret'}]}, {'instructions': ''},
])
def test_no_document_scope_rejects_hidden_document_or_edit(app, extra):
    request = payload(action='ask', scope='none', text='', document_name='', instructions='Explain the standard.')
    request.update(extra)
    assert app.test_client().post('/api/word/operations', headers=headers(), json=request).status_code == 400


def test_layout_request_returns_honest_guidance_without_model_or_retrieval(monkeypatch):
    from backend.word_addin.document_workflow import execute_document
    from tests.test_word_document_workflow import document_payload
    monkeypatch.setattr(service, 'call_model', lambda *_: pytest.fail('Unsupported layout must not call model'))
    monkeypatch.setattr(service, 'retrieve_sources', lambda *_: pytest.fail('Unsupported layout must not fetch sources'))
    request = service.validate_request(payload(instructions='Format to comply with local rules'))
    for result in [service.execute(request, 1), execute_document({**document_payload(), 'instructions': request['instructions']}, 1)]:
        assert result['kind'] == 'report'
        assert 'Automatic court formatting is not available in the Word add-in' in result['text']
        assert 'Sources & tools' not in result['text']
        assert 'Nothing was changed in Word' in result['text']
        assert not result.get('edits')


def word_copy_payload(**changes):
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as package:
        package.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        package.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:del w:id="1"><w:r><w:delText>Old</w:delText></w:r></w:del><w:ins w:id="2"><w:r><w:t>New</w:t></w:r></w:ins></w:p></w:body></w:document>')
        package.writestr('word/comments.xml', '<comments>Keep the lawyer comment</comments>')
    return {'request_id': str(uuid.uuid4()), 'project_id': 1, 'filename': 'Reply brief.docx',
            'content': base64.b64encode(buffer.getvalue()).decode(), **changes}


def test_save_word_copy_preserves_exact_file_and_retry_is_idempotent(app):
    c = app.test_client()
    value = word_copy_payload()
    first = c.post('/api/word/documents', json=value, headers=headers())
    assert first.status_code == 201
    document_id = first.json['id']
    retry = c.post('/api/word/documents', json=value, headers=headers())
    assert retry.status_code == 200
    assert retry.json == first.json
    assert Drafts.query.count() == 1
    download = c.get(f'/api/word/documents/{document_id}/file', headers=headers())
    assert download.data == base64.b64decode(value['content'])
    assert 'attachment' in download.headers['Content-Disposition']
    assert download.headers['Cache-Control'] == 'no-store'
    draft = Drafts.query.first()
    state = routes.entry(draft.id, 'state')
    assert f'/word-documents/{document_id}' in state['chat_history'][0][1]
    assert c.post('/api/word/documents', json={**value, 'filename': 'Different.docx'}, headers=headers()).status_code == 409
    assert c.post('/api/word/documents', json=word_copy_payload(), headers=headers()).status_code == 201
    assert Drafts.query.count() == 2  # New explicit save keeps the earlier copy.


def test_saved_copies_require_owner_and_valid_session(app):
    c = app.test_client()
    value = word_copy_payload()
    assert c.post('/api/word/documents', json=value).status_code == 401
    assert c.post('/api/word/documents', json={**value, 'project_id': 2}, headers=headers()).status_code == 404
    document_id = c.post('/api/word/documents', json=value, headers=headers()).json['id']
    for suffix in ['', '/file']:
        assert c.get(f'/api/word/documents/{document_id}{suffix}', headers=headers(2)).status_code == 404
        assert c.get(f'/api/word/documents/{document_id}{suffix}').status_code == 401
    assert c.post('/api/word/documents', json={**value, 'project_id': 2}, headers=headers(2)).status_code == 409


@pytest.mark.parametrize('changes', [
    {'content': 'not base64!'}, {'content': base64.b64encode(b'Not a DOCX').decode()},
    {'filename': '../brief.docx'}, {'filename': 'brief.docm'}, {'project_id': True}, {'request_id': 'invalid'},
])
def test_invalid_saved_copy_never_creates_a_draft(app, changes):
    assert app.test_client().post('/api/word/documents', json=word_copy_payload(**changes), headers=headers()).status_code == 400
    assert Drafts.query.count() == 0


def test_save_request_limit_is_separate_from_model_request_limit(app):
    from backend.word_addin.saved_documents import MAX_SAVE_REQUEST_BYTES
    with app.test_request_context('/api/word/documents', method='POST'):
        assert routes.request_limit() == MAX_SAVE_REQUEST_BYTES
    with app.test_request_context('/api/word/operations', method='POST'):
        assert routes.request_limit() == routes.MAX_REQUEST_BYTES
    response = app.test_client().post('/api/word/documents', headers=headers(),
        data=b'x' * (MAX_SAVE_REQUEST_BYTES + 1), content_type='application/json')
    assert response.status_code == 413


def test_saved_copy_rejects_active_content_and_entity_declarations(app):
    import io
    import zipfile
    for part, content in [('word/vbaProject.bin', b'macro'),
                          ('word/document.xml', b'<!DOCTYPE x [<!ENTITY x "bad">]><x/>')]:
        value = word_copy_payload()
        original = zipfile.ZipFile(io.BytesIO(base64.b64decode(value['content'])))
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            for name in original.namelist():
                if name != part:
                    archive.writestr(name, original.read(name))
            archive.writestr(part, content)
        value['content'] = base64.b64encode(output.getvalue()).decode()
        assert app.test_client().post('/api/word/documents', json=value, headers=headers()).status_code == 400
    assert Drafts.query.count() == 0


def test_selection_citation_context_is_validated_and_used(monkeypatch):
    from backend.word_addin import citations
    seen = []
    monkeypatch.setattr(citations, 'review_group', lambda text, context, *args: seen.append((text, context)) or {'text': 'done'})
    request = service.validate_request(payload(action='citations', text='Short form', citation_context='Full case elsewhere'))
    service.execute(request, 1)
    assert seen == [('Short form', 'Full case elsewhere')]
    for extra in [{'citation_context': 4}, {'citation_context': 'x' * 180001},
                  {'action': 'ask', 'scope': 'none', 'text': '', 'document_name': '', 'citation_context': 'private'},
                  {'action': 'revise', 'citation_context': 'private'}]:
        with pytest.raises(ValueError):
            service.validate_request({**payload(), **extra})
