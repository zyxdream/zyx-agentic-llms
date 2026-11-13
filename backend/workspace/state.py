"""Extracted, application-owned helpers for the Workspace agent."""
from backend.ai.model_policy import resolve_assistant_mode
from backend.ai.model_policy import _assistant_model, _output_effort

from celery import shared_task

from celery.exceptions import SoftTimeLimitExceeded

import re

import json

from backend.util.db import DBMemory

from backend.database import db

from backend.ai import base as ai_base
from backend.ai.base import chat_completion_parse

from backend.ai.workspace_schemas import BriefUpdateResult

from backend.models import ChatThread

import uuid

from backend.domains.legal.free_law_tools import annotate_text_with_citations_multiple, extract_validated_cases_from_citations, verify_citations_with_lookup_tags

from backend.util.task_analytics import LLM_USAGE_CHAT, increment_task_analytics, sync_task_analytics_draft_title, task_llm_usage_scope

WORKFLOW_STATUSES = {"running", "awaiting_user_confirmation", "completed", "failed", "cancelled"}


WORKFLOW_PHASES = {"agent", "confirm", "finalize"}


def create_empty_state():
    """Create the canonical empty JSON state for an Workspace conversation.
    Used by `load_state_from_memory` and shared smart-search initialization whenever a draft has no persisted state or the stored state is unusable.
    """
    return {
        'status':'',  # this is a string to indicate the status of the state, for example, 'drafting', 'summarizing', 'analyzing', etc.

        'draft': {
            'title': '',
            'sections': [],

        },
        'selected_section': 'Not Specified',
        'chat_history': [],
        'chat_history_meta': [],
        'uploaded_files': {},  # {unique_id: {raw_content, summary, extractions: {...}}}
        'conversation_brief': '',
        'constraints': {},
        'validated_cases': {},  # {citation_str: {cluster_id, case_name, llm_holding, ...}}
    }


def load_state_from_memory(memory):
    """Load the JSON workspace state from `DBMemory`, repairing missing keys as needed.

    Used by upload tasks, route helpers, Workspace routes and agent operations whenever persisted draft state is needed.
    """
    if 'state' in memory:
        try:
            state = json.loads(memory['state'])

            # Ensure basic keys exist with simple defaults
            if 'draft' not in state or not isinstance(state['draft'], dict):
                state['draft'] = {
                    'title': '',
                    'sections': [],
                }
            if 'selected_section' not in state:
                state['selected_section'] = 'Not Specified'
            if 'chat_history' not in state:
                state['chat_history'] = []
            if 'chat_history_meta' not in state or not isinstance(state['chat_history_meta'], list):
                state['chat_history_meta'] = []
            if len(state['chat_history_meta']) < len(state['chat_history']):
                missing_count = len(state['chat_history']) - len(state['chat_history_meta'])
                state['chat_history_meta'].extend({} for _ in range(missing_count))
            elif len(state['chat_history_meta']) > len(state['chat_history']):
                state['chat_history_meta'] = state['chat_history_meta'][:len(state['chat_history'])]
            for turn_meta in state['chat_history_meta']:
                turn_meta.setdefault('conversation_turn_id', uuid.uuid4().hex)
            if 'uploaded_files' not in state:
                state['uploaded_files'] = {}
            if 'conversation_brief' not in state:
                state['conversation_brief'] = ''
            if 'constraints' not in state:
                state['constraints'] = {}
            if 'validated_cases' not in state:
                state['validated_cases'] = {}

            sections_count = len(state.get('draft', {}).get('sections', []))
            chat_count = len(state.get('chat_history', []))
            print(f"STATE: Loaded from memory (sections: {sections_count}, chat: {chat_count})")
            return state
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            print(f"STATE: Corrupted state in memory, creating new state (error: {e})")
            return create_empty_state()
    else:
        print("STATE: No state in memory, creating new state")
        return create_empty_state()


class OperationMemory(dict):
    """Private in-memory state for an operation; has no database write methods."""


def save_state_to_memory(state, memory, *, auxiliary_fields=()):
    """Persist explicit draft/turn commands and only the caller's named auxiliary fields.

    SQL workspace reads are assembled projections, never replacement snapshots.
    Private operation and Smart Search memories retain their existing behavior.
    """
    if not isinstance(memory, DBMemory):
        memory['state'] = json.dumps(state)
        return state
    try:
        _save_workspace_changes(state, memory, auxiliary_fields)
    except Exception:
        # Legacy callers may catch a failed publication and save failure status.
        # Discard all staged draft/turn writes before that separate transaction.
        db.session.rollback()
        raise
    draft = state.get('draft') or {}
    sections_count = len(draft.get('sections') or []) if isinstance(draft, dict) else 0
    chat_count = len(state.get('chat_history', []))
    state_size = len(json.dumps(state))
    normalized_title = str(draft.get('title') or '').strip() if isinstance(draft, dict) else ''
    analytics_title = None
    if has_substantive_workspace_draft(draft):
        analytics_title = normalized_title or 'Untitled document'
    sync_task_analytics_draft_title(memory.doc_id, analytics_title)
    print(f"STATE: Saved to memory (sections: {sections_count}, chat: {chat_count}, size: {state_size} bytes)")
    return state


def _save_workspace_changes(state, memory, auxiliary_fields):
    """Commit explicit legacy publication commands together, never a state snapshot."""
    from backend.util import workspace_store, workspace_service as workflow
    from backend.util.account_access import chat_account_id, require_enabled_account
    # The chat owns turn ordering. Match admission/publication lock order before
    # locking a workflow or its document, so parallel writers cannot deadlock.
    ChatThread.query.filter_by(id=memory.doc_id).with_for_update().one()
    current = workspace_store.load(memory.doc_id)
    document_id = state.get('draft_document_id') or current['draft_document_id']
    pending_turns = state.get('_workspace_turns', [])
    write_draft = state.get('_workspace_draft_write', False)
    # Incidental draft copies carried by metadata/answer-only writers have no
    # authority to change the draft or its publication hash.
    prospective = state if write_draft else {
        **state, 'draft': current['draft']}
    workflow.guard_chat_write(prospective, memory)
    if write_draft:
        require_enabled_account(chat_account_id(memory.doc_id))
        options = {'draft_before': state['draft_before']} if 'draft_before' in state else {}
        state['draft_version'] = workspace_store.update_document(memory.doc_id, document_id, state['draft'],
            expected_version=state['draft_version'], **options)
    execution = workflow.current_execution()
    published = None
    for pending in pending_turns:
        meta = dict(pending['meta'])
        # Failed/cancelled saved requests are workflow overlays, not completed turns.
        if meta.get('workflow_failed') or meta.get('workflow_cancelled'):
            continue
        if (execution or meta.get('run_id')) and (state.get('active_workflow') or {}).get('status') != 'completed':
            continue
        require_enabled_account(chat_account_id(memory.doc_id))
        if execution:
            meta.setdefault('run_id', execution['id'])
        meta.update(chat_thread_id=current['chat_thread_id'], draft_document_id=document_id)
        published = workspace_store.append_turn(memory.doc_id, pending['turn'][0], pending['turn'][1],
            meta=meta, workflow_id=meta.get('run_id'), turn_id=meta.get('conversation_turn_id'))
    if auxiliary_fields:
        workspace_store.patch_auxiliary(memory.doc_id,
            {key: state[key] for key in auxiliary_fields if key in state},
            remove=tuple(key for key in auxiliary_fields if key not in state))
    if execution and published is not None and (state.get('active_workflow') or {}).get('status') == 'completed':
        row = workflow.validate_execution(lock=True)
        workflow.stage_publication(row, published.assistant_text, workspace_store.load(memory.doc_id)['draft'])
    db.session.commit()
    state.pop('_workspace_turns', None)
    state.pop('_workspace_draft_write', None)
    state.pop('_history_to_archive', None)


def merge_validated_cases(state: dict, new_cases: dict) -> None:
    """Merge newly validated citation records into `state['validated_cases']`.

    Called after citation annotation in answer, draft, edit, and revise branches so later guard checks can reuse CourtListener validation and enriched holdings.
    """
    vc = state.setdefault('validated_cases', {})
    for key, data in new_cases.items():
        existing = vc.get(key, {})
        merged = dict(data)
        # Keep non-None enriched fields (llm_holding, llm_hist, etc.) already saved by case view
        for f in ('llm_holding', 'llm_hist', 'court', 'freelaw_url'):
            if existing.get(f) is not None:
                merged[f] = existing[f]
        vc[key] = merged


def verify_and_annotate_response_citations(
    response_text: str,
    state: dict,
    case_viewer_prefix: str | None,
) -> tuple[str, list[dict], list[dict]]:
    """Verify response citations once, annotate linkable cites, and cache verified cases."""
    cite_info, lookup_diagnostics = verify_citations_with_lookup_tags(response_text or "")
    print("cite_info: ", cite_info)
    if cite_info:
        response_text = annotate_text_with_citations_multiple(
            response_text,
            cite_info,
            case_viewer_prefix=case_viewer_prefix,
        )
        merge_validated_cases(state, extract_validated_cases_from_citations(cite_info))
        print("merged validated cases, current keys of state[validated_cases] are: ", state.get('validated_cases', {}).keys())
    return response_text, cite_info, lookup_diagnostics


CONSTRAINTS_SCHEMA = {
    # ------ Global/session-wide constraints ------
    "tone":                 {"type": str, "max_len": 200},
    "exclusions":           {"type": list, "max_items": 20, "item_max_len": 200},
    "special_instructions": {"type": str, "max_len": 500},
    # ------ Per-case constraints, keyed by short case label ------
    "cases": {"type": dict, "max_items": 5},
}


CASE_FIELDS_SCHEMA = {
    # ------ Fields allowed inside each constraints["cases"][case_label] ------
    "jurisdiction":  {"type": str, "max_len": 200},
    "court":         {"type": str, "max_len": 200},
    "case_type":     {"type": str, "max_len": 200},
    "party_names":   {"type": list, "max_items": 20, "item_max_len": 200},
    "key_exhibits":  {"type": list, "max_items": 20, "item_max_len": 200},
    "deadlines":     {"type": list, "max_items": 20, "item_max_len": 200},
}


def sanitize_constraints(raw: dict) -> dict:
    """Validate and bound the durable user constraints extracted from the conversation brief.

    Used by `update_conversation_brief` before saving constraints back into session state, with support for global preferences and per-case fields.
    """
    if not isinstance(raw, dict):
        return {}
    sanitized = {}

    # Global fields
    for key in ("tone", "exclusions", "special_instructions"):
        if key not in raw:
            continue
        spec = CONSTRAINTS_SCHEMA[key]
        val = raw[key]
        if spec["type"] is str:
            sanitized[key] = str(val)[:spec["max_len"]] if val else ""
        elif spec["type"] is list:
            if not isinstance(val, list):
                val = [val] if isinstance(val, str) else []
            sanitized[key] = [str(i)[:spec["item_max_len"]] for i in val[:spec["max_items"]]]

    # Per-case fields
    raw_cases = raw.get("cases", {})
    if isinstance(raw_cases, dict):
        sanitized_cases = {}
        for case_label, case_data in list(raw_cases.items())[:5]:  # max 5 cases
            if not isinstance(case_data, dict):
                continue
            sanitized_case = {}
            for field, spec in CASE_FIELDS_SCHEMA.items():
                if field not in case_data:
                    continue
                val = case_data[field]
                if spec["type"] is str:
                    sanitized_case[field] = str(val)[:spec["max_len"]] if val else ""
                elif spec["type"] is list:
                    if not isinstance(val, list):
                        val = [val] if isinstance(val, str) else []
                    sanitized_case[field] = [str(i)[:spec["item_max_len"]] for i in val[:spec["max_items"]]]
            if sanitized_case:
                sanitized_cases[str(case_label)[:100]] = sanitized_case
        if sanitized_cases:
            sanitized["cases"] = sanitized_cases

    return sanitized


BRIEF_UPDATE_PROMPT = """You maintain a running conversation brief for a legal assistant session.
Given the CURRENT BRIEF and the LATEST EXCHANGE, produce an UPDATED BRIEF.

Rules:
- Keep the brief under 1500 tokens
- Preserve all durable constraints (jurisdiction, tone, parties, exhibit references, deadlines)
- Summarize older context; keep recent decisions in more detail
- Drop transient details (greetings, acknowledgments, routine confirmations)
- CRITICAL: If the conversation involves multiple legal cases or matters, keep their facts strictly separate in both the brief and the constraints. Never merge parties, exhibits, deadlines, or jurisdiction from one case into another.

Extract durable constraints into the structure below.

Global keys (use null or [] if no value):
- tone (string or null): writing tone preference
- exclusions (list of strings): topics/items to exclude
- special_instructions (string or null): other persistent user instructions

Per-case keys — return "cases" as an array of objects, each with a short case_label
(e.g. "Smith v. Jones"). Use null or [] for unknown fields:
- jurisdiction (string): state, country, or court system
- court (string): specific court
- case_type (string): type of legal matter
- party_names (list of strings): parties involved in THIS case only
- key_exhibits (list of strings): exhibits referenced for THIS case only
- deadlines (list of strings): dates/deadlines for THIS case only

If only one case is discussed, still use the "cases" array with one entry.

Return JSON: {"brief": "...", "constraints": {"tone": null, "exclusions": [], "special_instructions": null, "cases": [{"case_label": "Smith v. Jones", "jurisdiction": "...", "court": null, "case_type": null, "party_names": ["..."], "key_exhibits": [], "deadlines": []}]}}"""


def update_conversation_brief(current_brief: str, constraints: dict,
                               user_message: str, assistant_response: str,
                               model: str = ai_base.DEFAULT_MODEL, token_counter=None,
                               reasoning_effort: str | None = None, require_complete: bool = False) -> tuple[str, dict]:
    """Ask the LLM to update the rolling session brief and durable constraints.

    Phase 2 calls this after answer, create, edit, and revise branches, and smart search imports it for the same post-turn memory maintenance.
    """
    from backend.util.conversation_context import compact
    from backend.util.preparation_summary import token_count
    if token_count(user_message + assistant_response) > 24000:
        assistant_response = compact(f'User: {user_message}\nAssistant: {assistant_response}',
                                     8000, model=model, token_counter=token_counter)
        user_message = '(The complete exchange is compacted below.)'
    current_brief = compact(current_brief, 3900, model=model, token_counter=token_counter)
    constraints_str = compact(json.dumps(constraints) if constraints else "{}", 3900,
                              model=model, token_counter=token_counter)
    prompt = f"""{BRIEF_UPDATE_PROMPT}

CURRENT BRIEF:
{current_brief if current_brief else "(empty — first turn)"}

CURRENT CONSTRAINTS:
{constraints_str}

LATEST EXCHANGE:
User: {user_message}
Assistant: {assistant_response}"""

    from backend.util.prepared_retrieval import request_tokens
    if request_tokens(prompt, BriefUpdateResult) > 40000:
        raise ValueError("Conversation brief input exceeds its token budget")
    result = chat_completion_parse(
        prompt,
        BriefUpdateResult,
        model=model,
        reasoning_effort=reasoning_effort,
        sys_prompt="You maintain a conversation brief.",
        token_counter=token_counter, max_output_tokens=32768, raise_on_incomplete=True,
    )
    if result is None:
        if require_complete:
            raise ValueError('Conversation brief update returned no complete result')
        return current_brief, constraints
    new_brief = result.brief.strip()
    if not new_brief or token_count(new_brief) > 3900:
        if require_complete:
            raise ValueError('Conversation brief was empty or exceeded its token budget')
        return current_brief, constraints
    raw_constraints = result.constraints.model_dump(exclude_none=True)
    cases = {}
    for case in result.constraints.cases:
        label = case.case_label.strip()
        if not label or label in cases:
            continue
        case_data = case.model_dump(exclude={"case_label"}, exclude_none=True)
        cases[label] = {key: value for key, value in case_data.items() if value not in ("", [])}
    raw_constraints["cases"] = cases
    raw_constraints = {key: value for key, value in raw_constraints.items() if value not in ("", [], {})}
    if not raw_constraints:
        raw_constraints = constraints
    new_constraints = sanitize_constraints(raw_constraints)
    return new_brief, new_constraints


def _run_brief_update(data: dict) -> None:
    """Refresh the conversation brief/constraints without clobbering newer state.

    Extracted from the Celery task so it is unit-testable. Updates only brief/constraints;
    the draft was already persisted by phase 2. The brief LLM may run while another request
    persists a newer draft/chat snapshot, so reload immediately before saving and merge only
    the brief fields onto the latest state.
    """
    doc_id = int(data['doc_id'])
    from backend.util.account_access import account_execution, chat_account_id, require_enabled_account
    owner_id = chat_account_id(doc_id)
    require_enabled_account(owner_id)
    assistant_mode = resolve_assistant_mode(data)
    memory = DBMemory(db=db, doc_id=doc_id)
    state = load_state_from_memory(memory)
    from backend.util.conversation_context import available_turns, conversation_turn
    baseline = (state.get('conversation_brief_through'), state.get('conversation_brief', ''), state.get('constraints', {}))
    durable = getattr(memory, 'doc_id', None) is not None
    records = available_turns(state, doc_id if durable else None)
    ids = [record['id'] for record in records]
    requested_turn = data.get('run_id')
    target = next((i for i in range(len(records) - 1, -1, -1)
                   if (records[i]['id'] == requested_turn if requested_turn else
                       records[i]['turn'] == [data.get('user_message', ''), _clean_llm_response(data.get('assistant_response', ''))])), None)
    if durable and target is None:
        return  # Obsolete/unmatched messages cannot be called the latest exchange.
    covered = ids.index(baseline[0]) + 1 if baseline[0] in ids else 0
    if target is not None and target < covered:
        return  # A delayed background message must not move coverage backward.
    pending = [conversation_turn(record) for record in records[covered:target + 1]] if target is not None else [
        (data.get('user_message', ''), data.get('assistant_response', ''))]
    if durable:
        db.session.commit()  # Never keep the state read transaction over provider I/O.
    new_brief, new_constraints = baseline[1], baseline[2]
    if len(pending) > 1:
        # Catch up several short exchanges in one call, not one call per turn.
        from backend.util.source_context import windows
        text = '\n\n'.join(f'User: {q}\nAssistant: {a}' for q, a in pending)
        pending = [('Update from these consecutive complete exchanges:', text[p['start']:p['end']])
                   for p in windows(text, 0, len(text), 24000)]
    for question, answer in pending:
        with account_execution(owner_id):
            new_brief, new_constraints = update_conversation_brief(
                new_brief, new_constraints, question, answer,
                model=_assistant_model(assistant_mode), reasoning_effort="medium", require_complete=True)
    latest_state = load_state_from_memory(memory)
    if (latest_state.get('conversation_brief_through'), latest_state.get('conversation_brief', ''),
            latest_state.get('constraints', {})) != baseline:
        return  # Another update won; leave uncovered exchanges for the next update/request.
    latest_state['conversation_brief'] = new_brief
    latest_state['constraints'] = new_constraints
    if target is not None:
        latest_state['conversation_brief_through'] = ids[target]
    if durable:
        from backend.models import TextEntry
        # Lock only auxiliary metadata; draft and turns have their own stores.
        # A new workspace need not have a legacy state row yet.
        entry = TextEntry.query.filter_by(chat_thread_id=doc_id, textName='state').populate_existing().with_for_update().first()
        if entry is None:
            from backend.util.workspace_store import patch_auxiliary
            patch_auxiliary(doc_id, {})
            entry = TextEntry.query.filter_by(chat_thread_id=doc_id, textName='state').one()
        original = entry.content
        actual = json.loads(original)
        from backend.util.workspace_store import CANONICAL_KEYS
        actual = {key: value for key, value in actual.items() if key not in CANONICAL_KEYS}
        if (actual.get('conversation_brief_through'), actual.get('conversation_brief', ''), actual.get('constraints', {})) != baseline:
            return
        actual['conversation_brief'], actual['constraints'] = new_brief, new_constraints
        if target is not None:
            actual['conversation_brief_through'] = ids[target]
        require_enabled_account(owner_id)
        db.session.query(TextEntry).filter(TextEntry.id == entry.id, TextEntry.content == original).update(
            {'content': json.dumps(actual)}, synchronize_session=False)
        db.session.commit()
    else:
        require_enabled_account(owner_id)
        save_state_to_memory(latest_state, memory)


@shared_task(bind=True, ignore_result=True)
def update_conversation_brief_task(self, data: dict) -> None:
    """Background, post-turn maintenance of the rolling conversation brief/constraints.

    Deferred from phase 2 (via `_enqueue_brief_update`) so the brief's LLM call is off the
    response critical path.
    """
    try:
        doc_id = int(data['doc_id'])
        with task_llm_usage_scope(doc_id, LLM_USAGE_CHAT):
            _run_brief_update(data)
    except Exception as e:
        db.session.rollback()
        print(f"update_conversation_brief_task failed: {e}")


def _enqueue_brief_update(doc_id, user_message: str, llm_response: str, error_flag: bool = False,
                          *, assistant_mode: str = "deep") -> None:
    """Fire-and-forget the post-turn conversation-brief update (see update_conversation_brief_task)."""
    if error_flag or not llm_response:
        return
    try:
        update = {
            'doc_id': int(doc_id),
            'user_message': user_message,
            'assistant_response': llm_response,
            'assistant_mode': assistant_mode,
        }
        from backend.util.workspace_service import current_execution
        context = current_execution()
        if context:
            update['run_id'] = context['id']
        update_conversation_brief_task.apply_async(args=[update])
    except SoftTimeLimitExceeded:
        raise
    except Exception as e:
        print(f"failed to enqueue brief update: {e}")


def create_active_workflow(run_id: str, user_message: str) -> dict:
    """Create the per-turn workflow record stored under `state['active_workflow']`.

    Used when phase 1 starts or when `update_active_workflow` needs to repair missing workflow state; the workflow id tracks the logical user turn separately from Celery task ids.
    """
    return {
        "run_id": run_id,
        "status": "running",
        "phase": "classify",
        "user_message": user_message,
        "draft_action": "answer_only",
        "revision_mode": "general_revision",
        "file_selection": {
            "selected_files": [],
            "primary_draft_file_id": None,
        },
        "context_policy": "none",
        "pending_confirmation": None,
        "pending_turn": None,
        "phase1_payload": None,
    }


def update_active_workflow(state: dict, *, status: str | None = None, phase: str | None = None, **updates) -> dict:
    """Update `state['active_workflow']` in place while clamping status and phase values.

    Called at every major phase transition in phase 1, phase 2, and confirmation resumes so the frontend can show progress and pending-confirmation state.
    """
    workflow = state.get("active_workflow")
    if not isinstance(workflow, dict):
        workflow = create_active_workflow(f"wf_{uuid.uuid4().hex}", "")
        state["active_workflow"] = workflow
    if status in WORKFLOW_STATUSES:
        workflow["status"] = status
    if phase in WORKFLOW_PHASES:
        workflow["phase"] = phase
    for key, value in updates.items():
        workflow[key] = value
    return workflow


def _normalize_prompt_turn_meta(raw_prompt_turn_meta):
    """Normalize prompt-attachment metadata into `{'prompt_attachments': [...]}`.

    Phase 1 uses this on the frontend payload, phase 2 uses it again on the phase-1 payload, and `add_chat_message` stores the result with the saved chat turn.
    """
    raw_clarification = None
    document_scope = None
    if isinstance(raw_prompt_turn_meta, dict):
        raw_prompt_attachments = raw_prompt_turn_meta.get(
            'prompt_attachments',
            raw_prompt_turn_meta.get('promptAttachments', []),
        )
        raw_clarification = raw_prompt_turn_meta.get('clarification')
        document_scope = raw_prompt_turn_meta.get('document_scope')
    elif isinstance(raw_prompt_turn_meta, list):
        raw_prompt_attachments = raw_prompt_turn_meta
    else:
        raw_prompt_attachments = []

    normalized_prompt_attachments = []
    if isinstance(raw_prompt_attachments, list):
        for attachment in raw_prompt_attachments:
            if not isinstance(attachment, dict):
                continue
            file_id_raw = attachment.get('file_id') or attachment.get('fileId')
            file_name_raw = attachment.get('file_name') or attachment.get('fileName') or attachment.get('filename') or attachment.get('name')
            if not file_id_raw and not file_name_raw:
                continue
            file_id = str(file_id_raw) if file_id_raw else ''
            file_name = str(file_name_raw) if file_name_raw else (file_id or '')
            normalized_prompt_attachments.append({
                'file_id': file_id,
                'file_name': file_name,
                **{key: attachment[key] for key in ('artifact_id', 'headline') if isinstance(attachment.get(key), str)},
            })
    normalized_meta = {'prompt_attachments': normalized_prompt_attachments}
    if isinstance(raw_prompt_turn_meta, dict):
        for key in ('run_id', 'extraction_report'):
            if isinstance(raw_prompt_turn_meta.get(key), str):
                normalized_meta[key] = raw_prompt_turn_meta[key]
    if isinstance(document_scope, dict) and document_scope.get('version') in (1, 2):
        normalized_meta['document_scope'] = document_scope
    if isinstance(raw_clarification, dict) and raw_clarification:
        normalized_meta['clarification'] = {
            key: value
            for key, value in raw_clarification.items()
            if value not in (None, "")
        }
    return normalized_meta


def has_substantive_workspace_draft(current_draft: dict | None) -> bool:
    """Return true only when the workspace has a real structured draft.

    Used by intent classification and confirmation building to distinguish an editable right-side draft from uploaded documents or the default empty state.
    """
    if not isinstance(current_draft, dict):
        return False
    if str(current_draft.get("markdown") or "").strip():
        return True
    if str(current_draft.get("title") or "").strip():
        return True
    sections = current_draft.get("sections", [])
    if not isinstance(sections, list):
        return False
    return any(
        str(section.get("section_name") or "").strip()
        or str(section.get("section_content") or "").strip()
        for section in sections
        if isinstance(section, dict)
    )


def _balance_italic_tags(text: str) -> str:
    """Drop unmatched </i> and close any unclosed <i> so case-name italics stay balanced.

    The LLM occasionally emits a stray or doubled </i> (or omits the opening <i>).
    The chat frontend renders raw HTML (marked with sanitize:false, no DOMPurify),
    so an unmatched </i> shows up literally after a citation. Normalize here.
    """
    import re
    open_count = 0
    out = []
    pos = 0
    for m in re.finditer(r"</?i\s*>", text, flags=re.IGNORECASE):
        out.append(text[pos:m.start()])
        if m.group().lstrip("<").startswith("/"):   # closing tag
            if open_count > 0:
                open_count -= 1
                out.append("</i>")
            # else: stray close with no open -> drop it
        else:                                         # opening tag
            open_count += 1
            out.append("<i>")
        pos = m.end()
    out.append(text[pos:])
    result = "".join(out)
    if open_count > 0:                                # close any leftover opens
        result += "</i>" * open_count
    return result


def _clean_llm_response(text: str) -> str:
    """Remove internal citation markers and normalize whitespace before returning text.

    Used by phase-2 response branches so the frontend sees readable text after citation annotation and draft operations complete.
    """
    import re
    text = text.replace("[[CASE_START]]", "")
    text = _balance_italic_tags(text)                       # drop stray/unbalanced <i>/</i>
    text = text.replace("\r\n", "\n").replace("\r", "\n")   # normalize line endings
    text = re.sub(r"\n{3,}", "\n\n", text)                  # collapse excess blank lines
    return text.strip()


def finalize_llm_response_for_display(
    response_text: str,
    state: dict,
    case_viewer_prefix: str | None,
    *,
    verify_citations: bool = True,
) -> tuple[str, list[dict], list[dict]]:
    """Annotate citations, then remove any internal markers before display/storage."""
    cite_info: list[dict] = []
    lookup_diagnostics: list[dict] = []
    finalized = response_text or ""
    if finalized and verify_citations:
        finalized, cite_info, lookup_diagnostics = verify_and_annotate_response_citations(
            finalized,
            state,
            case_viewer_prefix,
        )
    return _clean_llm_response(finalized), cite_info, lookup_diagnostics


def _record_successful_chat_answer(doc_id: int, result: dict | None) -> bool:
    """Count one completed chat round, excluding confirmations, errors, and cancellation."""
    if not isinstance(result, dict):
        return False
    if result.get("error") or result.get("cancelled") or result.get("awaiting_confirmation"):
        return False
    if not result.get("response"):
        return False
    return increment_task_analytics(doc_id, {"answersReturned": 1})
