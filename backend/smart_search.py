from celery import shared_task, Task
import json
import logging
import redis as redis_lib

from datetime import datetime, timezone

import backend.ai.base as ai_base
from backend.ai.base import multichat_completion_text, num_tokens_from_string
from backend.database import db
from backend.models import UploadedFiles, SmartSearchSession, SmartSearchMessage
from backend.util.bm25_search import format_bm25_results
from backend.constants import REDIS_URL
from backend.util.analytics import analytics_record
from backend.util.account_access import (
    AccountUnavailable, account_execution, check_current_account, project_account_id,
)

from backend.everything import (
    classify_intent,
    bm25_search_chunks,
    get_full_content_chunks,
    get_relevant_content_bm25,
    retrieve_relevant_chat_turns,
    get_reduced_chat_history,
    update_conversation_brief,
    add_chat_message,
    SYSTEM_PROMPT,
    _status,
)

_redis = redis_lib.from_url(REDIS_URL)
STATE_TTL = 86400  # 24 hours
logger = logging.getLogger(__name__)
SMART_SEARCH_SCOPE_VERSION = 1
KEYWORD_REASONING_EFFORT = "high"
ANSWER_REASONING_EFFORT = "high"
NO_FILES_IN_SCOPE_ERROR = (
    "No files are available in the selected scope. "
    "Select a file or folder with documents and try again."
)


def create_empty_state():
    return {
        'chat_history': [],
        'conversation_brief': '',
        'constraints': {},
        'extraction_cache': {},
        'last_extraction': None,
        'active_task_id': None,
    }


def load_state(session_id: str) -> dict:
    key = f"smart_search_state:{session_id}"  # Build per-session Redis key namespace.
    raw = _redis.get(key)  # Attempt fast-path read from Redis hot cache.
    if raw:  # If cache hit, deserialize and return without querying SQL.
        try:
            state = json.loads(raw)  # Redis stores bytes/string JSON; convert to Python dict.
            state.setdefault('chat_history', [])  # Backfill missing key for backward compatibility.
            state.setdefault('conversation_brief', '')  # Ensure summary key always exists.
            state.setdefault('constraints', {})  # Ensure constraints map exists for downstream logic.
            state.setdefault('extraction_cache', {})  # Ensure memoized extraction cache exists.
            state.setdefault('last_extraction', None)  # Ensure "last extraction" pointer always present.
            state.setdefault('active_task_id', None)
            return state  # Serve cached state immediately.
        except (json.JSONDecodeError, TypeError):  # Handle corrupt/invalid Redis payload safely.
            pass  # Fall through to SQL rebuild path below.

    # Redis cache miss — reconstruct from SQL
    session = SmartSearchSession.query.get(session_id)  # Load session metadata row from SQL.
    if session:  # Only rebuild if session exists in persistent storage.
        messages = SmartSearchMessage.query.filter_by(session_id=session_id)\
            .order_by(SmartSearchMessage.created_at, SmartSearchMessage.id).all()  # Deterministic chronological message stream.

        # Pair up user/assistant rows into (user_msg, assistant_msg) tuples
        chat_history = []  # Target in-memory format used by the smart search pipeline.
        i = 0  # Manual index for two-pointer style scan over flat message list.
        while i < len(messages):  # Linear O(n) pass through ordered messages.
            if messages[i].role == 'user' and i + 1 < len(messages) and messages[i + 1].role == 'assistant':  # Valid adjacent user->assistant turn.
                chat_history.append((messages[i].content, messages[i + 1].content))  # Store normalized tuple turn.
                i += 2  # Advance by one complete turn when pair matched.
            else:
                i += 1  # Skip unmatched/malformed row and continue scanning.

        try:
            constraints = json.loads(session.constraints_json) if session.constraints_json else {}  # Parse persisted constraints blob.
        except (json.JSONDecodeError, TypeError):
            constraints = {}  # Fallback to empty constraints on bad JSON.

        state = {
            'chat_history': chat_history,  # Reconstructed turn pairs from SQL messages.
            'conversation_brief': session.conversation_brief or '',  # Persisted summary string (or empty).
            'constraints': constraints,  # Parsed constraints dict used by prompt-building logic.
            'extraction_cache': {},  # Intentionally ephemeral; repopulated during runtime.
            'last_extraction': None,  # Ephemeral pointer not persisted in SQL rebuild.
            'active_task_id': None,
        }  # Canonical runtime state object for smart search session.
        # Repopulate Redis hot cache
        _redis.set(key, json.dumps(state), ex=STATE_TTL)  # Write-through cache warm-up with TTL expiration.
        logger.info(
            "[smart_search] Rebuilt state from SQL for session %s (%s turns)",
            session_id,
            len(chat_history),
        )
        return state  # Return rebuilt state after cache repopulation.

    return create_empty_state()  # Session not found anywhere; start from default empty structure.


def save_state(session_id: str, state: dict, *, check_access=True):
    if check_access:
        check_current_account()
    # Build the Redis key from the session identifier.
    # Using the same prefix as load_state(...) means both functions read and write
    # the same cache entry for one smart-search session.
    key = f"smart_search_state:{session_id}"
    # Convert the Python dictionary into JSON text because Redis stores bytes/strings,
    # not native Python objects.
    # "TTL" means "time to live": ex=STATE_TTL tells Redis to expire this key
    # automatically after that many seconds unless a later write refreshes it.
    _redis.set(key, json.dumps(state), ex=STATE_TTL)

 
def _is_cancelled(session_id: str, expected_task_id: str) -> bool:
    # Read the latest shared state for this session.
    # We do not trust an older local variable here, because another request or
    # worker may already have updated Redis for the same session after this task started.
    # load_state(...) reads from Redis first, and if Redis is missing the key,
    # it rebuilds the state from SQL.
    fresh = load_state(session_id)
    # active_task_id is the task identifier currently recorded as "the active one"
    # for this session.
    # If that stored identifier is different from expected_task_id, this task has
    # lost ownership: either a newer task replaced it, or the active marker was removed.
    # dict.get(...) returns None when the key does not exist, so a missing key also
    # counts as "cancelled" because None != expected_task_id.
    return fresh.get('active_task_id') != expected_task_id
 
 
def _clear_active_task(session_id: str, expected_task_id: str):
    # Read the newest shared state before modifying it.
    # This protects against a race where this task is finishing, but a newer task
    # has already started and stored its own active_task_id in Redis.
    fresh = load_state(session_id)
    # Only clear the marker when the currently stored task identifier still matches
    # this task's expected_task_id.
    # That comparison prevents this older task from accidentally deleting the marker
    # written by a newer task.
    if fresh.get('active_task_id') == expected_task_id:
        # Remove the active_task_id key from the dictionary.
        # pop(..., None) avoids KeyError if the key disappeared between checks,
        # and calling it multiple times has the same safe result.
        fresh.pop('active_task_id', None)
        # Save the modified state back to Redis so future reads immediately see
        # that this session no longer has an active task recorded.
        save_state(session_id, fresh, check_access=False)  # Cancellation/denial cleanup.


def _get_persistable_sources_and_source_chunks(
    state: dict, *, use_uploaded_files: bool, file_extractions_str: str,
):
    """Return provenance only for the file context included in this answer."""
    if not use_uploaded_files or not file_extractions_str or not file_extractions_str.strip():
        return [], []

    last_extraction = state.get('last_extraction') or {}
    if last_extraction.get('file_extractions_str') != file_extractions_str:
        return [], []

    sources = list(last_extraction.get('sources') or [])
    source_chunks = list(last_extraction.get('source_chunks') or [])

    # Legacy compatibility: if only top_chunk is available, convert to one source_chunks entry.
    if not source_chunks:
        top_chunk = last_extraction.get('top_chunk') if isinstance(last_extraction.get('top_chunk'), dict) else {}
        top_uid = str(top_chunk.get('unique_id') or '').strip()
        top_filename = str(top_chunk.get('filename') or '').strip()
        top_content = str(top_chunk.get('content') or '').strip()
        if top_uid and top_filename and top_content:
            source_chunks = [{
                'unique_id': top_uid,
                'filename': top_filename,
                'chunk_index': top_chunk.get('chunk_index'),
                'content': top_content,
            }]

    # Ensure sources includes any file represented in source_chunks.
    for chunk in source_chunks:
        uid = str((chunk or {}).get('unique_id') or '').strip()
        filename = str((chunk or {}).get('filename') or '').strip()
        if uid and filename and not any((s or {}).get('unique_id') == uid for s in sources):
            sources.append({'filename': filename, 'unique_id': uid})

    return sources, source_chunks


def _get_searchable_files(project_id: int, scoped_file_ids):
    """Resolve the current search corpus; an explicit empty selection stays empty."""
    selected_ids = None
    if scoped_file_ids is not None:
        # Invalid selections must not fall through to an unrestricted BM25 search.
        selected_ids = (
            {file_id for file_id in scoped_file_ids if isinstance(file_id, str)}
            if isinstance(scoped_file_ids, list) else set()
        )

    matter_files = []
    seen_ids = set()
    for mf in UploadedFiles.query.filter_by(
        project_id=project_id, draft_id=None, source='search',
    ).all():
        if (
            not isinstance(mf.unique_id, str) or not mf.unique_id
            or mf.unique_id in seen_ids
            or (selected_ids is not None and mf.unique_id not in selected_ids)
        ):
            continue
        seen_ids.add(mf.unique_id)
        matter_files.append({'name': mf.original_filename, 'size': mf.file_size, 'unique_id': mf.unique_id})

    return matter_files, sorted(seen_ids), selected_ids is not None


def _scope_stamp(project_id: int, file_ids: list, file_scope: str) -> dict:
    return {
        'version': SMART_SEARCH_SCOPE_VERSION,
        'project_id': project_id,
        'file_scope': file_scope,
        'file_ids': sorted(set(file_ids)),
    }


def _context_for_current_files(state: dict, project_id: int, file_ids: list) -> str:
    """Do not show the planner context whose corpus cannot be verified."""
    extraction = state.get('last_extraction')
    if not isinstance(extraction, dict):
        state['last_extraction'] = None
        return ''

    context = extraction.get('file_extractions_str')
    stamp = extraction.get('smart_search_scope')
    if (
        not isinstance(context, str) or not context.strip()
        or not isinstance(stamp, dict)
        or stamp.get('version') != SMART_SEARCH_SCOPE_VERSION
        or stamp.get('project_id') != project_id
        or stamp.get('file_ids') != file_ids
        or not isinstance(stamp.get('file_scope'), str)
    ):
        state['last_extraction'] = None
        return ''
    return context


def _stamp_extraction(state: dict, context: str, scope: dict):
    """Attach Doc Search-only provenance after fresh retrieval or a cache hit."""
    if not context or not context.strip():
        state['last_extraction'] = None
        return

    extraction = state.get('last_extraction')
    if not isinstance(extraction, dict) or extraction.get('file_extractions_str') != context:
        extraction = {'file_extractions_str': context}
        state['last_extraction'] = extraction
    extraction['smart_search_scope'] = scope


def persist_turn(session_id: str, user_message: str, llm_response: str,
                 conversation_brief: str, constraints: dict, sources: list = None, source_chunks: list = None):
    """Persist a completed Q&A turn to SQL and update session metadata."""
    check_current_account()
    try:  # Single transaction block: both message rows + session metadata succeed/fail together.
        now_user = datetime.now(timezone.utc)  # UTC timestamp avoids timezone ambiguity across environments.
        db.session.add(SmartSearchMessage(session_id=session_id, role='user', content=user_message, created_at=now_user))  # Insert user turn row.

        metadata_payload = {}  # Optional assistant metadata container (only filled when present).
        if sources is not None:  # Keep empty list [] if explicitly provided; distinguishes from omitted.
            metadata_payload['sources'] = sources  # Persist source provenance for UI/source replay.
        if source_chunks is not None:  # Keep [] only when explicitly provided.
            metadata_payload['source_chunks'] = source_chunks  # Persist best BM25 chunk per file for highlighting.
        metadata = json.dumps(metadata_payload) if metadata_payload else None  # Store compact JSON or SQL NULL.

        now_assistant = datetime.now(timezone.utc)  # Separate timestamp for assistant row/update ordering.
        db.session.add(SmartSearchMessage(
            session_id=session_id,  # Foreign key back to smart search session.
            role='assistant',  # Marks this row as assistant response.
            content=llm_response,  # Full assistant output text.
            metadata_json=metadata,  # Serialized sources/source_chunks metadata if available.
            created_at=now_assistant  # Creation timestamp for chronological replay.
        ))  # Second row in the same transaction.

        session = SmartSearchSession.query.get(session_id)  # Load parent session row for denormalized fields.
        if session:  # Defensive: session may have been deleted concurrently.
            session.updated_at = now_assistant  # Heartbeat timestamp for "last activity" sorting.
            session.conversation_brief = conversation_brief  # Persist latest rolling summary.
            session.constraints_json = json.dumps(constraints)  # Persist constraints dict as JSON string.

        db.session.commit()  # Atomically flush all inserts/updates to durable SQL storage.
        logger.info("[smart_search] Persisted turn to SQL for session %s", session_id)
    except Exception as e:
        db.session.rollback()  # Abort transaction so partial writes are not persisted.
        logger.warning("[smart_search] Failed to persist turn to SQL: %s", e)


@shared_task(bind=True, ignore_result=False)
def smart_search_phase1(self, data: dict) -> dict:
    with account_execution(project_account_id(int(data['project_id']))):
        return _smart_search_phase1(self, data)


def _smart_search_phase1(self, data: dict) -> dict:
    """Phase 1: classify intent and retrieve file extractions."""
    tc = {'total': 0}
    project_id = int(data['project_id'])
    user_id = data.get('user_id')
    user_message = data['query']
    selected_model = ai_base.DEFAULT_MODEL
    session_id = data['session_id']

    state = load_state(session_id)
    chat_history = state['chat_history']

    matter_files, corpus_ids, explicit_scope = _get_searchable_files(
        project_id, data.get('scoped_file_ids'),
    )
    file_extractions_str = _context_for_current_files(state, project_id, corpus_ids)
    save_state(session_id, state)
    if explicit_scope and not corpus_ids:
        return {
            'error': True,
            'response': NO_FILES_IN_SCOPE_ERROR,
            'error_message': NO_FILES_IN_SCOPE_ERROR,
        }

    # Build previous questions context
    previous_2_user_questions_and_responses = ""
    if chat_history:
        if len(chat_history) > 1:
            for prompt, resp in chat_history[-3:-1]:
                resp = resp[-300:]
                previous_2_user_questions_and_responses += f"====\nolder_user_question: {prompt}\nlast part of the response: {resp}\n"
        last_prompt, last_resp = chat_history[-1]
        previous_2_user_questions_and_responses += f"====\nprevious_user_question: {last_prompt}\nlast part of the response: {last_resp}\n"

    self.update_state(state=_status('Classifying intent', tc))

    RETRY_INTENT_ERROR = "Something went wrong in the reasoning process, please try it again."
    try:
        (use_uploaded_files, refresh_context, file_scope, extraction_method,
         _all_uploaded_files_unique_ids, target_file_unique_ids,
         _create_new_draft, _change_draft_requested, _verbatim_file_unique_ids) = classify_intent(
            state,
            user_message,
            current_draft={},
            selected_section='Not Specified',
            matter_files=matter_files,
            previous_chat_files=[],
            last_new_chat_files=[],
            file_extractions_str=file_extractions_str,
            previous_user_questions=previous_2_user_questions_and_responses,
            model=selected_model,
            token_counter=tc,
        )
    except Exception as e:
        add_chat_message(state, (user_message, RETRY_INTENT_ERROR))
        save_state(session_id, state)
        return {
            'error': True,
            'response': RETRY_INTENT_ERROR,
            'error_message': RETRY_INTENT_ERROR,
        }

    logger.info(
        "[smart_search] use_uploaded_files=%s, refresh_context=%s, file_scope=%s, extraction_method=%s",
        use_uploaded_files,
        refresh_context,
        file_scope,
        extraction_method,
    )
    self.update_state(state=_status(
        f'Intent: use_files={use_uploaded_files}, scope={file_scope}, method={extraction_method}', tc))

    # --- File content extraction ---
    scope = _scope_stamp(project_id, corpus_ids, file_scope)
    if state.get('last_extraction') and (
        not isinstance(state['last_extraction'], dict)
        or state['last_extraction'].get('smart_search_scope') != scope
    ):
        state['last_extraction'] = None
        file_extractions_str = ''

    if not corpus_ids:
        use_uploaded_files = False

    if not use_uploaded_files:
        file_extractions_str = ""

    elif not refresh_context and file_extractions_str:
        logger.info(
            "[smart_search] Reusing cached extraction (length: %s)",
            len(file_extractions_str),
        )

    elif extraction_method == 'full_content':
        self.update_state(state=_status('Loading full file content ...', tc))
        target_ids = sorted(set(target_file_unique_ids or []).intersection(corpus_ids))
        chunks = get_full_content_chunks(
            draft_id=None, project_id=project_id,
            file_scope=file_scope, file_unique_ids=target_ids,
        ) if target_ids else []
        file_extractions_str = format_bm25_results(chunks) if chunks else ""
        seen_fc, sources_fc = set(), []
        for c in (chunks or []):
            uid = c.get('source_unique_id', '')
            if uid and uid not in seen_fc:
                seen_fc.add(uid)
                sources_fc.append({'filename': c.get('source_filename', ''), 'unique_id': uid})
        cache_key = f"full_content|{','.join(sorted(target_ids or []))}|scope={file_scope}"
        state.setdefault('extraction_cache', {})[cache_key] = {'file_extractions_str': file_extractions_str}
        state['last_extraction'] = {
            'query': user_message,
            'cache_key': cache_key,
            'file_extractions_str': file_extractions_str,
            'sources': sources_fc,
        }
        _stamp_extraction(state, file_extractions_str, scope)
        save_state(session_id, state)

    else:  # retrieve (BM25)
        self.update_state(state=_status('Searching uploaded files for relevant content ...', tc))
        file_extractions_str = get_relevant_content_bm25(
            user_message, draft_id=None, state=state, top_k=60, model=selected_model,
            project_id=project_id, file_scope=file_scope, file_unique_ids=corpus_ids,
            token_counter=tc,
            reasoning_effort=KEYWORD_REASONING_EFFORT,
        )
        _stamp_extraction(state, file_extractions_str, scope)
        save_state(session_id, state)

    return {
        'session_id': session_id,
        'project_id': project_id,
        'user_id': user_id,
        'user_message': user_message,
        'selected_model': selected_model,
        'extraction_method': extraction_method,
        'use_uploaded_files': use_uploaded_files,
        'file_extractions_str': file_extractions_str,
        'token_total': tc.get('total', 0),
        'error': False,
    }


@shared_task(bind=True, ignore_result=False)
def smart_search_phase2(self, payload: dict) -> dict:
    if payload.get('error'):
        return _smart_search_phase2(self, payload)
    with account_execution(project_account_id(int(payload['project_id']))):
        return _smart_search_phase2(self, payload)


def _smart_search_phase2(self, payload: dict) -> dict:
    """Phase 2: generate answer from file extractions and chat history."""
    if payload.get('error'):
        return {
            'response': payload.get('response', ''),
            'error': True,
            'error_message': payload.get('error_message', 'Something went wrong.'),
        }

    session_id = payload['session_id']
    user_message = payload.get('user_message', '')
    # Ignore model choices in older split-phase payloads; Doc Search follows base.
    selected_model = ai_base.DEFAULT_MODEL
    use_uploaded_files = payload.get('use_uploaded_files', False)
    file_extractions_str = payload.get('file_extractions_str', '')
    tc = {'total': payload.get('token_total', 0)}

    state = load_state(session_id)
    chat_history = state.get('chat_history', [])

    if chat_history:
        chat_history_reduced = get_reduced_chat_history(chat_history)
    else:
        chat_history_reduced = [("No chat history yet.", "No chat history yet.")]

    # Build session context
    relevant_chat_context = retrieve_relevant_chat_turns(
        user_message, chat_history, model=selected_model,
        reasoning_effort=KEYWORD_REASONING_EFFORT,
    )
    brief = state.get('conversation_brief', '')
    constraints = state.get('constraints', {})

    session_context_parts = []
    if brief:
        session_context_parts.append(f"[Conversation Summary]\n{brief}")
    if constraints:
        session_context_parts.append(f"[Session Constraints]\n{json.dumps(constraints, indent=2)}")
    if relevant_chat_context:
        session_context_parts.append(relevant_chat_context)
    session_context_msg = "\n\n".join(session_context_parts) if session_context_parts else None

    self.update_state(state=_status("Answering the user's question ...", tc))

    if use_uploaded_files:
        answer_prompt = f'''As a legal specialist, assist and respond to the user with their question or request:
                {user_message}
                ===
                Relevant context from user uploaded files if any:
                {file_extractions_str}
                ===
                * Format for your response: * Use proper indentation for paragraphs, bold for headers and use italic markdowns for case names, wrap case name with markup <i> and </i>. Do not forget to close the markup.
                Answer in organized professional English with proper markdowns for readability such as bold headers when appropriate, etc.
                Ensure to add [[CASE_START]] right before any case name in your response, this is to mark the beginning of a case name in your response for us to correctly parse and check on them.
                if there is <i> markup, add [[CASE_START]] after the <i> markup.
                Do not wrap document names, file paths, or titles in Markdown inline code (single backticks). Use plain text or bold instead.
                When naming an uploaded document, use the exact path/filename string as it appears in "Relevant context from user uploaded files" above.
                Do not output raw HTML links; the app will attach links automatically when filenames match.
                ===
                '''
    else:
        answer_prompt = f'''As a legal specialist, assist and respond to the user with their question or request:
                {user_message}
                ===
                * Format for your response: * Use proper indentation for paragraphs, bold for headers and use italic markdowns for case names, wrap case name with markup <i> and </i>. Do not forget to close the markup.
                Answer in organized professional English with proper markdowns for readability such as bold headers when appropriate, etc.
                Ensure to add [[CASE_START]] right before any case name in your response, this is to mark the beginning of a case name in your response for us to correctly parse and check on them.
                if there is <i> markup, add [[CASE_START]] after the <i> markup.
                Do not wrap document names, file paths, or titles in Markdown inline code (single backticks). Use plain text or bold instead.
                When referencing a document by name or path, use the exact string as it appears in your sources, quoted materials, or earlier in this conversation when applicable.
                Do not output raw HTML links; the app will attach links automatically when filenames match.
                ===
                '''

    logger.info(
        "[smart_search] answer_prompt tokens: %s",
        num_tokens_from_string(answer_prompt),
    )

    llm_response = multichat_completion_text(
        prev_prompt_output=chat_history_reduced,
        new_prompt=answer_prompt,
        sys_prompt=SYSTEM_PROMPT,
        model=selected_model,
        reasoning_effort=ANSWER_REASONING_EFFORT,
        context_message=session_context_msg,
        token_counter=tc,
    )
    logger.info("[smart_search] llm_response: %s", llm_response)

    if not llm_response:
        llm_response = ""

    # No citation annotation for smart_search — return raw response
    add_chat_message(state, (user_message, llm_response))
    state['conversation_brief'], state['constraints'] = update_conversation_brief(
        state.get('conversation_brief', ''),
        state.get('constraints', {}),
        user_message, llm_response,
        model=selected_model,
        token_counter=tc,
    )
    save_state(session_id, state)
    sources, source_chunks = _get_persistable_sources_and_source_chunks(
        state=state,
        use_uploaded_files=use_uploaded_files,
        file_extractions_str=file_extractions_str,
    )
    persist_turn(
        session_id,
        user_message,
        llm_response,
        state['conversation_brief'],
        state['constraints'],
        sources=sources if use_uploaded_files else None,
        source_chunks=source_chunks if use_uploaded_files else None,
    )
    return {
        'response': llm_response.replace("[[CASE_START]]", " "),
        'session_id': session_id,
        'sources': sources if use_uploaded_files else [],
        'source_chunks': source_chunks if use_uploaded_files else [],
    }


@shared_task(bind=True, ignore_result=False)
def smart_search_task(self, data: dict) -> dict:
    try:
        with account_execution(project_account_id(int(data['project_id']))):
            return _smart_search_task(self, data)
    except AccountUnavailable:
        _clear_active_task(data['session_id'], self.request.id)
        raise


def _smart_search_task(self, data: dict) -> dict:
    """Single combined task: file extraction (phase1) + LLM answer (phase2).
    Returns a single task ID so the frontend can poll progress from start to finish.
    """
    tc = {'total': 0}
    project_id = int(data['project_id'])
    user_id = data.get('user_id')
    user_message = data['query']
    selected_model = ai_base.DEFAULT_MODEL
    session_id = data['session_id']
    scoped_file_ids = data.get('scoped_file_ids')  # None means all files; [] is an explicit empty scope.

    state = load_state(session_id)
    state['active_task_id'] = self.request.id
    save_state(session_id, state)
    chat_history = state['chat_history']

    matter_files, corpus_ids, explicit_scope = _get_searchable_files(project_id, scoped_file_ids)
    file_extractions_str = _context_for_current_files(state, project_id, corpus_ids)
    save_state(session_id, state)

    previous_2_user_questions_and_responses = ""
    if chat_history:
        if len(chat_history) > 1:
            for prompt, resp in chat_history[-3:-1]:
                resp = resp[-300:]
                previous_2_user_questions_and_responses += f"====\nolder_user_question: {prompt}\nlast part of the response: {resp}\n"
        last_prompt, last_resp = chat_history[-1]
        previous_2_user_questions_and_responses += f"====\nprevious_user_question: {last_prompt}\nlast part of the response: {last_resp}\n"

    try:
        if explicit_scope and not corpus_ids:
            return {
                'error': True,
                'response': NO_FILES_IN_SCOPE_ERROR,
                'error_message': NO_FILES_IN_SCOPE_ERROR,
            }

        self.update_state(state=_status('Classifying intent', tc))

        RETRY_INTENT_ERROR = "Something went wrong in the reasoning process, please try it again."
        try:
            (use_uploaded_files, refresh_context, file_scope, extraction_method,
             _all_uploaded_files_unique_ids, target_file_unique_ids,
             _create_new_draft, _change_draft_requested, _verbatim_file_unique_ids) = classify_intent(
                state,
                user_message,
                current_draft={},
                selected_section='Not Specified',
                matter_files=matter_files,
                previous_chat_files=[],
                last_new_chat_files=[],
                file_extractions_str=file_extractions_str,
                previous_user_questions=previous_2_user_questions_and_responses,
                model=selected_model,
                token_counter=tc,
            )
        except Exception as e:
            add_chat_message(state, (user_message, RETRY_INTENT_ERROR))
            save_state(session_id, state)
            return {'error': True, 'response': RETRY_INTENT_ERROR, 'error_message': RETRY_INTENT_ERROR}

        # Preserve the selected-file full-content behavior, using only current files.
        if explicit_scope:
            target_file_unique_ids = corpus_ids

        self.update_state(state=_status(
            f'Intent: use_files={use_uploaded_files}, scope={file_scope}, method={extraction_method}', tc))

        scope = _scope_stamp(project_id, corpus_ids, file_scope)
        if state.get('last_extraction') and (
            not isinstance(state['last_extraction'], dict)
            or state['last_extraction'].get('smart_search_scope') != scope
        ):
            state['last_extraction'] = None
            file_extractions_str = ''

        if not corpus_ids:
            use_uploaded_files = False

        if not use_uploaded_files:
            file_extractions_str = ""
        elif not refresh_context and file_extractions_str:
            logger.info(
                "[smart_search] Reusing cached extraction (length: %s)",
                len(file_extractions_str),
            )
        elif extraction_method == 'full_content':
            self.update_state(state=_status('Loading full file content ...', tc))
            target_ids = sorted(set(target_file_unique_ids or []).intersection(corpus_ids))
            chunks = get_full_content_chunks(
                draft_id=None, project_id=project_id,
                file_scope=file_scope, file_unique_ids=target_ids,
            ) if target_ids else []
            file_extractions_str = format_bm25_results(chunks) if chunks else ""
            seen_fc, sources_fc = set(), []
            for c in (chunks or []):
                uid = c.get('source_unique_id', '')
                if uid and uid not in seen_fc:
                    seen_fc.add(uid)
                    sources_fc.append({'filename': c.get('source_filename', ''), 'unique_id': uid})
            cache_key = f"full_content|{','.join(sorted(target_ids or []))}|scope={file_scope}"
            state.setdefault('extraction_cache', {})[cache_key] = {'file_extractions_str': file_extractions_str, 'sources': sources_fc}
            state['last_extraction'] = {
                'query': user_message,
                'cache_key': cache_key,
                'file_extractions_str': file_extractions_str,
                'sources': sources_fc,
            }
            _stamp_extraction(state, file_extractions_str, scope)
            save_state(session_id, state)
        else:
            self.update_state(state=_status('Searching uploaded files for relevant content ...', tc))
            file_extractions_str = get_relevant_content_bm25(
                user_message, draft_id=None, state=state, top_k=60, model=selected_model,
                project_id=project_id, file_scope=file_scope, file_unique_ids=corpus_ids,
                token_counter=tc,
                reasoning_effort=KEYWORD_REASONING_EFFORT,
            )
            _stamp_extraction(state, file_extractions_str, scope)
            save_state(session_id, state)

        if _is_cancelled(session_id, self.request.id):
            return {'response': '', 'session_id': session_id, 'sources': [], 'cancelled': True}

        # --- Phase 2: LLM answer ---
        if chat_history:
            chat_history_reduced = get_reduced_chat_history(chat_history)
        else:
            chat_history_reduced = [("No chat history yet.", "No chat history yet.")]

        relevant_chat_context = retrieve_relevant_chat_turns(
            user_message, chat_history, model=selected_model,
            reasoning_effort=KEYWORD_REASONING_EFFORT,
        )
        brief = state.get('conversation_brief', '')
        constraints = state.get('constraints', {})

        session_context_parts = []
        if brief:
            session_context_parts.append(f"[Conversation Summary]\n{brief}")
        if constraints:
            session_context_parts.append(f"[Session Constraints]\n{json.dumps(constraints, indent=2)}")
        if relevant_chat_context:
            session_context_parts.append(relevant_chat_context)
        session_context_msg = "\n\n".join(session_context_parts) if session_context_parts else None

        self.update_state(state=_status("Answering the user's question ...", tc))

        if use_uploaded_files:
            answer_prompt = f'''As a legal specialist, assist and respond to the user with their question or request:
                    {user_message}
                    ===
                    Relevant context from user uploaded files if any:
                    {file_extractions_str}
                    ===
                    * Format for your response: * Use proper indentation for paragraphs, bold for headers and use italic markdowns for case names, wrap case name with markup <i> and </i>. Do not forget to close the markup.
                    Answer in organized professional English with proper markdowns for readability such as bold headers when appropriate, etc.
                    Ensure to add [[CASE_START]] right before any case name in your response, this is to mark the beginning of a case name in your response for us to correctly parse and check on them.
                    if there is <i> markup, add [[CASE_START]] after the <i> markup.
                    Do not wrap document names, file paths, or titles in Markdown inline code (single backticks). Use plain text or bold instead.
                    When naming an uploaded document, use the exact path/filename string as it appears in "Relevant context from user uploaded files" above.
                    Do not output raw HTML links; the app will attach links automatically when filenames match.
                    ===
                    '''
        else:
            answer_prompt = f'''As a legal specialist, assist and respond to the user with their question or request:
                    {user_message}
                    ===
                    * Format for your response: * Use proper indentation for paragraphs, bold for headers and use italic markdowns for case names, wrap case name with markup <i> and </i>. Do not forget to close the markup.
                    Answer in organized professional English with proper markdowns for readability such as bold headers when appropriate, etc.
                    Ensure to add [[CASE_START]] right before any case name in your response, this is to mark the beginning of a case name in your response for us to correctly parse and check on them.
                    if there is <i> markup, add [[CASE_START]] after the <i> markup.
                    Do not wrap document names, file paths, or titles in Markdown inline code (single backticks). Use plain text or bold instead.
                    When referencing a document by name or path, use the exact string as it appears in your sources, quoted materials, or earlier in this conversation when applicable.
                    Do not output raw HTML links; the app will attach links automatically when filenames match.
                    ===
                    '''

        llm_response = multichat_completion_text(
            prev_prompt_output=chat_history_reduced,
            new_prompt=answer_prompt,
            sys_prompt=SYSTEM_PROMPT,
            model=selected_model,
            reasoning_effort=ANSWER_REASONING_EFFORT,
            context_message=session_context_msg,
            token_counter=tc,
        )
        if not llm_response:
            llm_response = ""

        if _is_cancelled(session_id, self.request.id):
            return {'response': '', 'session_id': session_id, 'sources': [], 'cancelled': True}

        add_chat_message(state, (user_message, llm_response))
        state['conversation_brief'], state['constraints'] = update_conversation_brief(
            state.get('conversation_brief', ''),
            state.get('constraints', {}),
            user_message, llm_response,
            model=selected_model,
            token_counter=tc,
        )
        save_state(session_id, state)
        sources, source_chunks = _get_persistable_sources_and_source_chunks(
            state=state,
            use_uploaded_files=use_uploaded_files,
            file_extractions_str=file_extractions_str,
        )
        persist_turn(
            session_id,
            user_message,
            llm_response,
            state['conversation_brief'],
            state['constraints'],
            sources=sources if use_uploaded_files else None,
            source_chunks=source_chunks if use_uploaded_files else None,
        )
        return {
            'response': llm_response.replace("[[CASE_START]]", " "),
            'session_id': session_id,
            'sources': sources if use_uploaded_files else [],
            'source_chunks': source_chunks if use_uploaded_files else [],
        }
    finally:
        _clear_active_task(session_id, self.request.id)
