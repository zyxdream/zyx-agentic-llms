from flask import Blueprint, request, send_file, jsonify, current_app
from io import BytesIO
import logging
# from backend.answer_drafter import route_answer_logic
from backend.answer_drafter import (
    ANSWER_ACTIVE_CONTINUE_TASK_KEY,
    ANSWER_ACTIVE_PARSE_TASK_KEY,
    route_answer_logic,
    draft_full_answer_word,
    get_active_answer_task_id,
    handle_answer_continue,
    handle_answer_upload_complaint,
    DOC_TYPE as ANSWER_DOC_TYPE,
    DOWNLOAD_STEP as ANSWER_DOWNLOAD_STEP,
)
from backend.complaint_drafter import route_complaint_logic, draft_full_complaint_word, regenerate_citations_complaint, update_citation_selections_complaint, DOC_TYPE as COMPLAINT_DOC_TYPE, DOWNLOAD_STEP as COMPLAINT_DOWNLOAD_STEP
from backend.rfp_drafter import (
    RFP_ACTIVE_GENERATE_REQUESTS_TASK_KEY,
    RFP_ACTIVE_PARSE_TASK_KEY,
    route_rfp_logic,
    draft_full_rfp_word,
    further_reduce_rfp_task,
    get_active_rfp_task_id,
    handle_rfp_generate_requests,
    handle_rfp_upload_complaint,
    DOC_TYPE as RFP_DOC_TYPE,
    DOWNLOAD_STEP as RFP_DOWNLOAD_STEP,
)
from backend.rog_drafter import (
    ROG_ACTIVE_CONTINUE_TASK_KEY,
    ROG_ACTIVE_PARSE_TASK_KEY,
    route_rog_logic,
    draft_full_rog_word,
    further_reduce_task,
    get_active_rog_task_id,
    handle_rog_continue,
    handle_rog_upload_complaint,
    DOC_TYPE as ROG_DOC_TYPE,
    DOWNLOAD_STEP as ROG_DOWNLOAD_STEP,
)
from backend.rog_response_drafter import (
    ROG_RESPONSE_ACTIVE_CONTINUE_TASK_KEY,
    ROG_RESPONSE_ACTIVE_PARSE_TASK_KEY,
    route_rog_response_logic,
    draft_full_rog_response_word,
    get_active_rog_response_task_id,
    handle_rog_response_continue_step0,
    handle_rog_response_upload_rog,
    DOC_TYPE as ROG_RESPONSE_DOC_TYPE,
    DOWNLOAD_STEP as ROG_RESPONSE_DOWNLOAD_STEP,
)
from backend.rfa_drafter import (
    RFA_ACTIVE_CONTINUE_TASK_KEY,
    RFA_ACTIVE_PARSE_TASK_KEY,
    route_rfa_logic,
    draft_full_rfa_word,
    further_reduce_rfa_task,
    get_active_rfa_task_id,
    handle_rfa_continue,
    handle_rfa_upload_complaint,
    DOC_TYPE as RFA_DOC_TYPE,
    DOWNLOAD_STEP as RFA_DOWNLOAD_STEP,
)
from backend.rfp_response_drafter import (
    RFP_RESPONSE_ACTIVE_CONTINUE_TASK_KEY,
    RFP_RESPONSE_ACTIVE_PARSE_TASK_KEY,
    route_rfp_response_logic,
    draft_full_rfp_response_word,
    get_active_rfp_response_task_id,
    handle_rfp_response_continue_step0,
    handle_rfp_response_upload_rfp,
    DOC_TYPE as RFP_RESPONSE_DOC_TYPE,
    DOWNLOAD_STEP as RFP_RESPONSE_DOWNLOAD_STEP,
)
from backend.element_checker import route_element_checker, draft_full_analysis_word, regenerate_citations, update_citation_selections, DOC_TYPE as ELEMENT_CHECKER_DOC_TYPE, DOWNLOAD_STEP as ELEMENT_CHECKER_DOWNLOAD_STEP
from backend.standalone_hg import (
    STANDALONE_HG_ACTIVE_HG_TASK_KEY,
    STANDALONE_HG_ACTIVE_UPLOAD_TASK_KEY,
    get_active_standalone_hg_task_id,
    route_standalone_hg,
    standalone_hg_show_dev_progress_details,
    standalone_hg_task,
    standalone_hg_upload_file_task,
)
from backend.dismiss_drafter import (
    draft_full_dismiss_word,
    route_motion_dismiss,
    route_motion_dismiss_update_background,
    route_motion_dismiss_expand_argument,
    update_citation_selections_dismiss,
    DOC_TYPE as DISMISS_DOC_TYPE,
    DOWNLOAD_STEP as DISMISS_DOWNLOAD_STEP,
)
from backend.research import research_task
from backend.everything import (
    everything_turn_task,
    everything_task_phase1,
    everything_task_phase2,
    everything_confirmed_workflow_task,
    draft_full_everything_word,
    DOC_TYPE as EVERYTHING_DOC_TYPE,
    HALLUCINATION_GUARD_STEP,
    DOWNLOAD_STEP,
    load_state_from_memory,
    save_state_to_memory,
    add_section,
    remove_section,
    save_raw_content,
    release_draft_lock,
    update_active_workflow,
    hallucination_guard_task,
    TASK_TIMEOUT_RETRY_MESSAGE,
    TASK_TIMEOUT_RETRY_MESSAGE_PHASE1,
    TASK_TIMEOUT_RETRY_MESSAGE_PHASE2,
)
from backend.smart_search import smart_search_task
from backend.upload_tasks import case_assistant_upload_file_task, smart_search_upload_file_task
from backend.caseviewer_service import (
    CASEVIEWER_ACTIVE_TASK_TTL_SECONDS,
    caseviewer_active_task_key,
    caseviewer_no_details_response,
    load_cached_caseviewer_response,
)
from backend.caseviewer_tasks import caseviewer_enrichment_task
from backend.models import SmartSearchSession as _SmartSearchSession

from celery.result import AsyncResult
from celery.exceptions import SoftTimeLimitExceeded
from billiard.exceptions import TimeLimitExceeded
from flask_cognito import cognito_auth_required, current_user, cognito_group_permissions
from backend.util.msword import convert2doc
from backend.models import User, Project, ChatThread, StepAnalytics, StepLlmModelUsage, TaskAnalytics, UploadedFiles, FolderUpload, DocumentChunk, FileTag, FileTagAssociation, FolderTagAssociation
from backend.database import db
from pathlib import Path
from backend.util.db import DBMemory
from backend.util.free_law_tools import decode_citation_url
from backend.util.create_user import create_user_in_db, disable_cognito_user, enable_cognito_user, delete_cognito_user
from backend.dashboard import dashboard_api as build_dashboard_api_response
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError, OperationalError
import json
import logging
from backend.util.analytics import (
    analytics_record,
    analytics_record_research,
    analytics_record_user_event,
    guided_step_run,
)
from backend.util.task_analytics import (
    LLM_USAGE_DOWNLOAD,
    ensure_task_analytics_row,
    increment_task_analytics,
    task_llm_usage_scope,
)
from backend.util.document_metadata import parse_metadata

from backend.util.data_tools import get_complaint_text_from_file, get_text_from_extended_formats, allowed_filetypes as is_allowed_filetype, is_office_lock_filename
from backend.util.matter_upload import (
    MAX_FILES_PER_MATTER,
    matter_background_counts,
    classify_matter_upload_indices,
    stage_matter_upload_files,
    enqueue_matter_upload_file_tasks,
    cleanup_staged_files,
)
from backend.ai.complaint_parser import get_pagenum_from_pdf_fitz, get_last_ocr_warning
from backend.util.chunker import chunk_text
import uuid
import os
import redis as redis_lib
from backend.constants import REDIS_URL, STATIC_FOLDER, API_URL

CASE_ASSISTANT_MAX_FILE_SIZE = 50 * 1024 * 1024
CASE_ASSISTANT_MAX_FILE_SIZE_MB = 50
CASE_ASSISTANT_MAX_FILES = 100
CASE_ASSISTANT_MAX_TOTAL_BYTES = 1024**3

_cancel_redis = redis_lib.from_url(REDIS_URL)
logger = logging.getLogger(__name__)
CASE_ASSISTANT_TASK_META_TTL = 3600


ALLOWED_EXTENSIONS = {'txt', 'pdf', 'doc', 'docx'}
STANDALONE_HG_DRAFT_TYPE = 9
STANDALONE_HG_MAX_FILE_SIZE = 15 * 1024 * 1024

bp = Blueprint("api", __name__, url_prefix="/api")
OTHER_TASK_TIMEOUT_MESSAGE = "This task timed out. Please try again."


def _durable_upload(scope):
    from functools import wraps
    def decorate(fn):
        @wraps(fn)
        def upload(*args, **kwargs):
            from backend.document_preparation_routes import legacy_upload
            return legacy_upload(scope)
        return upload
    return decorate


def _uploaded_file_metadata(file: UploadedFiles) -> dict:
    return parse_metadata(getattr(file, 'metadata_json', None))


def _is_time_limit_error(exc) -> bool:
    """Return True when Celery reports a soft or hard task time limit."""
    if exc is None:
        return False
    if isinstance(exc, (SoftTimeLimitExceeded, TimeLimitExceeded)):
        return True
    return exc.__class__.__name__ in {"SoftTimeLimitExceeded", "TimeLimitExceeded"}


def _task_failure_value(exc, phase_hint: str | None = None) -> dict[str, object]:
    """Normalize Celery task failures into frontend-friendly payloads."""
    if _is_time_limit_error(exc):
        if phase_hint == "phase1":
            msg = TASK_TIMEOUT_RETRY_MESSAGE_PHASE1
        elif phase_hint == "phase2":
            msg = TASK_TIMEOUT_RETRY_MESSAGE_PHASE2
        elif phase_hint == "other":
            msg = OTHER_TASK_TIMEOUT_MESSAGE
        else:
            msg = TASK_TIMEOUT_RETRY_MESSAGE

        return {
            "error": True,
            "retry": True,
            "retryable": True,
            "error_code": "task_timeout",
            "error_message": msg,
        }

    return {
        "error": True,
        "retry": False,
        "retryable": False,
        "error_code": "task_failed",
        "error_message": "There was an error. Please contact customer support at support@lextext.ai or call 415-617-9439.",
    }


def _case_assistant_task_meta(task_id: str) -> dict:
    """Return optional Redis bookkeeping for a Case Assistant task."""
    raw = _cancel_redis.get(f"task_meta:{task_id}")
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _reconcile_case_assistant_timeout(
    task_id: str, meta: dict, *, expected_active_id: str | None = None
) -> None:
    """Clear only the timed-out run; a newer run must remain untouched."""
    if meta.get("task_type") not in (None, "everything"):
        return
    draft_id_raw = meta.get("draft_id")
    user_id = meta.get("user_id")
    if not draft_id_raw:
        return
    try:
        doc_id = ChatThread.resolve_id(draft_id_raw)
        memory = DBMemory(db=db, doc_id=doc_id)
        state = load_state_from_memory(memory)
        if state.get("active_task_id") != (expected_active_id or task_id):
            return
        state.pop("active_task_id", None)
        update_active_workflow(
            state,
            status="failed",
            phase="finalize",
            pending_turn=None,
            pending_confirmation=None,
            phase1_payload=None,
        )
        save_state_to_memory(state, memory, auxiliary_fields=('active_workflow', 'active_task_id', 'status'))
        if user_id:
            release_draft_lock(doc_id, user_id)
    except Exception:
        logger.exception("Could not reconcile Case Assistant timeout task_id=%s", task_id)

def allowed_filetypes(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@bp.get("/users")
@cognito_auth_required
@cognito_group_permissions(['admins'])
def list_users():
    """Return a list of users that can be displayed in the admin UI."""

    users = User.query.order_by(User.email.asc()).all()
    user_payload = [
        {
            "id": user.id,
            "email": user.email,
            "contactInfo": user.contact_info or "",
            "cognitoUsername": user.cognito_username,
            "isDisabled": user.is_disabled,
        }
        for user in users
    ]

    return jsonify({"users": user_payload})


@bp.post("/createnewuser")
@cognito_auth_required
@cognito_group_permissions(['admins'])
def create_new_user():
    data = request.get_json()
    email = data.get('email')
    if not email:
        return jsonify({'error': 'Email is required'}), 400

    create_msg = create_user_in_db(email, db)
    if create_msg.startswith('Error'):
        return jsonify({'error': create_msg}), 400
    else:
        return jsonify({'message': 'User created successfully'}), 201


@bp.post("/users/<int:user_id>/disable")
@cognito_auth_required
@cognito_group_permissions(['admins'])
def disable_user(user_id: int):
    user = User.query.filter_by(id=user_id).first()
    if user is None:
        return jsonify({'error': 'User not found'}), 404

    if user.is_disabled:
        return jsonify({'message': 'User already disabled'}), 200

    # disable to the user on AWS Cognito
    try:
        disable_cognito_user(user.cognito_username)
        user.is_disabled = True
        db.session.commit()
        return jsonify({'message': 'User disabled successfully'}), 200
    except:
        return jsonify({'error': 'could not disable user'}), 404


@bp.post("/users/<int:user_id>/enable")
@cognito_auth_required
@cognito_group_permissions(['admins'])
def enable_user(user_id: int):
    user = User.query.filter_by(id=user_id).first()
    if user is None:
        return jsonify({'error': 'User not found'}), 404

    if not user.is_disabled:
        return jsonify({'message': 'User already enabled'}), 200

    # enable to the user on AWS Cognito
    try:
        enable_cognito_user(user.cognito_username)
        user.is_disabled = False
        db.session.commit()
        return jsonify({'message': 'User enabled successfully'}), 200
    except:
        return jsonify({'error': 'could not enable user'}), 404


@bp.post("/users/<int:user_id>/delete")
@cognito_auth_required
@cognito_group_permissions(['admins'])
def delete_user(user_id: int):
    user = User.query.filter_by(id=user_id).first()
    if user is None:
        return jsonify({'error': 'User not found'}), 404

    if not user.is_disabled:
        return jsonify({'error': 'User must be disabled before deletion'}), 400

    projects = Project.query.filter_by(userId=user.id).all()
    sessions = _SmartSearchSession.query.filter_by(user_id=user.id).all()

    try:
        StepAnalytics.query.filter_by(userId=user.id).delete(synchronize_session=False)
        TaskAnalytics.query.filter_by(userId=user.id).delete(synchronize_session=False)

        for session in sessions:
            db.session.delete(session)

        for project in projects:
            db.session.delete(project)

        # Flush child deletes first so FK-protected rows are gone before the user delete.
        db.session.flush()

        db.session.delete(user)
        db.session.flush()

        if user.cognito_username:
            delete_cognito_user(user.cognito_username)

        db.session.commit()
        return jsonify({'message': 'User deleted successfully'}), 200
    except Exception as exc:
        db.session.rollback()
        logger.warning("Unable to delete user '%s': %s", user_id, exc)
        return jsonify({'error': 'could not delete user'}), 500

@bp.get("/result/<id>")
@cognito_auth_required
def result(id: str) -> dict[str, object]:
    if id.startswith('wf_'):
        from backend.case_assistant_routes import progress
        return progress.__wrapped__(id)
    result = AsyncResult(id)
    task_meta = _case_assistant_task_meta(id)
    phase1_id_raw = _cancel_redis.get(f"task_chain:{id}")
    phase1_result = AsyncResult(phase1_id_raw.decode()) if phase1_id_raw else None

    if phase1_result and phase1_result.failed():
        failure = phase1_result.result
        if _is_time_limit_error(failure):
            _reconcile_case_assistant_timeout(
                id, task_meta, expected_active_id=phase1_id_raw.decode()
            )
        return {
            "ready": True,
            "successful": False,
            "value": _task_failure_value(failure, phase_hint="phase1"),
            "state": phase1_result.state,
            "info": str(failure) if failure is not None else None,
        }

    ready = result.ready()
    # Include task state and progress meta for frontend to display
    try:
        info = result.info  # Celery meta dict when using update_state
    except Exception:
        info = None

    if ready and not result.successful():
        # result is a Celery AsyncResult object
        # result.result is the task outcome payload: if the task succeeded, it is the return value of the task; if the task failed, it is usually the exception object that caused the failure
        failure = result.result
        if _is_time_limit_error(failure):
            _reconcile_case_assistant_timeout(
                id,
                task_meta,
                expected_active_id=phase1_id_raw.decode() if phase1_id_raw else None,
            )
        # # for Testing: pretend failure is exception object that caused the failure, it has class name TimeLimitExceeded: # failure = TimeLimitExceeded("Monica's Task timed out")
        return {
            "ready": True,
            "successful": False,
            "value": _task_failure_value(
                failure,
                phase_hint=(
                    None if task_meta.get("task_type") == "everything"
                    else "phase2" if phase1_id_raw else "other"
                ),
            ),
            "state": result.state,
            "info": str(failure) if failure is not None else None,
        }

    if ready:
        value = result.get()
        return {
            "ready": True,
            "successful": result.successful(),
            "value": value,
            "state": result.state,
            "info": info if isinstance(info, dict) else (str(info) if info is not None else None),
        }

    # Legacy chains poll their phase-2 id, which stays PENDING until phase 1
    # finishes. Surface phase-1 progress for those in-flight jobs.
    if phase1_result is not None and not phase1_result.ready():
        try:
            phase1_info = phase1_result.info
        except Exception:
            phase1_info = None
        return {
            "ready": False,
            "successful": None,
            "value": None,
            "state": phase1_result.state,
            "info": phase1_info if isinstance(phase1_info, dict) else (str(phase1_info) if phase1_info is not None else None),
        }

    return {
        "ready": False,
        "successful": None,
        "value": result.result,
        "state": result.state,
        "info": info if isinstance(info, dict) else (str(info) if info is not None else None),
    }

@bp.post("/cancel/<task_id>")
@cognito_auth_required
def cancel_task(task_id: str):
    """Cancel a running Celery workload for either Everything or Smart Search.

    This endpoint is intentionally shared by both flows:

    - Everything flow: new turns use one task ID; legacy in-flight chains retain
      the task_chain:<phase2_id> -> <phase1_id> mapping. Revoke the requested
      task and any legacy parent, then clear matching draft state and its lock.

    - Smart Search flow:
      Frontend receives a single task id (no Celery chain parent).
      Redis stores:
        * task_meta:<task_id> -> {"task_type": "smart_search", "session_id", "user_id", ...}
      Cancel behavior:
        1) revoke that single task id
        2) clear active_task_id in Redis smart_search_state:<session_id>
           so stale workers no-op before commit

    In both cases, task_meta ownership is checked against current_user.id first.
    """
    if task_id.startswith('wf_'):
        from backend.case_assistant_routes import cancel
        return cancel.__wrapped__(task_id)
    # Verify task ownership
    meta_raw = _cancel_redis.get(f"task_meta:{task_id}")
    if not meta_raw:
        return {"status": "not_found"}, 404
    meta = json.loads(meta_raw.decode())
    if meta.get("user_id") != current_user.id:
        return {"status": "forbidden"}, 403
    task_type = meta.get("task_type")
    is_everything_task = task_type in (None, "everything")

    celery_app = current_app.extensions["celery"]

    # Revoke requested task id first. Legacy Everything chains have a parent.
    celery_app.control.revoke(task_id, terminate=True, signal='SIGTERM')

    # For Everything chains only: revoke phase1 if we stored parent mapping.
    # For Smart Search this key won't exist, so this block is a no-op.
    phase1_raw = _cancel_redis.get(f"task_chain:{task_id}")
    if phase1_raw:
        celery_app.control.revoke(phase1_raw.decode(), terminate=True, signal='SIGTERM')

    draft_id_raw = meta.get("draft_id")
    user_id = meta.get("user_id")
    # Clear only the task being cancelled. A newer run by the same user must
    # retain its state and lock even if an older cancellation arrives late.
    if is_everything_task and draft_id_raw:
        try:
            doc_id = ChatThread.resolve_id(draft_id_raw)
            mem = DBMemory(db=db, doc_id=doc_id)
            st = load_state_from_memory(mem)
            active_id = phase1_raw.decode() if phase1_raw else task_id
            if st.get('active_task_id') == active_id:
                st.pop('active_task_id', None)
                update_active_workflow(
                    st, status="cancelled", phase="finalize",
                    pending_turn=None, pending_confirmation=None,
                )
                save_state_to_memory(st, mem, auxiliary_fields=('active_workflow', 'active_task_id', 'status'))
                if user_id:
                    release_draft_lock(doc_id, user_id)
        except Exception:
            pass

    # Smart Search-only state cleanup:
    # If session_id exists in meta, clear active_task_id in smart_search_state so an
    # in-flight worker that missed revoke will still no-op before persisting output.
    session_id = meta.get("session_id")
    if session_id:
        try:
            ss_key = f"smart_search_state:{session_id}"
            state_raw = _cancel_redis.get(ss_key)
            if state_raw:
                payload = state_raw.decode() if isinstance(state_raw, (bytes, bytearray)) else state_raw
                state = json.loads(payload)
                if state.get("active_task_id") == task_id:
                    state.pop("active_task_id", None)
                    ttl = _cancel_redis.ttl(ss_key)
                    if ttl is None or ttl <= 0:
                        ttl = 86400
                    _cancel_redis.set(ss_key, json.dumps(state), ex=ttl)
        except Exception:
            pass

    if task_type == "caseviewer":
        active_task_key = meta.get("active_task_key")
        if active_task_key:
            try:
                _cancel_redis.delete(active_task_key)
            except Exception:
                pass

    # Cleanup Redis bookkeeping for this task id.
    # (Everything may also have task_chain:<id>; Smart Search usually will not.)
    _cancel_redis.delete(f"task_chain:{task_id}", f"task_meta:{task_id}")

    return {"status": "cancelled"}, 200

@bp.post('/complaintdrafter')
@cognito_auth_required
def handle_complaint_draft():
    """Creates a complaint."""
    chk = ownership_check(d=request.form, jwt_id=current_user.id)
    if chk is not None:
        return chk
    
    return route_complaint_logic(request)


@bp.post('/complaintdrafter/regenerate-citations')
@cognito_auth_required
def handle_regenerate_citations_complaint():
    """Regenerate citations for a specific COA in the complaint drafter."""
    data = request.get_json()
    logger.info("regenerate-citations-complaint received: %s", data)
    
    # Check ownership
    chk = ownership_check(d={'draftId': data.get('draftId')}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    
    # Trigger the Celery task
    result = regenerate_citations_complaint.delay(data)
    return {"result_id": result.id}, 200


@bp.post('/complaintdrafter/update-citation-selections')
@cognito_auth_required
def handle_update_citation_selections_complaint():
    """Update which citations are selected for inclusion in the final draft."""
    data = request.get_json()
    logger.info("update-citation-selections-complaint received: %s", data)
    
    # Check ownership
    chk = ownership_check(d={'draftId': data.get('draftId')}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    
    # Update the citations
    result = update_citation_selections_complaint(data)
    return result, 200


@bp.post('/answerdrafter')
@cognito_auth_required
def handle_answer_draft():
    """Creates an answer to a complaint."""
    data = request.get_json(silent=True) if request.is_json else request.form
    if not isinstance(data, dict):
        data = request.form
    chk = ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk
    
    return route_answer_logic(request)


@bp.post('/answer/upload-complaint')
@cognito_auth_required
def handle_answer_upload_complaint_route():
    """Upload and parse an Answer complaint without running setup analysis."""
    chk = ownership_check(d=request.form, jwt_id=current_user.id)
    if chk is not None:
        return chk

    return handle_answer_upload_complaint(request)


@bp.post('/answer/continue-step0')
@cognito_auth_required
def handle_answer_continue_step0_route():
    """Run Answer step-0 setup analysis from previously parsed complaint text."""
    data = request.get_json(silent=True) or request.form
    chk = ownership_check(d={'draftId': data.get('draftId')}, jwt_id=current_user.id)
    if chk is not None:
        return chk

    return handle_answer_continue(request)


@bp.post('/answer/continue')
@cognito_auth_required
def handle_answer_continue_route():
    """Compatibility alias for /answer/continue-step0."""
    return handle_answer_continue_step0_route()


@bp.post('/rfpdrafter')
@cognito_auth_required
def handle_rfp_draft():
    """Creates an RFP document."""
    chk = ownership_check(d=request.form, jwt_id=current_user.id)
    if chk is not None:
        return chk
    
    return route_rfp_logic(request)


@bp.post('/rfp/upload-complaint')
@cognito_auth_required
def handle_rfp_upload_complaint_route():
    """Upload and parse an RFP complaint without generating requests."""
    chk = ownership_check(d=request.form, jwt_id=current_user.id)
    if chk is not None:
        return chk

    return handle_rfp_upload_complaint(request)


@bp.post('/rfp/generate-requests')
@cognito_auth_required
def handle_rfp_generate_requests_route():
    """Generate RFP requests from previously parsed complaint text."""
    data = request.get_json(silent=True) or request.form
    chk = ownership_check(d={'draftId': data.get('draftId')}, jwt_id=current_user.id)
    if chk is not None:
        return chk

    return handle_rfp_generate_requests(request)


@bp.post('/rfp/further-reduce')
@cognito_auth_required
def handle_rfp_further_reduce():
    data = request.get_json()
    chk = ownership_check(d={'draftId': data.get('draftId')}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    result = further_reduce_rfp_task.delay(data)
    return {"result_id": result.id}, 200


@bp.post('/rfpresponsedrafter')
@cognito_auth_required
def handle_rfp_response_draft():
    """Creates an RFP document."""
    chk = ownership_check(d=request.form, jwt_id=current_user.id)
    if chk is not None:
        return chk

    return route_rfp_response_logic(request)


@bp.post('/rfp-response/upload-rfp')
@cognito_auth_required
def handle_rfp_response_upload_rfp_route():
    """Upload and parse RFPs without running response setup."""
    chk = ownership_check(d=request.form, jwt_id=current_user.id)
    if chk is not None:
        return chk

    return handle_rfp_response_upload_rfp(request)


@bp.post('/rfp-response/continue-step0')
@cognito_auth_required
def handle_rfp_response_continue_step0_route():
    """Run RFP response setup from previously parsed RFP text."""
    data = request.get_json(silent=True) or request.form
    chk = ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk

    return handle_rfp_response_continue_step0(request)


@bp.post('/rogresponsedrafter')
@cognito_auth_required
def handle_rog_response_draft():
    """Creates responses to interrogatories."""
    chk = ownership_check(d=request.form, jwt_id=current_user.id)
    if chk is not None:
        return chk

    return route_rog_response_logic(request)


@bp.post('/rog-response/upload-rog')
@cognito_auth_required
def handle_rog_response_upload_rog_route():
    """Upload and parse interrogatories without running response setup."""
    chk = ownership_check(d=request.form, jwt_id=current_user.id)
    if chk is not None:
        return chk

    return handle_rog_response_upload_rog(request)


@bp.post('/rog-response/continue-step0')
@cognito_auth_required
def handle_rog_response_continue_step0_route():
    """Run ROG response setup from previously parsed interrogatories text."""
    data = request.get_json(silent=True) or request.form
    chk = ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk

    return handle_rog_response_continue_step0(request)


@bp.post('/rfadrafter')
@cognito_auth_required
def handle_rfa_draft():
    """Creates an RFA document."""
    chk = ownership_check(d=request.form, jwt_id=current_user.id)
    if chk is not None:
        return chk

    return route_rfa_logic(request)


@bp.post('/rfa/upload-complaint')
@cognito_auth_required
def handle_rfa_upload_complaint_route():
    """Upload and parse an RFA complaint without generating admissions."""
    chk = ownership_check(d=request.form, jwt_id=current_user.id)
    if chk is not None:
        return chk

    return handle_rfa_upload_complaint(request)


@bp.post('/rfa/continue-step0')
@cognito_auth_required
def handle_rfa_continue_step0_route():
    """Run RFA step-0 generation from previously parsed complaint text."""
    data = request.get_json(silent=True) or request.form
    chk = ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk

    return handle_rfa_continue(request)


@bp.post('/rfa/further-reduce')
@cognito_auth_required
def handle_rfa_further_reduce():
    data = request.get_json()
    chk = ownership_check(d={'draftId': data.get('draftId')}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    result = further_reduce_rfa_task.delay(data)
    return {"result_id": result.id}, 200


@bp.post('/rogdrafter')
@cognito_auth_required
def handle_rog_draft():
    """Creates an ROG document."""
    logger.info("rogdrafter received: %s", request.form)
    chk = ownership_check(d=request.form, jwt_id=current_user.id)
    if chk is not None:
        return chk
    return route_rog_logic(request)


@bp.post('/rog/upload-complaint')
@cognito_auth_required
def handle_rog_upload_complaint_route():
    """Upload and parse a ROG complaint without generating interrogatories."""
    chk = ownership_check(d=request.form, jwt_id=current_user.id)
    if chk is not None:
        return chk

    return handle_rog_upload_complaint(request)


@bp.post('/rog/continue-step0')
@cognito_auth_required
def handle_rog_continue_step0_route():
    """Run ROG step-0 generation from previously parsed complaint text."""
    data = request.get_json(silent=True) or request.form
    chk = ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk

    return handle_rog_continue(request)


@bp.post('/rog/further-reduce')
@cognito_auth_required
def handle_rog_further_reduce():
    data = request.get_json()
    chk = ownership_check(d={'draftId': data.get('draftId')}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    result = further_reduce_task.delay(data)
    return {"result_id": result.id}, 200




@bp.post('/elementchecker')
@cognito_auth_required
def handle_element_checker():
    """Analizes COAs in a complaint."""
    logger.info("elementchecker received: %s", request.form)
    chk = ownership_check(d=request.form, jwt_id=current_user.id)
    if chk is not None:
        return chk
    
    return route_element_checker(request)


@bp.post('/standalone-hg')
@cognito_auth_required
def handle_standalone_hg():
    data = request.get_json(silent=True) or request.form
    chk = ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk

    draft_obj = ownership_check._last_draft
    if draft_obj.docCategory != STANDALONE_HG_DRAFT_TYPE:
        return {'msg': 'Draft is not a Hallucination Guard run'}, 400

    return route_standalone_hg(request)


@bp.post('/elementchecker/regenerate-citations')
@cognito_auth_required
def handle_regenerate_citations():
    """Regenerate citations for a specific element in the element checker."""
    data = request.get_json()
    logger.info("regenerate-citations received: %s", data)
    
    # Check ownership
    chk = ownership_check(d={'draftId': data.get('draftId')}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    
    # Trigger the Celery task
    result = regenerate_citations.delay(data)
    return {"result_id": result.id}, 200


@bp.post('/elementchecker/update-citation-selections')
@cognito_auth_required
def handle_update_citation_selections():
    """Update which citations are selected for inclusion in the final draft."""
    data = request.get_json()
    logger.info("update-citation-selections received: %s", data)
    
    # Check ownership
    chk = ownership_check(d={'draftId': data.get('draftId')}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    
    # Update the citations
    result = update_citation_selections(data)
    return result, 200


@bp.post('/motiondismiss')
@cognito_auth_required
def handle_motion_dismiss():
    """Suggest and draft motion to dismiss for a complaint"""
    logger.info("motiondismiss received: %s", request.form)
    chk = ownership_check(d=request.form, jwt_id=current_user.id)
    if chk is not None:
        return chk
    
    return route_motion_dismiss(request)

@bp.post('/motiondismiss/state1update')
@cognito_auth_required
def handle_motion_dismiss_state1_update():
    """
    This endpoint handles partial updates for step1 (i.e. user has typed extra facts
    and wants an immediate updated background from LLM).
    """
    logger.info("Received partial update: %s", request.form)
    # Perform ownership check if needed
    chk = ownership_check(d=request.form, jwt_id=current_user.id)
    if chk is not None:
        return chk
    
    return route_motion_dismiss_update_background(request)


@bp.post('/motiondismiss/state3expand')
@cognito_auth_required
def handle_motion_dismiss_state3_expand():
    """This endpoint handles argument expand/revise for step3 """
    logger.info("state3expand received form data: %s", request.form)
    # Perform ownership check if needed
    chk = ownership_check(d=request.form, jwt_id=current_user.id)
    if chk is not None:
        return chk
    return route_motion_dismiss_expand_argument(request.form)


@bp.post('/motiondismiss/update-citation-selections')
@cognito_auth_required
def handle_update_citation_selections_dismiss():
    """Update which citations are selected for inclusion in the final draft."""
    data = request.get_json()
    logger.info("update-citation-selections-dismiss received: %s", data)
    
    # Check ownership
    chk = ownership_check(d={'draftId': data.get('draftId')}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    
    # Update the citations
    result = update_citation_selections_dismiss(data)
    return result, 200


def _resolve_pdf(draftId):
    """Try new UUID-based filename first, fall back to legacy display-name-only filename."""
    new_name = ChatThread.pdf_filename(draftId)
    new_path = os.path.join(STATIC_FOLDER, new_name)
    if os.path.exists(new_path):
        return new_path, f'{API_URL}static/{new_name}'
    # Legacy fallback: LexText_{safe}.pdf (no UUID)
    draft = ChatThread.query.filter_by(public_id=draftId).first()
    if draft and draft.display_name:
        safe = re.sub(r'[^A-Za-z0-9]+', '_', draft.display_name).strip('_')
        legacy_name = f'LexText_{safe}.pdf'
        legacy_path = os.path.join(STATIC_FOLDER, legacy_name)
        if os.path.exists(legacy_path):
            return legacy_path, f'{API_URL}static/{legacy_name}'
    return None, None


def _static_pdf_url(filename):
    """Return a static URL when a saved PDF-ish file exists in STATIC_FOLDER."""
    if filename and os.path.exists(os.path.join(STATIC_FOLDER, filename)):
        return f'{API_URL}static/{filename}'
    return None


def _restore_pdf_url(draftId, mem, resume_step, generated_from_step, uploaded_key='inputs/uploaded_filename'):
    """Pick the viewer PDF for a restored guided draft.

    Before a generated draft step, keep showing the uploaded source document.
    Once generation has begun, prefer the generated draft. Missing preferred
    files fall back to the best available legacy/generated/uploaded URL.
    """
    uploaded_pdf_url = _static_pdf_url(mem.get(uploaded_key, ''))
    generated_pdf_url = _static_pdf_url(mem.get('inputs/generated_pdf_filename', ''))

    if resume_step < generated_from_step:
        if uploaded_pdf_url:
            return uploaded_pdf_url
        if generated_pdf_url:
            return generated_pdf_url
        _, fallback_pdf_url = _resolve_pdf(draftId)
        return fallback_pdf_url

    if generated_pdf_url:
        return generated_pdf_url
    _, fallback_pdf_url = _resolve_pdf(draftId)
    return fallback_pdf_url or uploaded_pdf_url


def _answer_resume_step(mem, ads_suggestions: dict, outline: list, general_denial: str | None) -> int:
    if mem.get('drafted_sections/intro', ''):
        return 5
    if mem.get('drafted_sections/counter_claims', ''):
        return 4
    if mem.get('drafted_sections/affirmative_defenses', ''):
        return 3
    if mem.get('drafted_sections/responses', ''):
        return 2
    if ads_suggestions or outline or general_denial:
        return 1
    return 0


@bp.get('/complaint-inputs/<string:draftId>')
@cognito_auth_required
def get_complaint_inputs(draftId):
    """Return saved complaint input fields for the given draft."""
    chk = ownership_check({'draftId': draftId}, current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft

    mem = DBMemory(db=db, doc_id=draft.id)

    plaintiff = mem.get('inputs/plaintiff', '')
    plaintiff_address = mem.get('inputs/plaintiffAddress', '')
    defendant = mem.get('inputs/defendant', '')
    defendant_address = mem.get('inputs/defendantAddress', '')
    venue = mem.get('inputs/venue', '')
    factual = mem.get('inputs/factualAllegations', '')

    plaintiff_list = [p.strip() for p in plaintiff.split(',')] if plaintiff else []
    defendant_list = [d.strip() for d in defendant.split(',')] if defendant else []

    coa_suggestions_raw = mem.get('analysis/coa_suggestions', '')
    pfr_suggestions_raw = mem.get('analysis/pfr_suggestions', '')
    intro_suggestion = mem.get('analysis/intro_suggestion', '')
    intro = mem.get('drafted_sections/intro', '') or intro_suggestion

    try:
        coa_suggestions = json.loads(coa_suggestions_raw) if coa_suggestions_raw else []
    except Exception:
        coa_suggestions = []

    try:
        pfr_suggestions = json.loads(pfr_suggestions_raw) if pfr_suggestions_raw else []
    except Exception:
        pfr_suggestions = []

    # Determine which step to resume at based on what has been saved
    if mem.get('drafted_sections/intro', ''):
        resume_step = 4
    elif mem.get('drafted_sections/prayer', ''):
        resume_step = 3
    elif mem.get('drafted_sections/causes', ''):
        resume_step = 2
    elif coa_suggestions:
        resume_step = 1
    else:
        resume_step = 0

    # Include PDF URL if the file has been generated
    pdf_url = _restore_pdf_url(draftId, mem, resume_step, generated_from_step=0)

    try:
        halls = json.loads(mem.get('hallucination_check', '[]') or '[]')
    except Exception:
        halls = []

    return jsonify({
        'plaintiff': plaintiff_list,
        'plaintiffAddress': plaintiff_address,
        'defendant': defendant_list,
        'defendantAddress': defendant_address,
        'venue': venue,
        'factualAllegations': factual,
        'coa_suggestions': coa_suggestions,
        'pfr_suggestions': pfr_suggestions,
        'intro': intro,
        'resume_step': resume_step,
        'pdf_url': pdf_url,
        'halls': halls,
    })


@bp.get('/answer-inputs/<draftId>')
@cognito_auth_required
def get_answer_inputs(draftId):
    """Return saved answer drafter state for the given draft."""
    chk = ownership_check({'draftId': draftId}, current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft

    mem = DBMemory(db=db, doc_id=draft.id)

    try:
        ads_suggestions = json.loads(mem.get('ads_suggestions', '{}') or '{}')
    except Exception:
        ads_suggestions = {}

    try:
        outline_raw = mem.get('analysis/outline', '') or mem.get('outline', '')
        outline = json.loads(outline_raw) if outline_raw else []
    except Exception:
        outline = []

    try:
        paragraph_warnings = json.loads(mem.get('paragraph_warnings', '{}') or '{}')
    except Exception:
        paragraph_warnings = {}

    general_denial = mem.get('analysis/general_denial', '') or None
    intro = mem.get('drafted_sections/intro', '')
    uploaded_filename = mem.get('inputs/uploaded_filename', '')
    has_parsed_complaint = bool(mem.get('complaint_text', '') and mem.get('complaint_cover_text', ''))

    try:
        halls = json.loads(mem.get('hallucination_check', '[]') or '[]')
    except Exception:
        halls = []

    defendant_str = mem.get('inputs/defendant', '')
    defendant = [[d.strip()] for d in defendant_str.split(';')] if defendant_str else []

    resume_step = _answer_resume_step(mem, ads_suggestions, outline, general_denial)

    pdf_url = _restore_pdf_url(draftId, mem, resume_step, generated_from_step=2)

    return jsonify({
        'ads_suggestions': ads_suggestions,
        'outline': outline,
        'paragraph_warnings': paragraph_warnings,
        'general_denial': general_denial,
        'intro': intro,
        'defendant': defendant,
        'resume_step': resume_step,
        'pdf_url': pdf_url,
        'halls': halls,
        'uploaded_filename': uploaded_filename,
        'has_parsed_complaint': has_parsed_complaint,
        'active_parse_task_id': get_active_answer_task_id(draft.id, ANSWER_ACTIVE_PARSE_TASK_KEY),
        'active_continue_task_id': get_active_answer_task_id(draft.id, ANSWER_ACTIVE_CONTINUE_TASK_KEY),
    })


@bp.get('/draft-pdf/<string:draftId>')
@cognito_auth_required
def serve_draft_pdf(draftId):
    chk = ownership_check({'draftId': draftId}, current_user.id)
    if chk is not None:
        return chk
    pdf_path, _ = _resolve_pdf(draftId)
    if not pdf_path:
        return {'msg': 'PDF not found'}, 404
    return send_file(pdf_path, mimetype='application/pdf')


import re

def _sanitize_name(name):
    """Sanitize a draft display name for use in download filenames."""
    return re.sub(r'[^A-Za-z0-9]+', '_', name).strip('_') if name else 'Lextext_draft'


@bp.get('/download-word-answer/<string:draftId>')
@cognito_auth_required
def download_word_answer(draftId):
    """Requires a draftId to be sent over."""
    chk = ownership_check(d={'draftId': draftId}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft
    with guided_step_run(draft.id, ANSWER_DOC_TYPE, ANSWER_DOWNLOAD_STEP):
        mem = DBMemory(db=db, doc_id=draft.id)
        output_fname = f"static/{draft.public_id}.docx"
        draft_full_answer_word(mem, f"backend/{output_fname}")
        outgoing_name = f'LexIntel_{_sanitize_name(draft.display_name)}.docx'
        return send_file(output_fname, as_attachment=True, download_name=outgoing_name)


@bp.get('/download-word-complaint/<string:draftId>')
@cognito_auth_required
def download_word_complaint(draftId):
    """Requires a draftId to be sent over."""
    chk = ownership_check(d={'draftId': draftId}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft
    with guided_step_run(draft.id, COMPLAINT_DOC_TYPE, COMPLAINT_DOWNLOAD_STEP):
        mem = DBMemory(db=db, doc_id=draft.id)
        output_fname = f"static/{draft.public_id}.docx"
        draft_full_complaint_word(mem, f"backend/{output_fname}")
        outgoing_name = f'LexIntel_{_sanitize_name(draft.display_name)}.docx'
        return send_file(output_fname, as_attachment=True, download_name=outgoing_name)

@bp.get('/rfp-inputs/<draftId>')
@cognito_auth_required
def get_rfp_inputs(draftId):
    """Return saved RFP drafter state for the given draft."""
    chk = ownership_check({'draftId': draftId}, current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft

    mem = DBMemory(db=db, doc_id=draft.id)

    try:
        reduced_rfps = json.loads(mem.get('temp/reduced_rfps', '[]') or '[]')
    except Exception:
        reduced_rfps = []

    try:
        consolidated_rfps = json.loads(mem.get('temp/consolidated_rfps', '[]') or '[]')
    except Exception:
        consolidated_rfps = []

    # Prefer the further-reduced list if the user ran that step
    rfps_to_restore = reduced_rfps if reduced_rfps else consolidated_rfps

    intro = mem.get('drafted_sections/intro', '')
    definition = mem.get('drafted_sections/defs', '')
    instructions = mem.get('drafted_sections/instructions', '')
    uploaded_filename = mem.get('inputs/uploaded_filename', '')
    has_parsed_complaint = bool(mem.get('complaint_text', '') and mem.get('complaint_cover_text', ''))

    if mem.get('drafted_sections/rfps', ''):
        resume_step = 2
    elif rfps_to_restore:
        resume_step = 1
    else:
        resume_step = 0

    pdf_url = _restore_pdf_url(draftId, mem, resume_step, generated_from_step=2)

    return jsonify({
        'consolidated_rfps': rfps_to_restore,
        'intro': intro,
        'definition': definition,
        'instructions': instructions,
        'resume_step': resume_step,
        'pdf_url': pdf_url,
        'uploaded_filename': uploaded_filename,
        'has_parsed_complaint': has_parsed_complaint,
        'active_parse_task_id': get_active_rfp_task_id(draft.id, RFP_ACTIVE_PARSE_TASK_KEY),
        'active_generate_requests_task_id': get_active_rfp_task_id(draft.id, RFP_ACTIVE_GENERATE_REQUESTS_TASK_KEY),
    })


@bp.get('/download-word-rfp/<draftId>')
@cognito_auth_required
def download_word_rfp(draftId):
    """Requires a draftId to be sent over."""
    chk = ownership_check(d={'draftId': draftId}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft
    with guided_step_run(draft.id, RFP_DOC_TYPE, RFP_DOWNLOAD_STEP):
        mem = DBMemory(db=db, doc_id=draft.id)
        output_fname = f"static/{draft.public_id}.docx"
        draft_full_rfp_word(mem, f"backend/{output_fname}")
        outgoing_name = f'LexIntel_{_sanitize_name(draft.display_name)}.docx'
        return send_file(output_fname, as_attachment=True, download_name=outgoing_name)


@bp.get('/objections-inputs/<draftId>')
@cognito_auth_required
def get_objections_inputs(draftId):
    """Return saved Objections drafter state for the given draft."""
    chk = ownership_check({'draftId': draftId}, current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft

    mem = DBMemory(db=db, doc_id=draft.id)

    intro = mem.get('drafted_sections/intro', '') or mem.get('analysis/intro', '')
    general = mem.get('drafted_sections/gen_obj', '') or mem.get('analysis/general', '')
    definition_objections = mem.get('analysis/definition_objections', '')
    instruction_objections = mem.get('analysis/instruction_objections', '')

    try:
        responses = json.loads(mem.get('drafted_sections/requests', '[]') or '[]')
    except Exception:
        responses = []

    if responses:
        resume_step = 2
    elif mem.get('drafted_sections/definitions', ''):
        resume_step = 1
    else:
        resume_step = 0

    pdf_url = _restore_pdf_url(draftId, mem, resume_step, generated_from_step=2)

    return jsonify({
        'intro': intro,
        'general': general,
        'definition_objections': definition_objections,
        'instruction_objections': instruction_objections,
        'responses': responses,
        'uploaded_filename': mem.get('inputs/uploaded_filename', ''),
        'has_parsed_document': bool(mem.get('body_text', '') and mem.get('cover_text', '')),
        'active_parse_task_id': get_active_rfp_response_task_id(draft.id, RFP_RESPONSE_ACTIVE_PARSE_TASK_KEY),
        'active_continue_task_id': get_active_rfp_response_task_id(draft.id, RFP_RESPONSE_ACTIVE_CONTINUE_TASK_KEY),
        'resume_step': resume_step,
        'pdf_url': pdf_url,
    })


@bp.get('/rog-objections-inputs/<draftId>')
@cognito_auth_required
def get_rog_objections_inputs(draftId):
    """Return saved ROG Response drafter state for the given draft."""
    chk = ownership_check({'draftId': draftId}, current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft

    mem = DBMemory(db=db, doc_id=draft.id)

    intro = mem.get('drafted_sections/intro', '')
    general = mem.get('drafted_sections/gen_obj', '')

    try:
        responses = json.loads(mem.get('drafted_sections/interrogatories', '[]') or '[]')
    except Exception:
        responses = []

    if responses and any(r.get('objection') for r in responses):
        resume_step = 2
    elif intro:
        resume_step = 1
    else:
        resume_step = 0

    pdf_url = _restore_pdf_url(draftId, mem, resume_step, generated_from_step=2)

    return jsonify({
        'intro': intro,
        'general': general,
        'responses': responses,
        'uploaded_filename': mem.get('inputs/uploaded_filename', ''),
        'has_parsed_document': bool(mem.get('body_text', '') and mem.get('cover_text', '')),
        'active_parse_task_id': get_active_rog_response_task_id(draft.id, ROG_RESPONSE_ACTIVE_PARSE_TASK_KEY),
        'active_continue_task_id': get_active_rog_response_task_id(draft.id, ROG_RESPONSE_ACTIVE_CONTINUE_TASK_KEY),
        'resume_step': resume_step,
        'pdf_url': pdf_url,
    })


@bp.get('/element-checker-inputs/<draftId>')
@cognito_auth_required
def get_element_checker_inputs(draftId):
    """Return saved Complaint Analyzer drafter state for the given draft."""
    chk = ownership_check({'draftId': draftId}, current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft

    mem = DBMemory(db=db, doc_id=draft.id)

    fa_short = mem.get('drafted_sections/fa_sum_final', '')
    fa_sum_str = mem.get('drafted_sections/fa_sum', '')
    fas = [line for line in fa_sum_str.split('\n') if line] if fa_sum_str else []

    try:
        coa_sum = json.loads(mem.get('analysis/coa_sum', '{}') or '{}')
        coas = list(coa_sum.values()) if coa_sum else []
    except Exception:
        coas = []

    el_data = []
    recomendations = []
    try:
        element_analysis = json.loads(mem.get('analysis/element_analysis', '{}') or '{}')
        if element_analysis:
            el_data = [
                [v['title'], {'description': v.get('description', []), 'elements': v.get('elements', [])}]
                for v in element_analysis.values()
            ]
            recomendations = [
                [v['title'], v.get('recommendations_html', [])]
                for v in element_analysis.values()
                if 'recommendations_html' in v
            ]
    except Exception:
        pass

    if mem.get('drafted_sections/recommendation_markdown', ''):
        resume_step = 4
    elif mem.get('drafted_sections/recommendation', ''):
        resume_step = 3
    elif mem.get('analysis/element_analysis', ''):
        resume_step = 2
    elif mem.get('analysis/coa_sum', ''):
        resume_step = 1
    else:
        resume_step = 0

    pdf_url = _restore_pdf_url(
        draftId,
        mem,
        resume_step,
        generated_from_step=2,
        uploaded_key='inputs/filename',
    )

    try:
        halls = json.loads(mem.get('hallucination_check', '[]') or '[]')
    except Exception:
        halls = []

    return jsonify({
        'fas': fas,
        'fa_short': fa_short,
        'coas': coas,
        'el_data': el_data,
        'recomendations': recomendations,
        'resume_step': resume_step,
        'pdf_url': pdf_url,
        'halls': halls,
    })


@bp.get('/standalone-hg-inputs/<draftId>')
@cognito_auth_required
def get_standalone_hg_inputs(draftId):
    chk = ownership_check({'draftId': draftId}, current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft
    if draft.docCategory != STANDALONE_HG_DRAFT_TYPE:
        return {'msg': 'Draft is not a Hallucination Guard run'}, 400

    mem = DBMemory(db=db, doc_id=draft.id)
    uploaded_file = UploadedFiles.query.filter_by(chat_thread_id=draft.id).order_by(UploadedFiles.upload_time.desc()).first()

    case_results_html = mem.get('standalone_hg/case_results_html', '') or ''
    statute_results_html = mem.get('standalone_hg/statute_results_html', '') or ''
    prompt_token_counts_raw = mem.get('standalone_hg/prompt_token_counts', '') or ''
    try:
        prompt_token_counts = json.loads(prompt_token_counts_raw) if prompt_token_counts_raw else None
    except Exception:
        prompt_token_counts = None

    uploaded_file_payload = None
    if uploaded_file is not None:
        uploaded_file_payload = {
            'unique_id': uploaded_file.unique_id,
            'filename': uploaded_file.original_filename,
            'file_size': uploaded_file.file_size,
            'upload_time': uploaded_file.upload_time.isoformat() if uploaded_file.upload_time else None,
        }

    return jsonify({
        'draftId': draft.public_id,
        'draftName': draft.display_name,
        'projectId': draft.projectId,
        'case_results_html': case_results_html,
        'statute_results_html': statute_results_html,
        'prompt_token_counts': prompt_token_counts,
        'uploaded_file': uploaded_file_payload,
        'show_dev_progress_details': standalone_hg_show_dev_progress_details(),
        'active_upload_task_id': get_active_standalone_hg_task_id(draft.id, STANDALONE_HG_ACTIVE_UPLOAD_TASK_KEY),
        'active_hg_task_id': get_active_standalone_hg_task_id(draft.id, STANDALONE_HG_ACTIVE_HG_TASK_KEY),
    })


@bp.get('/dismiss-inputs/<draftId>')
@cognito_auth_required
def get_dismiss_inputs(draftId):
    """Return saved Motion to Dismiss drafter state for the given draft."""
    chk = ownership_check({'draftId': draftId}, current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft

    mem = DBMemory(db=db, doc_id=draft.id)

    background = mem.get('drafted_sections/fa_background', '')
    standard = mem.get('drafted_sections/legal_standard', '')
    standard_citations = mem.get('drafted_sections/standard_citations', '')
    introduction = mem.get('drafted_sections/intro', '')
    conclusion = mem.get('drafted_sections/conclusion', '')

    try:
        fas = json.loads(mem.get('inputs/fa_sums', '[]') or '[]')
    except Exception:
        fas = []

    try:
        coa_dict_raw = json.loads(mem.get('coa_dict', '{}') or '{}')
        coa_dict_list = list(coa_dict_raw.values()) if coa_dict_raw else []
    except Exception:
        coa_dict_list = []

    batch_heading = ''
    batch_argument = ''
    total_batches = 1
    batch_citations = []
    current_batch_index = 0
    expanded_arguments_map = {}
    try:
        batches = json.loads(mem.get('drafted_sections/batches', '{}') or '{}')
        if batches:
            total_batches = len(batches)
            # Restore to the batch the user was last viewing, not always batch 0
            saved_idx = mem.get('drafted_sections/current_batch_index', '0') or '0'
            current_batch_index = int(saved_idx)
            restore_batch = batches.get(str(current_batch_index), batches.get('0', {}))
            batch_argument = restore_batch.get('argument', '')
            batch_heading = restore_batch.get('sub_heading', '')
            raw_citations = restore_batch.get('final_citations', [])
            batch_citations = []
            for cit in raw_citations:
                try:
                    batch_citations.append(json.loads(cit))
                except Exception:
                    pass
            # Collect any saved expanded arguments across all batches
            for idx, batch in batches.items():
                if batch.get('expanded_argument'):
                    expanded_arguments_map[int(idx)] = batch['expanded_argument']
    except Exception:
        pass

    if conclusion:
        resume_step = 5
    elif mem.get('drafted_sections/batches', ''):
        resume_step = 3
    elif coa_dict_list:
        resume_step = 2
    elif background:
        resume_step = 1
    else:
        resume_step = 0

    pdf_url = _restore_pdf_url(draftId, mem, resume_step, generated_from_step=3)

    return jsonify({
        'fas': fas,
        'background': background,
        'standard': standard,
        'standard_citations': standard_citations,
        'coa_dict_list': coa_dict_list,
        'batch_heading': batch_heading,
        'batch_argument': batch_argument,
        'total_batches': total_batches,
        'batch_citations': batch_citations,
        'current_batch_index': current_batch_index,
        'expanded_arguments_map': expanded_arguments_map,
        'introduction': introduction,
        'conclusion': conclusion,
        'resume_step': resume_step,
        'pdf_url': pdf_url,
    })


@bp.get('/download-word-rfp-response/<draftId>')
@cognito_auth_required
def download_word_rfp_response(draftId):
    """Requires a draftId to be sent over."""
    chk = ownership_check(d={'draftId': draftId}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft
    with guided_step_run(draft.id, RFP_RESPONSE_DOC_TYPE, RFP_RESPONSE_DOWNLOAD_STEP):
        mem = DBMemory(db=db, doc_id=draft.id)
        output_fname = f"static/{draft.public_id}.docx"
        draft_full_rfp_response_word(mem, f"backend/{output_fname}")
        outgoing_name = f'LexIntel_{_sanitize_name(draft.display_name)}.docx'
        return send_file(output_fname, as_attachment=True, download_name=outgoing_name)


@bp.get('/download-word-rog-response/<string:draftId>')
@cognito_auth_required
def download_word_rog_response(draftId):
    """Requires a draftId to be sent over."""
    chk = ownership_check(d={'draftId': draftId}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft
    with guided_step_run(draft.id, ROG_RESPONSE_DOC_TYPE, ROG_RESPONSE_DOWNLOAD_STEP):
        mem = DBMemory(db=db, doc_id=draft.id)
        output_fname = f"static/{draft.public_id}.docx"
        draft_full_rog_response_word(mem, f"backend/{output_fname}")
        outgoing_name = f'LexIntel_{_sanitize_name(draft.display_name)}.docx'
        return send_file(output_fname, as_attachment=True, download_name=outgoing_name)


@bp.get('/rfa-inputs/<draftId>')
@cognito_auth_required
def get_rfa_inputs(draftId):
    """Return saved RFA drafter state for the given draft."""
    chk = ownership_check({'draftId': draftId}, current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft

    mem = DBMemory(db=db, doc_id=draft.id)

    try:
        reduced_rfas = json.loads(mem.get('temp/reduced_rfas', '[]') or '[]')
    except Exception:
        reduced_rfas = []

    try:
        consolidated_rfas = json.loads(mem.get('temp/consolidated_rfas', '[]') or '[]')
    except Exception:
        consolidated_rfas = []

    # Prefer the further-reduced list if the user ran that step
    rfas_to_restore = reduced_rfas if reduced_rfas else consolidated_rfas

    intro = mem.get('drafted_sections/intro', '')
    definition = mem.get('drafted_sections/defs', '')
    instructions = mem.get('drafted_sections/instructions', '')

    if mem.get('drafted_sections/rfas', ''):
        resume_step = 2
    elif rfas_to_restore:
        resume_step = 1
    else:
        resume_step = 0

    pdf_url = _restore_pdf_url(draftId, mem, resume_step, generated_from_step=2)

    return jsonify({
        'consolidated_rfas': rfas_to_restore,
        'intro': intro,
        'definition': definition,
        'instructions': instructions,
        'uploaded_filename': mem.get('inputs/uploaded_filename', ''),
        'has_parsed_complaint': bool(mem.get('complaint_text', '') and mem.get('complaint_cover_text', '')),
        'active_parse_task_id': get_active_rfa_task_id(draft.id, RFA_ACTIVE_PARSE_TASK_KEY),
        'active_continue_task_id': get_active_rfa_task_id(draft.id, RFA_ACTIVE_CONTINUE_TASK_KEY),
        'resume_step': resume_step,
        'pdf_url': pdf_url,
    })


@bp.get('/download-word-rfa/<draftId>')
@cognito_auth_required
def download_word_rfa(draftId):
    """Requires a draftId to be sent over."""
    chk = ownership_check(d={'draftId': draftId}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft
    with guided_step_run(draft.id, RFA_DOC_TYPE, RFA_DOWNLOAD_STEP):
        mem = DBMemory(db=db, doc_id=draft.id)
        output_fname = f"static/{draft.public_id}.docx"
        draft_full_rfa_word(mem, f"backend/{output_fname}")
        outgoing_name = f'LexIntel_{_sanitize_name(draft.display_name)}.docx'
        return send_file(output_fname, as_attachment=True, download_name=outgoing_name)


@bp.get('/rog-inputs/<draftId>')
@cognito_auth_required
def get_rog_inputs(draftId):
    """Return saved ROG drafter state for the given draft."""
    chk = ownership_check({'draftId': draftId}, current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft

    mem = DBMemory(db=db, doc_id=draft.id)

    try:
        reduced_rogs = json.loads(mem.get('temp/reduced_rogs', '[]') or '[]')
    except Exception:
        reduced_rogs = []

    try:
        consolidated_rogs = json.loads(mem.get('temp/consolidated_rogs', '[]') or '[]')
    except Exception:
        consolidated_rogs = []

    # Prefer the further-reduced list if the user ran that step
    rogs_to_restore = reduced_rogs if reduced_rogs else consolidated_rogs

    intro = mem.get('drafted_sections/intro', '')
    definition = mem.get('drafted_sections/defs', '')
    instructions = mem.get('drafted_sections/instructions', '')

    if mem.get('drafted_sections/rogs', ''):
        resume_step = 2
    elif rogs_to_restore:
        resume_step = 1
    else:
        resume_step = 0

    pdf_url = _restore_pdf_url(draftId, mem, resume_step, generated_from_step=2)

    return jsonify({
        'consolidated_rogs': rogs_to_restore,
        'intro': intro,
        'definition': definition,
        'instructions': instructions,
        'uploaded_filename': mem.get('inputs/uploaded_filename', ''),
        'has_parsed_complaint': bool(mem.get('complaint_text', '') and mem.get('complaint_cover_text', '')),
        'active_parse_task_id': get_active_rog_task_id(draft.id, ROG_ACTIVE_PARSE_TASK_KEY),
        'active_continue_task_id': get_active_rog_task_id(draft.id, ROG_ACTIVE_CONTINUE_TASK_KEY),
        'resume_step': resume_step,
        'pdf_url': pdf_url,
    })


@bp.get('/download-word-rog/<draftId>')
@cognito_auth_required
def download_word_rog(draftId):
    """Requires a draftId to be sent over."""
    chk = ownership_check(d={'draftId': draftId}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft
    with guided_step_run(draft.id, ROG_DOC_TYPE, ROG_DOWNLOAD_STEP):
        mem = DBMemory(db=db, doc_id=draft.id)
        output_fname = f"static/{draft.public_id}.docx"
        draft_full_rog_word(mem, f"backend/{output_fname}")
        outgoing_name = f'LexIntel_{_sanitize_name(draft.display_name)}.docx'
        return send_file(output_fname, as_attachment=True, download_name=outgoing_name)


@bp.get('/download-word-analysis/<string:draftId>')
@cognito_auth_required
def download_word_analysis(draftId):
    """Requires a draftId to be sent over."""
    chk = ownership_check(d={'draftId': draftId}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft
    with guided_step_run(draft.id, ELEMENT_CHECKER_DOC_TYPE, ELEMENT_CHECKER_DOWNLOAD_STEP):
        mem = DBMemory(db=db, doc_id=draft.id)
        output_fname = f"static/{draft.public_id}.docx"
        draft_full_analysis_word(mem, f"backend/{output_fname}")
        outgoing_name = f'LexIntel_{_sanitize_name(draft.display_name)}.docx'
        return send_file(output_fname, as_attachment=True, download_name=outgoing_name)


@bp.get('/download-word-dismiss/<string:draftId>')
@cognito_auth_required
def download_word_dismiss(draftId):
    """Requires a draftId to be sent over."""
    chk = ownership_check(d={'draftId': draftId}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft
    with guided_step_run(draft.id, DISMISS_DOC_TYPE, DISMISS_DOWNLOAD_STEP):
        mem = DBMemory(db=db, doc_id=draft.id)
        output_fname = f"static/{draft.public_id}.docx"
        draft_full_dismiss_word(mem, f"backend/{output_fname}")
        outgoing_name = f'LexIntel_{_sanitize_name(draft.display_name)}.docx'
        return send_file(output_fname, as_attachment=True, download_name=outgoing_name)


@bp.post('/download-everything/<string:draftId>')
@cognito_auth_required
def download_word_everything(draftId):
    """Export a pinned Markdown revision, or preserve the legacy Word path."""
    from backend.util.document_drafts import is_markdown_draft
    from backend.util.document_downloads import download_revision, DocumentDownloadError
    from backend.util.document_exports import DocumentExportError
    from backend.util.document_preparation import PreparationError
    data = request.get_json() or {}
    if not isinstance(data, dict):
        return {'msg': 'A download request must be a JSON object.'}, 400
    chk = ownership_check(d={**data, 'draftId': draftId}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft
    analytics_record(db, draft.id, EVERYTHING_DOC_TYPE, DOWNLOAD_STEP)
    increment_task_analytics(draft.id, {"downloadAttempts": 1})

    try:
        section_order = data.get('sectionOrder', [])
        mem = DBMemory(db=db, doc_id=draft.id)
        state = json.loads(mem.get('state', '{}'))
        current_draft = state.get('draft') or {}
        if is_markdown_draft(current_draft) or data.get('revision_id'):
            path, metadata, export_id = download_revision(user_id=current_user.id,
                draft_id=draft.id, revision_id=data.get('revision_id') or current_draft.get('revision_id'),
                format=data.get('format', 'docx'), preset=data.get('preset', 'general'),
                current_draft=current_draft)
            response = send_file(path, as_attachment=True, download_name=metadata['filename'],
                mimetype=metadata['content_type'], conditional=True)
            response.headers['X-Document-Revision'] = data.get('revision_id') or current_draft['revision_id']
            response.headers['X-Export-Artifact'] = export_id
        else:
            if data.get('format', 'docx') != 'docx':
                raise DocumentDownloadError('Request a document revision to use the additional download formats.')
            output_fname = f"static/{draft.public_id}.docx"
            with task_llm_usage_scope(draft.id, LLM_USAGE_DOWNLOAD):
                draft_full_everything_word(mem, f"backend/{output_fname}", section_order)
            outgoing_name = f'LexIntel_{_sanitize_name(draft.display_name)}.docx'
            response = send_file(output_fname, as_attachment=True, download_name=outgoing_name)
    except DocumentDownloadError as exc:
        increment_task_analytics(draft.id, {"downloadFailures": 1})
        return {'msg': str(exc)}, exc.status
    except PreparationError as exc:
        increment_task_analytics(draft.id, {"downloadFailures": 1})
        return exc.payload, exc.status
    except DocumentExportError as exc:
        increment_task_analytics(draft.id, {"downloadFailures": 1})
        current_app.logger.warning('Document export failed: %s', exc)
        return {'msg': str(exc)}, 503
    except Exception:
        increment_task_analytics(draft.id, {"downloadFailures": 1})
        raise

    increment_task_analytics(draft.id, {"downloadSuccesses": 1})
    return response


@bp.get('/download-word/<string:draftId>')
@cognito_auth_required
def download_word(draftId):
    """Requires a draftId to be sent over."""
    chk = ownership_check(d={'draftId': draftId}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft

    output_fname = "static/word.docx"

    pdf_name = ChatThread.pdf_filename(draft.public_id)
    myp = Path(f'backend/static/{pdf_name}')
    if not myp.exists():
        return {'msg': 'The requested file does not exist.'}, 404

    convert2doc(f'backend/static/{pdf_name}', 'backend/' + output_fname)

    outgoing_name = f'LexText_{_sanitize_name(draft.display_name)}.docx'
    return send_file(output_fname, as_attachment=True, download_name=outgoing_name)


@bp.get('/')
def hello():
    """This is for debug to demonstrate the API is reachable."""
    return "This is for API usage only."

SUCCESS_JSON = {'msg': 'Command processed successfully!'}
####   ***************  DB CRUD Routes
@bp.post('create-project')
@cognito_auth_required
def create_project():
    data = request.get_json()
    if 'proj-name' not in data:
        return {'msg': 'missing project name'}, 401
    
    logger.info("create_project data: %s", data)

    # check if this user already has a project with this name
    proj_check = Project.query.filter_by(name=data['proj-name'], userId=current_user.id).first()
    if proj_check is not None:
        return {'msg': 'Project with that name already exists'}, 401

    new_proj = Project(name=data['proj-name'], userId=current_user.id)
    db.session.add(new_proj)
    db.session.commit()  # we need to do a commit here to get an id

    # see if the user wants to create a new draft also
    if 'draft-name' in data and 'draft-type' in data and data['draft-name']:
        logger.info("adding a draft with the project")
        logger.info("project id: %s", new_proj.id)
        draft = ChatThread(display_name=data['draft-name'],
                   docCategory=data['draft-type'],
                   projectId=new_proj.id,
                   location=None)
    
        db.session.add(draft)
        db.session.flush()
        from backend.util import workspace_store
        workspace_store.initialize(draft.id)
        db.session.commit()
    
    return SUCCESS_JSON, 200


@bp.post('update-project-name')
@cognito_auth_required
def update_project_name():
    """Rename only a matter owned by the current user; keep its ID and contents."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {'msg': 'A JSON object is required.'}, 400
    project_id = data.get('projId')
    if isinstance(project_id, bool) or not isinstance(project_id, (int, str)):
        return {'msg': 'A valid matter ID is required.'}, 400
    try:
        project_id = int(project_id)
    except (TypeError, ValueError):
        return {'msg': 'A valid matter ID is required.'}, 400
    if project_id <= 0:
        return {'msg': 'A valid matter ID is required.'}, 400
    name = data.get('projectName')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 250:
        return {'msg': 'Matter name must contain 1–250 characters.'}, 400
    name = name.strip()
    project = Project.query.filter_by(id=project_id, userId=current_user.id).first()
    if project is None:
        return {'msg': 'Matter not found.'}, 404
    duplicate = Project.query.filter_by(name=name, userId=current_user.id).filter(Project.id != project_id).first()
    if duplicate is not None:
        return {'msg': 'A matter with that name already exists.'}, 409
    if project.name != name:
        try:
            project.name = name
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception('Unable to rename matter %s', project_id)
            return {'msg': 'Unable to rename matter. Please try again.'}, 500
    return {'msg': 'Matter renamed.', 'projId': project.id, 'projectName': project.name}, 200


@bp.post('list-projects')
@cognito_auth_required
def list_all_projects():
    all_projs = Project.query.filter_by(userId=current_user.id).all()
    proj_details = map(lambda x: [x.name, x.id], all_projs)
    return {'projects': list(proj_details)}, 200


@bp.post('create-draft')
@cognito_auth_required
def create_draft():
    """Creates a draft for a given project id, the user must own that project.
    The proj-id, draft-name, and draft-type need to be given."""
    data = request.get_json() or {}
    for term in ['proj-id', 'draft-name', 'draft-type']:
        if term not in data or data[term] is None:
            return {'msg': f'missing {term}'}, 401
        if isinstance(data[term], str) and not data[term].strip():
            return {'msg': f'missing {term}'}, 401

    draft_name = data['draft-name'].strip()

    # the the project 
    proj_check = Project.query.filter_by(id=data['proj-id']).first()
    if proj_check is None:
        return {'msg': 'This Project does not exist'}, 401
    if proj_check.userId != current_user.id:
        return {'msg': 'You do not own this project'}, 401
    
    draft = ChatThread(display_name=draft_name,
                   docCategory=data['draft-type'],
                   projectId=data['proj-id'],
                   location=None)
    
    db.session.add(draft)
    db.session.flush()
    from backend.util import workspace_store
    workspace_store.initialize(draft.id)
    identity = workspace_store.load(draft.id)
    db.session.commit()

    if str(data['draft-type']) == '69':
        ensure_task_analytics_row(draft.id, user_id=current_user.id)

    return {'msg': 'Draft created', 'draftId': draft.public_id, 'draftName': draft.display_name,
        'chat_thread_id': identity['chat_thread_id'], 'draft_document_id': identity['draft_document_id'],
        'draft_version': identity['draft_version']}, 200

@bp.post('update-draft-name')
@cognito_auth_required
def update_draft_name():
    """Updates the display name of a draft. The user must own the draft."""
    data = request.get_json()
    if 'draftId' not in data or 'draftName' not in data:
        return {'msg': 'missing draftId or draftName'}, 401

    draft = ChatThread.query.filter_by(public_id=data['draftId']).first()
    if draft is None:
        return {'msg': 'Draft does not exist'}, 404
    if draft.parent.userId != current_user.id:
        return {'msg': 'You do not own this draft'}, 401

    draft.display_name = data['draftName']
    db.session.commit()

    return SUCCESS_JSON, 200

@bp.get('/draft-name/<string:draftId>')
@cognito_auth_required
def get_draft_name(draftId):
    """Returns basic draft metadata if the user owns it."""
    chk = ownership_check(d={'draftId': draftId}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft
    return {
        'draftId': draft.public_id,
        'draftName': draft.display_name,
        'projectId': draft.projectId,
        'projectName': draft.parent.name if draft.parent else None,
        'docCategory': draft.docCategory,
    }, 200

@bp.post('list-drafts')
@cognito_auth_required
def list_all_drafts():
    """List all drafts for a given project.  The user must own the project."""
    data = request.get_json()
    logger.info("list_all_drafts data: %s", data)
    if 'proj-id' not in data:
        return {'msg': 'missing proj-id'}, 401

    proj_check = Project.query.filter_by(id=data['proj-id']).first()
    if proj_check is None:
        return {'msg': 'This Project does not exist'}, 401
    if proj_check.userId != current_user.id:
        return {'msg': 'You do not own this project'}, 401
    
    all_drafts = ChatThread.query.filter_by(projectId=data['proj-id']).all()
    draft_details = map(lambda x: [x.public_id, x.display_name, x.location, x.docCategory], all_drafts)
    return {'drafts': list(draft_details)}, 200


@bp.post('deletedraft')
@cognito_auth_required
def delete_draft():
    data = request.get_json()

    if 'draftId' not in data:
        return {'msg': 'No draftId in request'}, 401
    
    draft = ChatThread.query.filter_by(public_id=data['draftId']).first()
    if not draft:
        return {'msg': 'Draft not found'}, 404
    if current_user.id != draft.parent.userId:
        return {'msg': 'Current user does not own this draft'}, 401

    # Also delete any uploaded files associated with this draft
    # Note: TextEntry rows are already configured to cascade delete from ChatThread
    for f in UploadedFiles.query.filter_by(chat_thread_id=draft.id).all():
        db.session.delete(f)

    db.session.delete(draft)
    db.session.commit()

    return SUCCESS_JSON, 200

@bp.post('deleteproject')
@cognito_auth_required
def delete_project():
    data = request.get_json() or {}

    if 'projId' not in data:
        return {'msg': 'No projId in request'}, 401

    try:
        proj_id = int(data['projId'])
    except (TypeError, ValueError):
        return {'msg': 'Invalid projId'}, 400

    proj = Project.query.filter_by(id=proj_id).first()
    if not proj:
        return {'msg': 'Project not found'}, 404

    if current_user.id != proj.userId:
        return {'msg': 'Current user does not own this project'}, 401

    try:
        # Defensive cleanup for smart-search tags. These rows are not part of the
        # legacy delete path and can otherwise block project deletion in some DBs.
        file_ids_for_project = db.session.query(UploadedFiles.id).filter(
            UploadedFiles.project_id == proj_id
        )
        folder_ids_for_project = db.session.query(FolderUpload.id).filter(
            FolderUpload.project_id == proj_id
        )

        db.session.query(FileTagAssociation).filter(
            FileTagAssociation.uploaded_file_id.in_(file_ids_for_project)
        ).delete(synchronize_session=False)

        db.session.query(FolderTagAssociation).filter(
            FolderTagAssociation.folder_upload_id.in_(folder_ids_for_project)
        ).delete(synchronize_session=False)

        db.session.query(FileTag).filter(
            FileTag.project_id == proj_id
        ).delete(synchronize_session=False)

        # Delete the project; ORM cascade handles related drafts/files/sessions/chunks.
        db.session.delete(proj)
        db.session.commit()
        return SUCCESS_JSON, 200
    except IntegrityError as exc:
        db.session.rollback()
        current_app.logger.exception(
            "DELETE_PROJECT integrity error for project_id=%s user_id=%s: %s",
            proj_id,
            current_user.id,
            exc,
        )
        return {'msg': 'Unable to delete matter because related records are still linked.'}, 500
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(
            "DELETE_PROJECT unexpected error for project_id=%s user_id=%s: %s",
            proj_id,
            current_user.id,
            exc,
        )
        return {'msg': 'Unable to delete matter. Please try again.'}, 500



@bp.get('opinions/<string:citationName>&<string:clusterId>')
@bp.get('opinions/<string:citationName>')
@cognito_auth_required
def get_clusteropinion_data(citationName, clusterId=None):
    citationNameDecoded = decode_citation_url(citationName)

    # If a draft_id was provided, validate ownership before returning any data.
    draft_id_raw = request.args.get('draft_id')
    if draft_id_raw:
        try:
            doc_id = ChatThread.resolve_id(draft_id_raw)
        except Exception:
            return {'msg': 'Draft not found'}, 404
        draft_for_task = ChatThread.query.get(doc_id)
        if not draft_for_task:
            return {'msg': 'Draft not found'}, 404
        if current_user.id != draft_for_task.parent.userId:
            return {'msg': 'Current user does not own this draft'}, 403

    if not clusterId:
        logger.info("caseviewer no cluster id citation=%s draft_id=%s", citationNameDecoded, draft_id_raw)
        return caseviewer_no_details_response(cluster_id=clusterId, citation=citationNameDecoded), 200

    cached_caseviewer_response, cache_details, cache_row = load_cached_caseviewer_response(clusterId)
    if cached_caseviewer_response:
        return cached_caseviewer_response, 200

    cache_status = "miss" if cache_row is None else "fallback"
    active_task_key = caseviewer_active_task_key(current_user.id, draft_id_raw, clusterId)
    existing_task_id = None
    try:
        existing_task_id_raw = _cancel_redis.get(active_task_key)
        if existing_task_id_raw:
            existing_task_id = (
                existing_task_id_raw.decode()
                if isinstance(existing_task_id_raw, (bytes, bytearray))
                else existing_task_id_raw
            )
    except Exception:
        logger.exception("caseviewer active task read failed cluster_id=%s key=%s", clusterId, active_task_key)

    if existing_task_id:
        try:
            existing_result = AsyncResult(existing_task_id)
            if not existing_result.ready():
                logger.info(
                    "caseviewer task reused task_id=%s cluster_id=%s draft_id=%s cache_status=%s reason=%s",
                    existing_task_id,
                    clusterId,
                    draft_id_raw,
                    cache_status,
                    cache_details.get("reason"),
                )
                return {
                    "async": True,
                    "result_id": existing_task_id,
                    "message": "Case details are being prepared...",
                    "progress": 5,
                    "cache_status": cache_status,
                    "cache_reason": cache_details.get("reason"),
                }, 202
            _cancel_redis.delete(active_task_key)
        except Exception:
            logger.exception(
                "caseviewer active task check failed task_id=%s cluster_id=%s",
                existing_task_id,
                clusterId,
            )

    task_payload = {
        "cluster_id": clusterId,
        "citation_name_decoded": citationNameDecoded,
        "draft_id": draft_id_raw,
        "user_id": current_user.id,
        "active_task_key": active_task_key,
    }
    result = caseviewer_enrichment_task.apply_async(args=[task_payload])
    try:
        _cancel_redis.set(active_task_key, result.id, ex=CASEVIEWER_ACTIVE_TASK_TTL_SECONDS)
        _cancel_redis.set(
            f"task_meta:{result.id}",
            json.dumps({
                "task_type": "caseviewer",
                "user_id": current_user.id,
                "draft_id": draft_id_raw,
                "cluster_id": clusterId,
                "citation": citationNameDecoded,
                "active_task_key": active_task_key,
            }),
            ex=CASEVIEWER_ACTIVE_TASK_TTL_SECONDS,
        )
    except Exception:
        logger.exception("caseviewer active task metadata save failed task_id=%s cluster_id=%s", result.id, clusterId)

    logger.info(
        "caseviewer task enqueued task_id=%s cluster_id=%s draft_id=%s cache_status=%s reason=%s",
        result.id,
        clusterId,
        draft_id_raw,
        cache_status,
        cache_details.get("reason"),
    )
    return {
        "async": True,
        "result_id": result.id,
        "message": "Case details are being prepared...",
        "progress": 5,
        "cache_status": cache_status,
        "cache_reason": cache_details.get("reason"),
    }, 202


@bp.post('getprofile')
@cognito_auth_required
def get_profile():
    user = User.query.filter_by(id=current_user.id).first()

    return {'contact_info': user.contact_info}, 200


@bp.post('updateprofile')
@cognito_auth_required
def update_profile():
    data = request.get_json()

    if 'contactInfo' not in data:
        return {'msg': 'No contactInfo in request'}, 401
    
    user = User.query.filter_by(id=current_user.id).first()
    user.contact_info = data['contactInfo']

    db.session.add(user)
    db.session.commit()

    return SUCCESS_JSON, 200


def ownership_check(d: dict, jwt_id: int):
    """Authorize a chat, accepting ``draftId`` only as its legacy identity alias.

    A separately supplied document must belong to that same chat. Existing
    callers keep _last_draft; new code may use the explicit _last_chat_thread.
    """
    if not isinstance(d, dict) or not any(d.get(key) is not None for key in ('draftId', 'chat_thread_id')):
        return {'msg': 'No draftId in request'}, 401
    from backend.util.case_assistant_workflow import resolve_chat_identity
    try:
        chat_id = resolve_chat_identity(d)
    except ValueError as exc:
        if 'different chats' in str(exc) or 'Invalid chat' in str(exc):
            return {'code': 'chat_identity_conflict', 'msg': str(exc)}, 400
        return {'msg': 'Draft not found'}, 404
    draft = db.session.get(ChatThread, chat_id)
    if not draft:
        return {'msg': 'Draft not found'}, 404
    if jwt_id != draft.parent.userId:
        return {'msg': 'Current user does not own this draft'}, 401

    # Store the draft so callers can access draft.id for internal operations
    ownership_check._last_draft = draft
    ownership_check._last_chat_thread = draft
    # JSON callers pass a mutable dict; existing guided upload callers pass
    # immutable request.form and retain their original legacy field names.
    if type(d) is dict:
        d['draftId'] = d['chat_thread_id'] = draft.public_id
    if 'draft_document_id' in d:
        if not isinstance(d['draft_document_id'], str) or not d['draft_document_id'].strip():
            return {'code': 'invalid_document_identity', 'msg': 'Invalid draft_document_id.'}, 400
        from backend.util import workspace_store
        try:
            document = workspace_store.document_for_chat(draft.id, d['draft_document_id'])
            if document.public_id != d['draft_document_id']:
                raise ValueError('Document not found')
        except ValueError:
            return {'code': 'document_not_found', 'msg': 'Document not found in this chat.'}, 404
    return None


def _clear_standalone_hg_state(mem: DBMemory):
    for key in (
        'hallucination_check',
        'standalone_hg/results', 'standalone_hg/results_html',
        'standalone_hg/case_results', 'standalone_hg/case_results_html',
        'standalone_hg/statute_results', 'standalone_hg/statute_results_html',
        'standalone_hg/filtered_citations', 'standalone_hg/lookup_diagnostics',
        'standalone_hg/error_message', 'standalone_hg/token_limit',
        'standalone_hg/current_tokens', 'standalone_hg/prompt_token_counts',
        STANDALONE_HG_ACTIVE_UPLOAD_TASK_KEY, STANDALONE_HG_ACTIVE_HG_TASK_KEY,
    ):
        try:
            del mem[key]
        except Exception:
            pass

    # Standalone HG is single-document scoped, so reset validated cases whenever
    # the uploaded source file is replaced or deleted.
    try:
        state = load_state_from_memory(mem)
        state['validated_cases'] = {}
        save_state_to_memory(state, mem, auxiliary_fields=('validated_cases',))
    except Exception:
        pass


def _standalone_hg_uploaded_file_payload(uploaded_file: UploadedFiles):
    return {
        'unique_id': uploaded_file.unique_id,
        'filename': uploaded_file.original_filename,
        'file_size': uploaded_file.file_size,
        'upload_time': uploaded_file.upload_time.isoformat() if uploaded_file.upload_time else None,
    }


def _standalone_hg_default_run_name(filename: str) -> str:
    base_name = os.path.splitext(os.path.basename(filename or ""))[0].strip()
    return base_name or 'Hallucination Guard'


def _process_standalone_hg_upload(file, draft_obj):
    if file.filename == '':
        return {'msg': 'No file selected'}, 400
    if not allowed_filetypes(file.filename):
        return {'msg': 'Unsupported file type. Please upload a .pdf, .docx, .doc, or .txt file.'}, 400

    existing_file = UploadedFiles.query.filter_by(chat_thread_id=draft_obj.id).first()
    if existing_file is not None:
        return {'msg': 'Only one document is allowed per Hallucination Guard run. Delete the current file before uploading a new one.'}, 400

    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)
    if file_size > STANDALONE_HG_MAX_FILE_SIZE:
        return {'msg': f'File too large. Maximum size is 15 MB per file (received {file_size / (1024*1024):.1f}MB)'}, 400

    unique_id = f"{uuid.uuid4()}_{file.filename}"
    temp_filename = f"temp_{unique_id}"
    temp_path = os.path.join("backend/static", temp_filename)
    file.save(temp_path)

    try:
        parsed_text, _, _ = get_complaint_text_from_file(temp_path)
        ocr_warning = get_last_ocr_warning()

        if file.filename.lower().endswith('.pdf'):
            try:
                page_count = get_pagenum_from_pdf_fitz(temp_path)
            except Exception:
                words = len(parsed_text.split()) if parsed_text else 0
                page_count = max(1, round(words / 400)) if words else 0
        else:
            words = len(parsed_text.split()) if parsed_text else 0
            page_count = max(1, round(words / 400)) if words else 0

        uploaded_file = UploadedFiles(
            unique_id=unique_id,
            original_filename=file.filename,
            file_size=os.path.getsize(temp_path),
            page_count=page_count,
            content=parsed_text,
            draft_id=draft_obj.id,
        )
        db.session.add(uploaded_file)
        db.session.commit()

        chunks = chunk_text(parsed_text)
        if chunks:
            for c in chunks:
                db.session.add(DocumentChunk(
                    uploaded_file_id=uploaded_file.id,
                    draft_id=draft_obj.id,
                    chunk_index=c['chunk_index'],
                    content=c['content'],
                    token_count=c['token_count'],
                    char_count=c['char_count'],
                    source_filename=file.filename,
                    source_relative_path=None,
                    source_unique_id=unique_id,
                ))
            db.session.commit()

        mem = DBMemory(db=db, doc_id=draft_obj.id)
        mem[f'inputs/{unique_id}'] = parsed_text
        mem['inputs/uploaded_filename'] = file.filename
        _clear_standalone_hg_state(mem)

        os.remove(temp_path)
        return {
            'msg': 'File uploaded successfully',
            'uploaded_file': _standalone_hg_uploaded_file_payload(uploaded_file),
            'ocr_warning': ocr_warning,
            'ocr_warning_message': "Some scanned pages could not be read. Your document is still processed. Press OK to continue.",
        }, 200
    except Exception as e:
        db.session.rollback()
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return {'msg': f'Error processing file: {str(e)}'}, 500


@bp.post('research')
@cognito_auth_required
def research_api():
    data = request.get_json()
    if 'query' not in data:
        return {'msg': 'No query in request'}, 401

    analytics_record_research(db, current_user.id)
    data['user_id'] = current_user.id
    result = research_task.delay(data)
    return {"result_id": result.id}, 200


@bp.post('/analytics')
@cognito_auth_required
@cognito_group_permissions(['admins'])#@cognito_auth_required
def analytics_api():
    # Get pagination parameters from query string (default page=1, per_page=50)
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    analytics_view = request.args.get('view', 'steps')

    if analytics_view == 'tasks':
        query = db.session.query(TaskAnalytics, User.email) \
            .outerjoin(User, TaskAnalytics.userId == User.id) \
            .order_by(TaskAnalytics.updatedAt.desc())
        total = db.session.query(func.count(TaskAnalytics.id)).scalar()
        results = query.offset((page - 1) * per_page).limit(per_page).all()
        return jsonify({
            'view': 'tasks',
            'page': page,
            'per_page': per_page,
            'total': total,
            'results': [{
                'id': ta.id,
                'createdAt': ta.createdAt.isoformat() if ta.createdAt else None,
                'updatedAt': ta.updatedAt.isoformat() if ta.updatedAt else None,
                'userId': ta.userId,
                'email': email,
                'draftId': ta.draftId,
                'draftTitle': ta.draftTitle,
                'promptsSent': ta.promptsSent,
                'answersReturned': ta.answersReturned,
                'llmCallCount': ta.llmCallCount,
                'totalTokens': ta.totalTokens,
                'hallucinationGuardAttempts': ta.hallucinationGuardAttempts,
                'hallucinationGuardSuccesses': ta.hallucinationGuardSuccesses,
                'hallucinationGuardFailures': ta.hallucinationGuardFailures,
                'hallucinationGuardLlmCallCount': ta.hallucinationGuardLlmCallCount,
                'hallucinationGuardTokens': ta.hallucinationGuardTokens,
                'downloadAttempts': ta.downloadAttempts,
                'downloadSuccesses': ta.downloadSuccesses,
                'downloadFailures': ta.downloadFailures,
                'downloadLlmCallCount': ta.downloadLlmCallCount,
                'downloadTokens': ta.downloadTokens,
            } for ta, email in results],
        })

    if analytics_view != 'steps':
        return jsonify({'error': 'Unknown analytics view'}), 400

    # Query StepAnalytics joined with User (outer join in case there's no matching user)
    query = db.session.query(StepAnalytics, User.email) \
        .outerjoin(User, StepAnalytics.userId == User.id) \
        .order_by(StepAnalytics.date.desc())

    # Get total count for pagination metadata.
    total = db.session.query(func.count(StepAnalytics.id)).scalar()

    # Apply pagination: skip the records before this page and limit the number of rows.
    results = query.offset((page - 1) * per_page).limit(per_page).all()
    result_ids = [step_row.id for step_row, _email in results]
    model_usage_by_step = {step_id: [] for step_id in result_ids}
    if result_ids:
        usage_rows = StepLlmModelUsage.query.filter(
            StepLlmModelUsage.stepAnalyticsId.in_(result_ids)
        ).order_by(
            StepLlmModelUsage.stepAnalyticsId,
            StepLlmModelUsage.model,
        ).all()
        for usage in usage_rows:
            model_usage_by_step[usage.stepAnalyticsId].append({
                'model': usage.model,
                'llmCallCount': usage.llmCallCount,
                'llmSuccessCount': usage.llmSuccessCount,
                'llmFailureCount': usage.llmFailureCount,
                'totalTokens': usage.totalTokens,
            })

    # Serialize the data.
    data = {
        'page': page,
        'per_page': per_page,
        'total': total,
        'results': [{
            'id': sa.id,
            'date': sa.date.isoformat() if sa.date else None,
            'email': email,
            'draftId': sa.draftId,
            'docType': sa.docType,
            'step': sa.step,
            'status': sa.status,
            'completedAt': sa.completedAt.isoformat() if sa.completedAt else None,
            'llmCallCount': sa.llmCallCount,
            'llmSuccessCount': sa.llmSuccessCount,
            'llmFailureCount': sa.llmFailureCount,
            'totalTokens': sa.totalTokens,
            'modelUsage': model_usage_by_step.get(sa.id, []),
        } for sa, email in results]
    }
    return jsonify(data)


@bp.get('/dashboard')
@cognito_auth_required
@cognito_group_permissions(['admins'])
def dashboard_api():
    return build_dashboard_api_response()


def _sync_editor_draft_title(draft_id, draft):
    from backend import everything
    title = str(draft.get('title') or '').strip() or 'Untitled document'
    everything.sync_task_analytics_draft_title(draft_id,
        title if everything.has_substantive_workspace_draft(draft) else None)


@bp.post('update_section_text')
@cognito_auth_required
def update_section_text():
    from backend.util import workspace_store
    data = request.get_json() or {}
    if not isinstance(data, dict):
        return {'code': 'draft_version_required', 'msg': 'A draft save must be a JSON object.'}, 400
    expected = data.get('expected_draft_version')
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        return {'code': 'draft_version_required', 'msg': 'Refresh the draft before saving.'}, 400
    if 'draft' in data and not isinstance(data['draft'], dict):
        return {'code': 'invalid_draft', 'msg': 'Draft content must be a JSON object.'}, 400
    chk = ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft

    # Lock before reading the draft to mutate, rather than locking a stale
    # workspace snapshot only when it is about to overwrite newer chat state.
    state = workspace_store.load(draft.id, lock=True)

    # New path: full draft structure provided from frontend
    if 'draft' in data:
        from backend.everything import save_draft_to_state
        save_draft_to_state(state, data['draft'])
        logger.info("STATE: Replaced 'draft' from frontend payload.")
    else:
        # Legacy path: update a single section by name
        section = data['sectionName']
        text = data['text']
        original_section = data.get('originalSectionName', section)

        current_draft = state.get('draft') or {}
        sections_list = current_draft.get('sections', []) if isinstance(current_draft, dict) else []

        # If section name changed, rename within sections_list preserving order
        if original_section != section:
            renamed = False
            for sec in sections_list:
                if sec.get('section_name') == original_section:
                    sec['section_name'] = section
                    sec['section_content'] = text
                    renamed = True
                    break
            if not renamed:
                # Fallback: just add as new section
                from backend.everything import add_section
                add_section(state, section, text)
        else:
            # Just update the content for existing section (or add if missing)
            from backend.everything import add_section
            add_section(state, section, text)

    # Clear pending review snapshot — unless frontend asks to keep it (per-section resolution)
    if not data.get('keepDraftBefore', False):
        state.pop('draft_before', None)

    try:
        version = workspace_store.update_document(draft.id, data.get('draft_document_id', state['draft_document_id']),
            state['draft'], expected_version=expected,
            draft_before=state.get('draft_before'))
        db.session.commit()
    except workspace_store.WorkspaceConflict as exc:
        db.session.rollback()
        return {'code': 'draft_conflict', 'draft_version': exc.current_version,
            'msg': 'The draft changed elsewhere. Your unsaved edits have been kept. Reload the current draft before saving again.'}, 409
    _sync_editor_draft_title(draft.id, state['draft'])
    return {'draft': state.get('draft', {}), 'draft_version': version,
        'chat_thread_id': state['chat_thread_id'], 'draft_document_id': state['draft_document_id']}, 200


@bp.post('delete_draft_section')
@cognito_auth_required
def delete_draft_section():
    from backend.util import workspace_store
    data = request.get_json() or {}
    if not isinstance(data, dict):
        return {'code': 'draft_version_required', 'msg': 'A draft save must be a JSON object.'}, 400
    expected = data.get('expected_draft_version')
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        return {'code': 'draft_version_required', 'msg': 'Refresh the draft before saving.'}, 400
    chk = ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft

    section_id = data['sectionId']

    state = workspace_store.load(draft.id, lock=True)

    # Use helper function to remove section by section_id
    remove_section(state, section_id)

    try:
        version = workspace_store.update_document(draft.id, data.get('draft_document_id', state['draft_document_id']),
            state['draft'], expected_version=expected,
            draft_before=state.get('draft_before'))
        db.session.commit()
    except workspace_store.WorkspaceConflict as exc:
        db.session.rollback()
        return {'code': 'draft_conflict', 'draft_version': exc.current_version,
            'msg': 'The draft changed elsewhere. Your unsaved edits have been kept. Reload the current draft before saving again.'}, 409
    _sync_editor_draft_title(draft.id, state['draft'])
    return {'draft': state.get('draft', {}), 'draft_version': version,
        'chat_thread_id': state['chat_thread_id'], 'draft_document_id': state['draft_document_id']}, 200


@bp.post('get_document')
@cognito_auth_required
def get_document():
    """Read one coherent draft/conversation projection, then add workflow status."""
    data = request.get_json()
    chk = ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft = ownership_check._last_draft

    from backend.everything import OperationMemory
    from backend.util import workspace_store
    # Publication takes this same lock while updating the draft and its turn.
    # Normalize the captured view privately, then release the short read lock
    # before polling workflow status (which takes its own workflow-row locks).
    snapshot = workspace_store.load(draft.id, lock=True)
    state = load_state_from_memory(OperationMemory(state=json.dumps(snapshot)))
    db.session.commit()

    result = {
        'chat_thread_id': state['chat_thread_id'],
        'draft_document_id': state['draft_document_id'],
        'draft': state.get('draft', {'title': '', 'sections': []}),
        'draft_version': state['draft_version'],
        'selected_section': state.get('selected_section'),
        'chatHistory': state['chat_history'],
        'chatHistoryMeta': state.get('chat_history_meta', []),
    }
    from backend.models import CaseAssistantWorkflow
    from backend.util import case_assistant_workflow as workflows
    active_request = CaseAssistantWorkflow.query.filter_by(active_chat_thread_id=draft.id, user_id=current_user.id).first()
    if active_request:
        result['activeWorkflow'] = workflows.status(active_request)
        if active_request.status in ('running', 'awaiting_user_confirmation'):
            original = json.loads(active_request.data_json)
            result['pendingTurn'] = {'input': original.get('display_user_message') or original['query'],
                'response': '', 'draftId': draft.public_id, 'chat_thread_id': draft.public_id,
                'draft_document_id': state['draft_document_id'], 'workflowRunId': active_request.id,
                'promptAttachments': original.get('promptAttachments', [])}
            if active_request.execution_version == 1:
                result['pendingTurn']['agentRun'] = True
                if active_request.status == 'awaiting_user_confirmation':
                    pending = json.loads(active_request.result_json)
                    result['pendingTurn'].update(awaitingConfirmation=True,
                        confirmation=pending['confirmation'], response=pending['confirmation']['message'])
    # Failed requests and cancelled agent requests survive reload, including
    # stops before a chat answer existed. These are display overlays, not answers.
    history, history_meta = list(result['chatHistory']), list(result['chatHistoryMeta'])
    history_meta.extend({} for _ in range(max(0, len(history)-len(history_meta))))
    known = {meta.get('workflow_run_id') for meta in history_meta if isinstance(meta, dict)}
    request_times = {uid: (created_at, uid) for uid, created_at in db.session.query(
        CaseAssistantWorkflow.id, CaseAssistantWorkflow.created_at).filter_by(
            draft_id=draft.id, user_id=current_user.id).all()}
    stopped_requests = CaseAssistantWorkflow.query.filter_by(chat_thread_id=draft.id, user_id=current_user.id).filter(
        CaseAssistantWorkflow.status.in_(('failed', 'cancelled'))).order_by(
            CaseAssistantWorkflow.created_at, CaseAssistantWorkflow.id).all()
    for stopped in stopped_requests:
        cancelled = stopped.status == 'cancelled'
        if cancelled and stopped.execution_version != 1:
            continue
        metadata = {'workflow_run_id': stopped.id, 'workflow_failed': not cancelled,
                    'workflow_cancelled': cancelled, 'agent_run': stopped.execution_version == 1,
                    'extraction_report': stopped.id}
        if stopped.id not in known:
            original = json.loads(stopped.data_json)
            # Keep saved turns in place; restore a stopped request before the next
            # known later request, with its metadata at the same position.
            position = next((index for index, meta in enumerate(history_meta)
                if isinstance(meta, dict) and meta.get('workflow_run_id') in request_times
                and request_times[meta['workflow_run_id']] > (stopped.created_at, stopped.id)), len(history))
            message = 'Request cancelled.' if cancelled else stopped.error_message or 'Request failed. Retry to resume saved results.'
            history.insert(position, [original.get('display_user_message') or original['query'], message])
            history_meta.insert(position, {**metadata, 'prompt_attachments': original.get('promptAttachments', [])})
        else:
            for meta in history_meta:
                if meta.get('workflow_run_id') == stopped.id:
                    meta.update(metadata)
    result['chatHistory'], result['chatHistoryMeta'] = history, history_meta
    workflow = state.get("active_workflow") if isinstance(state.get("active_workflow"), dict) else {}
    pending_turn = workflow.get("pending_turn") if isinstance(workflow.get("pending_turn"), dict) else None
    if pending_turn and workflow.get("status") in {"running", "awaiting_user_confirmation"} and active_request and workflow.get('workflow_run_id') == active_request.id:
        result["pendingTurn"] = pending_turn
    # Include pending review snapshot so comparison mode survives page reload
    if 'draft_before' in state:
        result['draft_before'] = state['draft_before']

    return result, 200


@bp.post('/everything')
@cognito_auth_required
def everything_api():
    data = request.get_json() or {}
    if 'query' not in data:
        return {'msg': 'No query in request'}, 401

    chk = ownership_check(d=data, jwt_id=current_user.id)
    if chk:
        return chk

    from backend.util import document_preparation as preparation
    if preparation.enabled():
        try:
            data['document_snapshot'] = preparation.build_snapshot(current_user.id, data.get('draftId'), data)
            db.session.commit()
        except preparation.PreparationError as exc:
            db.session.rollback()
            return exc.payload, exc.status
        except OperationalError:
            db.session.rollback()
            return {'code': 'preparation_unavailable', 'msg': 'Document preparation is unavailable. Check database migrations and retry.'}, 503

    data['user_id'] = current_user.id  # pass to Celery tasks

    from backend.util import case_assistant_workflow as workflows
    from backend.case_assistant_tasks import dispatch_planning
    from sqlalchemy.exc import IntegrityError
    try:
        # Persist the accepted prompt/scope before publishing any Celery work.
        # New version-1 advance tasks all refer to this SQL row; saved version-0
        # requests keep their old planning/extraction/answer path. The HTTP
        # request returns promptly instead of waiting for either workflow.
        workflow = workflows.submit(current_user.id, data)
    except (ValueError, IntegrityError) as exc:
        db.session.rollback()
        return {'msg': str(exc) if isinstance(exc, ValueError) else 'Another request is active in this chat.'}, 409
    except OperationalError:
        db.session.rollback()
        return {'code': 'workflow_unavailable', 'msg': 'Document processing is unavailable. Apply database migrations and retry.'}, 503
    if workflow.dispatch_pending and workflow.payload_json is None:
        dispatch_planning(workflow)
    if preparation.enabled():
        preparation.mark_snapshot_committed(data['document_snapshot'])
    draft = ownership_check._last_draft
    increment_task_analytics(
        draft.id,
        {"promptsSent": 1},
        user_id=current_user.id,
    )

    # Return the SQL workflow ID, not the planning/answer Celery ID. The frontend
    # polls saved status, including agent steps/questions (legacy: extraction),
    # through case_assistant_routes.progress (also adapted by /result/wf_...).
    from backend.util import workspace_store
    document = workspace_store.document_for_chat(workflow.chat_thread_id, workflow.draft_document_id)
    return {'result_id': workflow.id, 'workflow_run_id': workflow.id,
        'chat_thread_id': draft.public_id, 'draft_document_id': document.public_id}, 200


@bp.post('/everything/confirm-workflow')
@cognito_auth_required
def everything_confirm_workflow_api():
    data = request.get_json() or {}
    draft_id_raw = data.get("draftId")
    workflow_run_id = data.get("workflow_run_id")
    decision = data.get("decision") if isinstance(data.get("decision"), dict) else {}
    if not draft_id_raw or not workflow_run_id:
        return {"msg": "Missing draftId or workflow_run_id"}, 400

    from backend.models import CaseAssistantWorkflow
    if db.session.get(CaseAssistantWorkflow, workflow_run_id):
        from backend.case_assistant_routes import confirm
        return confirm.__wrapped__(workflow_run_id)

    chk = ownership_check(d={'draftId': draft_id_raw}, jwt_id=current_user.id)
    if chk:
        return chk

    doc_id = ChatThread.resolve_id(draft_id_raw)
    if decision.get("cancelled") or decision.get("draft_action") == "cancel":
        mem = DBMemory(db=db, doc_id=doc_id)
        state = load_state_from_memory(mem)
        workflow = state.get("active_workflow") if isinstance(state.get("active_workflow"), dict) else {}
        if workflow.get("workflow_run_id") != workflow_run_id:
            return {"msg": "Workflow confirmation is no longer active"}, 409
        update_active_workflow(state, status="cancelled", phase="finalize", pending_confirmation=None, pending_turn=None)
        state.pop("active_task_id", None)
        save_state_to_memory(state, mem, auxiliary_fields=('active_workflow', 'active_task_id'))
        return {"cancelled": True}, 200

    from backend.util import document_preparation as preparation
    if preparation.enabled():
        state = load_state_from_memory(DBMemory(db=db, doc_id=doc_id))
        workflow = state.get('active_workflow') or {}
        pending = workflow.get('pending_confirmation') or {}
        snapshot = (pending.get('original_turn_input') or {}).get('document_snapshot')
        if workflow.get('workflow_run_id') != workflow_run_id or snapshot is None:
            return {'code': 'stale_scope', 'msg': 'Refresh the request before confirming.'}, 409
        try:
            preparation.validate_snapshot(current_user.id, draft_id_raw, snapshot)
        except preparation.PreparationError as exc:
            db.session.rollback()
            return exc.payload, exc.status

    result = everything_confirmed_workflow_task.apply_async(args=[{
        "draftId": draft_id_raw,
        "workflow_run_id": workflow_run_id,
        "decision": decision,
        "user_id": current_user.id,
    }])
    _cancel_redis.set(
        f"task_meta:{result.id}",
        json.dumps({
            "task_type": "everything",
            "draft_id": draft_id_raw,
            "user_id": current_user.id,
        }),
        ex=CASE_ASSISTANT_TASK_META_TTL,
    )
    return {"result_id": result.id}, 200


@bp.post('/hallucination_guard')
@cognito_auth_required
def hallucination_guard_api():
    """Run the hallucination guard on the citations in the specified draft.

    Requires draftId in the request body — used to load the draft's session state
    (validated_cases cache + current draft content) from DBMemory.
    """
    data = request.get_json()
    draft_id_raw = data.get('draftId')
    if not draft_id_raw:
        return {'msg': 'draftId required'}, 400

    chk = ownership_check(d={'draftId': draft_id_raw}, jwt_id=current_user.id)
    if chk:
        return chk

    try:
        doc_id = ChatThread.resolve_id(draft_id_raw)
    except Exception:
        return {'msg': 'Invalid draftId'}, 400

    analytics_record(db, doc_id, EVERYTHING_DOC_TYPE, HALLUCINATION_GUARD_STEP)
    increment_task_analytics(doc_id, {"hallucinationGuardAttempts": 1})
    try:
        result = hallucination_guard_task.apply_async(args=[{'doc_id': doc_id}])
    except Exception:
        increment_task_analytics(doc_id, {"hallucinationGuardFailures": 1})
        raise
    return {'result_id': result.id}, 200


# ---------------------------------------------------------------------------
# Case Assistant — task-specific file uploads (per chat / draft)
# ---------------------------------------------------------------------------
# These files are scoped to a Draft (draft_id), not the whole matter. They
# power composer attachments and phase-1/phase-2 context for a single Case
# Assistant task.
#
# Upload flow:
#   POST /everything/upload-files-async  → stage to disk, enqueue Celery per file
#   GET  /result/<task_id>               → poll until parse/chunk/DBMemory ready
#
# Wired in frontend: EverythingDrafter.js → enqueueCaseAssistantUploads()
#   Supports multiple files in one request (no folder picker in UI today).
#
# List / delete:
#   POST /get-uploaded-files-everything, POST /delete-uploaded-file-everything
# ---------------------------------------------------------------------------

@bp.post('/everything/upload-files-async')
@cognito_auth_required
@_durable_upload('chat')
def upload_files_everything_async():
    """
    **Case Assistant — primary upload route (async).**

    Accepts one multipart request with multiple task-specific files for a single
    draft/chat. Does *not* run OCR or parsing in the gunicorn worker.

    Request (multipart/form-data):
      - draftId: public or internal draft id (ownership-checked)
      - files: one or more uploaded files
      - clientUploadIds: JSON array parallel to files (frontend chip ids)

    Per accepted file:
      1) Validate size (50 MB), draft file-count cap (100 files), and 1 GB total.
      2) Save bytes to backend/static/temp_<uuid>_<name>.
      3) Enqueue case_assistant_upload_file_task (Celery) with temp path + ids.
      4) Store task_meta in Redis for cancel authorization.

    Response 202: { uploads: [{ clientUploadId, unique_id, filename, task_id }], failed: [...] }
    Response 400: no files queued, over file limit, or missing draftId.

    The accepted document ID is reused by the worker. The frontend polls task
    completion, then aggregate file metadata until summary status is ready;
    parsing completion alone does not turn a composer chip ready.

    Timeout note: HTTP returns quickly after staging; heavy work runs in Celery.
    nginx/gunicorn still subject to client_body_timeout during multipart receive.
    """
    draft_id = request.form.get('draftId')
    if not draft_id:
        return {'msg': 'No draftId provided'}, 400

    chk = ownership_check(d={'draftId': draft_id}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft_obj = ownership_check._last_draft
    internal_draft_id = draft_obj.id

    files = request.files.getlist('files')
    if not files:
        return {'msg': 'No files uploaded'}, 400

    raw_client_ids = request.form.get('clientUploadIds')
    client_ids = []
    if raw_client_ids:
        try:
            parsed = json.loads(raw_client_ids)
            if isinstance(parsed, list):
                client_ids = [str(x) if x is not None else '' for x in parsed]
        except Exception:
            client_ids = []
    while len(client_ids) < len(files):
        client_ids.append(str(uuid.uuid4()))

    MAX_FILES_PER_DRAFT = CASE_ASSISTANT_MAX_FILES
    existing_file_count = UploadedFiles.query.filter_by(chat_thread_id=internal_draft_id).count()
    if existing_file_count + len(files) > MAX_FILES_PER_DRAFT:
        return {
            'msg': f'Too many files. Max {MAX_FILES_PER_DRAFT} per chat. '
                   f'Currently {existing_file_count}, trying to add {len(files)}.'
        }, 400

    existing_bytes = db.session.query(func.coalesce(func.sum(UploadedFiles.file_size), 0)).filter_by(
        draft_id=internal_draft_id).scalar() or 0
    incoming_bytes = 0
    for file in files:
        file.seek(0, os.SEEK_END)
        size = file.tell()
        file.seek(0)
        if file.filename and size <= CASE_ASSISTANT_MAX_FILE_SIZE:
            incoming_bytes += size
    if existing_bytes + incoming_bytes > CASE_ASSISTANT_MAX_TOTAL_BYTES:
        return {'code': 'total_size_limit', 'msg': 'Maximum 1 GB total attachments per chat. Remove files before adding more.'}, 400

    uploads = []
    failed = []

    for idx, file in enumerate(files):
        client_upload_id = client_ids[idx]
        if not file or not file.filename:
            failed.append({
                'clientUploadId': client_upload_id,
                'filename': 'unknown',
                'reason': 'missing_file',
                'error': 'No file provided',
            })
            continue

        file.seek(0, os.SEEK_END)
        file_size = file.tell()
        file.seek(0)
        if file_size > CASE_ASSISTANT_MAX_FILE_SIZE:
            failed.append({
                'clientUploadId': client_upload_id,
                'filename': file.filename,
                'reason': 'file_too_large',
                'error': f'File too large ({file_size / (1024*1024):.1f}MB > {CASE_ASSISTANT_MAX_FILE_SIZE_MB}MB)',
            })
            continue

        safe_filename = os.path.basename(file.filename)
        stage_id = f"{uuid.uuid4()}_{safe_filename}"
        temp_path = os.path.join("backend/static", f"temp_{stage_id}")
        file.save(temp_path)

        payload = {
            'draft_id': draft_obj.public_id,
            'client_upload_id': client_upload_id,
            'unique_id': str(uuid.uuid4()),
            'temp_path': temp_path,
            'filename': safe_filename,
        }

        try:
            result = case_assistant_upload_file_task.delay(payload)
            _cancel_redis.set(
                f"task_meta:{result.id}",
                json.dumps({
                    "task_type": "case_assistant_upload",
                    "user_id": current_user.id,
                    "draft_id": draft_obj.public_id,
                    "client_upload_id": client_upload_id,
                }),
                ex=7200,
            )
            uploads.append({
                'clientUploadId': client_upload_id,
                'filename': safe_filename,
                'task_id': result.id,
                'unique_id': payload['unique_id'],
            })
        except Exception as exc:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            failed.append({
                'clientUploadId': client_upload_id,
                'filename': safe_filename,
                'reason': 'enqueue_error',
                'error': str(exc),
            })

    if not uploads:
        return {'msg': 'No files queued for processing', 'uploads': [], 'failed': failed}, 400
    return {'uploads': uploads, 'failed': failed}, 202


@bp.post('/upload-file-standalone-hg')
@cognito_auth_required
def upload_file_standalone_hg():
    if 'file' not in request.files:
        return {'msg': 'No file uploaded'}, 400

    file = request.files['file']
    draft_id = request.form.get('draftId')

    if not draft_id:
        return {'msg': 'No draftId provided'}, 400
    if file.filename == '':
        return {'msg': 'No file selected'}, 400

    chk = ownership_check(d={'draftId': draft_id}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft_obj = ownership_check._last_draft
    if draft_obj.docCategory != STANDALONE_HG_DRAFT_TYPE:
        return {'msg': 'Draft is not a Hallucination Guard run'}, 400

    return _process_standalone_hg_upload(file, draft_obj)


@bp.post('/standalone-hg/upload-async')
@cognito_auth_required
def upload_standalone_hg_async():
    if 'file' not in request.files:
        return {'msg': 'No file uploaded'}, 400

    file = request.files['file']
    project_id_raw = request.form.get('projectId')
    if not project_id_raw:
        return {'msg': 'No projectId provided'}, 400
    if not file or file.filename == '':
        return {'msg': 'No file selected'}, 400
    if not allowed_filetypes(file.filename):
        return {'msg': 'Unsupported file type. Please upload a .pdf, .docx, .doc, or .txt file.'}, 400

    try:
        project_id = int(project_id_raw)
    except (TypeError, ValueError):
        return {'msg': 'Invalid projectId provided'}, 400

    chk = project_ownership_check(d={'projectId': project_id}, jwt_id=current_user.id)
    if chk is not None:
        return chk

    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)
    if file_size > STANDALONE_HG_MAX_FILE_SIZE:
        return {'msg': f'File too large. Maximum size is 15 MB per file (received {file_size / (1024*1024):.1f}MB)'}, 400

    safe_filename = os.path.basename(file.filename)
    draft_obj = ChatThread(
        display_name=_standalone_hg_default_run_name(safe_filename),
        docCategory=STANDALONE_HG_DRAFT_TYPE,
        projectId=project_id,
        location=None,
    )
    db.session.add(draft_obj)
    db.session.flush()
    from backend.util import workspace_store
    workspace_store.initialize(draft_obj.id)
    db.session.commit()

    stage_id = f"{uuid.uuid4()}_{safe_filename}"
    temp_path = os.path.join("backend/static", f"temp_{stage_id}")
    try:
        file.save(temp_path)
        result = standalone_hg_upload_file_task.delay({
            'draftId': draft_obj.public_id,
            'temp_path': temp_path,
            'filename': safe_filename,
        })
        mem = DBMemory(db=db, doc_id=draft_obj.id)
        mem[STANDALONE_HG_ACTIVE_UPLOAD_TASK_KEY] = result.id
        try:
            _cancel_redis.set(
                f"task_meta:{result.id}",
                json.dumps({
                    "task_type": "standalone_hg_upload",
                    "user_id": current_user.id,
                    "draft_id": draft_obj.public_id,
                }),
                ex=7200,
            )
        except Exception:
            pass
    except Exception as exc:
        db.session.rollback()
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        try:
            db.session.delete(draft_obj)
            db.session.commit()
        except Exception:
            db.session.rollback()
        return {'msg': f'Error starting document upload: {str(exc)}'}, 500

    return {
        'draftId': draft_obj.public_id,
        'draftName': draft_obj.display_name,
        'upload_task_id': result.id,
        'uploaded_filename': safe_filename,
        'show_dev_progress_details': standalone_hg_show_dev_progress_details(),
    }, 202


@bp.post('/standalone-hg/start')
@cognito_auth_required
def start_standalone_hg():
    if 'file' not in request.files:
        return {'msg': 'No file uploaded'}, 400

    file = request.files['file']
    project_id_raw = request.form.get('projectId')
    if not project_id_raw:
        return {'msg': 'No projectId provided'}, 400

    try:
        project_id = int(project_id_raw)
    except (TypeError, ValueError):
        return {'msg': 'Invalid projectId provided'}, 400

    chk = project_ownership_check(d={'projectId': project_id}, jwt_id=current_user.id)
    if chk is not None:
        return chk

    draft_name = _standalone_hg_default_run_name(file.filename)
    draft_obj = ChatThread(
        display_name=draft_name,
        docCategory=STANDALONE_HG_DRAFT_TYPE,
        projectId=project_id,
    )
    db.session.add(draft_obj)
    db.session.flush()
    from backend.util import workspace_store
    workspace_store.initialize(draft_obj.id)
    db.session.commit()

    upload_response, status_code = _process_standalone_hg_upload(file, draft_obj)
    if status_code != 200:
        try:
            DocumentChunk.query.filter_by(chat_thread_id=draft_obj.id).delete(synchronize_session=False)
            UploadedFiles.query.filter_by(chat_thread_id=draft_obj.id).delete(synchronize_session=False)
            db.session.delete(draft_obj)
            db.session.commit()
        except Exception:
            db.session.rollback()
        return upload_response, status_code

    result = standalone_hg_task.delay({"draftId": draft_obj.public_id})
    mem = DBMemory(db=db, doc_id=draft_obj.id)
    mem[STANDALONE_HG_ACTIVE_HG_TASK_KEY] = result.id
    return {
        'draftId': draft_obj.public_id,
        'draftName': draft_obj.display_name,
        'result_id': result.id,
        'uploaded_file': upload_response.get('uploaded_file'),
        'ocr_warning': upload_response.get('ocr_warning', False),
        'ocr_warning_message': upload_response.get('ocr_warning_message'),
    }, 200


@bp.post('/get-uploaded-files-standalone-hg')
@cognito_auth_required
def get_uploaded_files_standalone_hg():
    data = request.get_json() or {}
    if 'draftId' not in data:
        return {'msg': 'No draftId provided'}, 400

    chk = ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft_obj = ownership_check._last_draft
    if draft_obj.docCategory != STANDALONE_HG_DRAFT_TYPE:
        return {'msg': 'Draft is not a Hallucination Guard run'}, 400

    uploaded_file = UploadedFiles.query.filter_by(chat_thread_id=draft_obj.id).order_by(UploadedFiles.upload_time.desc()).first()
    return {'uploaded_file': _standalone_hg_uploaded_file_payload(uploaded_file) if uploaded_file else None}, 200


@bp.post('/delete-uploaded-file-standalone-hg')
@cognito_auth_required
def delete_uploaded_file_standalone_hg():
    data = request.get_json() or {}
    if 'draftId' not in data:
        return {'msg': 'draftId required'}, 400

    chk = ownership_check(d={'draftId': data['draftId']}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft_obj = ownership_check._last_draft
    if draft_obj.docCategory != STANDALONE_HG_DRAFT_TYPE:
        return {'msg': 'Draft is not a Hallucination Guard run'}, 400

    uploaded_file = UploadedFiles.query.filter_by(chat_thread_id=draft_obj.id).first()
    if not uploaded_file:
        return {'msg': 'File not found'}, 404

    unique_id = uploaded_file.unique_id
    db.session.delete(uploaded_file)
    db.session.commit()

    mem = DBMemory(db=db, doc_id=draft_obj.id)
    try:
        del mem[f'inputs/{unique_id}']
    except Exception:
        pass
    try:
        del mem['inputs/uploaded_filename']
    except Exception:
        pass
    _clear_standalone_hg_state(mem)

    return {'msg': 'File deleted'}, 200


@bp.post('/get-uploaded-files-everything')
@cognito_auth_required
def get_uploaded_files_everything():
    """
    **Case Assistant — list task-specific files for a draft.**

    Request JSON: { draftId }

    Returns metadata for all UploadedFiles rows with matching draft_id
    (unique_id, filename, size, upload_time, relative_path). Does not return
    full parsed text (that lives in DB / DBMemory for pipeline use).

    **Wiring:** EverythingDrafter.js on draft select / refresh (load chat files).
    Active; not deprecated.
    """
    data = request.get_json()
    if 'draftId' not in data:
        return {'msg': 'No draftId provided'}, 400
    
    # Check ownership
    chk = ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft_obj = ownership_check._last_draft

    uploaded_files = UploadedFiles.query.filter_by(chat_thread_id=draft_obj.id).all()
    
    files_data = []
    for file in uploaded_files:
        files_data.append({
            'unique_id': file.unique_id,
            'filename': file.original_filename,
            'file_size': file.file_size,
            'upload_time': file.upload_time.isoformat() if file.upload_time else None,
            'relative_path': file.relative_path,
            'metadata': _uploaded_file_metadata(file),
        })
    
    return {'uploaded_files': files_data}, 200


@bp.post('/delete-uploaded-file-everything')
@cognito_auth_required
def delete_uploaded_file_everything():
    """
    **Case Assistant — delete one task-specific file from a draft.**

    Request JSON: { draftId, unique_id }

    Deletes UploadedFiles row (cascade removes DocumentChunks), clears
    DBMemory inputs/{unique_id} and state['uploaded_files'][unique_id], and
    recomputes state total_page_count.

    **Wiring:** EverythingDrafter.js when user removes a composer attachment /
    task file. Active; not deprecated.

    Note: MatterContext.js also calls this route with draftId — separate from
    matter-level Case Background files (those use matter delete routes).
    """
    data = request.get_json()
    if 'draftId' not in data or 'unique_id' not in data:
        return {'msg': 'draftId and unique_id required'}, 400

    # ownership check
    chk = ownership_check(d={'draftId': data['draftId']}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    draft_obj = ownership_check._last_draft

    unique_id = data['unique_id']

    from backend.util import document_preparation as preparation
    if preparation.enabled():
        from backend.models import DocumentPreparation
        doc = DocumentPreparation.query.filter_by(id=unique_id, scope_key=f'chat:{draft_obj.id}').with_for_update().first()
        if doc:
            preparation.cancel_document(doc)
            return {'msg': 'File removed'}, 200

    uploaded_file = UploadedFiles.query.filter_by(chat_thread_id=draft_obj.id, unique_id=unique_id).first()
    if not uploaded_file:
        return {'msg': 'File not found'}, 404

    # delete DB record
    db.session.delete(uploaded_file)
    db.session.commit()

    # remove from DBMemory cache and state
    mem = DBMemory(db=db, doc_id=draft_obj.id)
    try:
        del mem[f'inputs/{unique_id}']
    except Exception:
        pass
    try:
        # Removing one uploaded-file cache entry is a mutation of current
        # auxiliary metadata, not a replacement of an earlier workspace view.
        from backend.models import TextEntry
        TextEntry.query.filter_by(draftId=draft_obj.id, textName='state').populate_existing().with_for_update().all()
        state = load_state_from_memory(mem)
        if unique_id in state.get('uploaded_files', {}):
            del state['uploaded_files'][unique_id]
            state['total_page_count'] = sum(
                info.get('page_count', 0) for info in state['uploaded_files'].values()
            )
            save_state_to_memory(state, mem, auxiliary_fields=('uploaded_files', 'total_page_count'))
    except Exception:
        pass

    return {'msg': 'File deleted'}, 200


def project_ownership_check(d: dict, jwt_id: int):
    """Ensure projectId is present, and sender owns it."""
    if 'projectId' not in d:
        return {'msg': 'No projectId in request'}, 401

    project = Project.query.filter_by(id=d['projectId']).first()
    if not project:
        return {'msg': 'Project not found'}, 404
    if jwt_id != project.userId:
        return {'msg': 'Current user does not own this project'}, 401
    return None


# ---------------------------------------------------------------------------
# Case Background — matter-level uploads (source='everything', shared across chats)
# ---------------------------------------------------------------------------
# Async flow (production): stage multipart → one Celery task per staged file → 202 + uploads[]
# Frontend polls GET /result/<task_id> for each upload. Limits: 100 MB/file, 30 files.
# Corpus excludes Doc Search files (source != 'search').
# Wired: EverythingDrafter.js handleMatterFileChange, uploadFolderItems
# ---------------------------------------------------------------------------

def _register_matter_upload_task_meta(task_id: str, *, project_id: int, folder_id: int | None = None) -> None:
    meta = {
        "task_type": "matter_background_upload",
        "user_id": current_user.id,
        "project_id": int(project_id),
    }
    if folder_id is not None:
        meta["folder_id"] = folder_id
    _cancel_redis.set(f"task_meta:{task_id}", json.dumps(meta), ex=7200)


@bp.post('/upload-folder-matter')
@cognito_auth_required
@_durable_upload('matter')
def upload_folder_matter():
    """
    **Case Background — async folder upload.**

    Accepts files[] + relativePaths + optional folderId. Stages to disk, enqueues
    one Case Background upload task per staged file.
    Returns 202 { uploads, folder_id }; frontend polls GET /result/<task_id>.
    """
    project_id = request.form.get('projectId')
    if not project_id:
        return {'msg': 'No projectId provided'}, 400

    chk = project_ownership_check(d={'projectId': int(project_id)}, jwt_id=current_user.id)
    if chk is not None:
        return chk

    files = request.files.getlist('files')
    folder_id = request.form.get('folderId')
    relative_paths_json = request.form.get('relativePaths', '[]')
    try:
        relative_paths = json.loads(relative_paths_json)
    except (json.JSONDecodeError, TypeError):
        relative_paths = []

    if not files:
        return {'msg': 'No files uploaded'}, 400

    while len(relative_paths) < len(files):
        relative_paths.append(None)

    folder_upload = None
    if folder_id:
        try:
            folder_id = int(folder_id)
        except (TypeError, ValueError):
            return {'msg': 'Invalid folderId'}, 400

        folder_upload = FolderUpload.query.filter_by(
            id=folder_id,
            project_id=int(project_id),
            draft_id=None,
        ).first()
        if not folder_upload:
            return {'msg': 'Folder not found'}, 404
        folder_name = folder_upload.folder_name
    else:
        valid_paths = [p for p in relative_paths if p]
        if valid_paths:
            first_parts = [p.split('/')[0] for p in valid_paths]
            folder_name = first_parts[0] if first_parts else 'Uploaded Folder'
        else:
            folder_name = 'Uploaded Folder'

    existing_file_count, _existing_page_count = matter_background_counts(int(project_id))

    allowed_indices, skipped_files = classify_matter_upload_indices(files, relative_paths)

    if not allowed_indices:
        return {'msg': 'No supported files found in folder (.pdf, .doc, .docx, .txt)', 'skipped_files': skipped_files}, 400

    if existing_file_count + len(allowed_indices) > MAX_FILES_PER_MATTER:
        return {
            'msg': f'Too many files. Max {MAX_FILES_PER_MATTER} per matter. '
                   f'Currently {existing_file_count}, trying to add {len(allowed_indices)}.'
        }, 400

    if folder_upload is None:
        folder_upload = FolderUpload(
            folder_name=folder_name,
            project_id=int(project_id),
        )
        db.session.add(folder_upload)
        db.session.commit()

    staged_files, failed_files = stage_matter_upload_files(
        files, allowed_indices, relative_paths,
    )

    if not staged_files:
        return {
            'folder_name': folder_name,
            'folder_id': folder_upload.id,
            'batch_file_count': 0,
            'batch_total_chunks': 0,
            'file_count': folder_upload.file_count or 0,
            'total_chunks': folder_upload.total_chunks or 0,
            'uploads': [],
            'files': [],
            'skipped_files': skipped_files,
            'failed_files': failed_files,
            'ocr_warning_count': 0,
            'timed_out': False,
        }, 200

    enqueued = enqueue_matter_upload_file_tasks(
        project_id=int(project_id),
        staged_files=staged_files,
        folder_id=folder_upload.id,
    )

    uploads = []
    for staged_file, result in enqueued:
        _register_matter_upload_task_meta(result.id, project_id=int(project_id), folder_id=folder_upload.id)
        uploads.append({
            'filename': staged_file.get('filename'),
            'relative_path': staged_file.get('relative_path'),
            'task_id': result.id,
        })
    return {
        'uploads': uploads,
        'folder_id': folder_upload.id,
        'skipped_files': skipped_files,
        'failed_files': failed_files,
    }, 202


@bp.post('/upload-file-matter')
@cognito_auth_required
@_durable_upload('matter')
def upload_file_matter():
    """
    **Case Background — async loose-file upload.**

    Accepts files[] for projectId. Stages to disk, enqueues one task per file.
    Returns 202 { uploads }; frontend polls GET /result/<task_id>.
    """
    project_id = request.form.get('projectId')
    if not project_id:
        return {'msg': 'No projectId provided'}, 400

    chk = project_ownership_check(d={'projectId': int(project_id)}, jwt_id=current_user.id)
    if chk is not None:
        return chk

    files = request.files.getlist('files')
    if not files:
        return {'msg': 'No files uploaded'}, 400

    existing_file_count, _existing_page_count = matter_background_counts(int(project_id))

    allowed_indices, skipped_files = classify_matter_upload_indices(files)

    if not allowed_indices:
        return {'msg': 'No supported files found (.pdf, .doc, .docx, .txt)', 'skipped_files': skipped_files}, 400

    if existing_file_count + len(allowed_indices) > MAX_FILES_PER_MATTER:
        return {
            'msg': f'Too many files. Max {MAX_FILES_PER_MATTER} per matter. '
                   f'Currently {existing_file_count}, trying to add {len(allowed_indices)}.'
        }, 400

    staged_files, failed_files = stage_matter_upload_files(files, allowed_indices)

    if not staged_files:
        return {
            'file_count': 0,
            'total_chunks': 0,
            'uploads': [],
            'files': [],
            'skipped_files': skipped_files,
            'failed_files': failed_files,
            'ocr_warning_count': 0,
            'timed_out': False,
            'processed_files': len(skipped_files) + len(failed_files),
            'total_files': len(skipped_files) + len(failed_files),
        }, 200

    enqueued = enqueue_matter_upload_file_tasks(
        project_id=int(project_id),
        staged_files=staged_files,
        folder_id=None,
    )

    uploads = []
    for staged_file, result in enqueued:
        _register_matter_upload_task_meta(result.id, project_id=int(project_id))
        uploads.append({
            'filename': staged_file.get('filename'),
            'relative_path': staged_file.get('relative_path'),
            'task_id': result.id,
        })
    return {
        'uploads': uploads,
        'skipped_files': skipped_files,
        'failed_files': failed_files,
    }, 202


@bp.post('/get-uploaded-files-matter-everything')
@cognito_auth_required
def get_uploaded_files_matter_everything():
    """Get matter-level files uploaded via everything.py only (excludes search uploads)."""
    data = request.get_json()
    chk = project_ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk

    project_id = int(data['projectId'])

    folders = FolderUpload.query.filter_by(project_id=project_id, draft_id=None).all()
    folders_data = []
    for folder in folders:
        files = UploadedFiles.query.filter(
            UploadedFiles.folder_upload_id == folder.id,
            or_(UploadedFiles.source != 'search', UploadedFiles.source.is_(None)),
        ).all()
        if files:
            folders_data.append({
                'folder_id': folder.id,
                'folder_name': folder.folder_name,
                'files': [{
                    'unique_id': f.unique_id,
                    'filename': f.original_filename,
                    'relative_path': f.relative_path,
                    'file_size': f.file_size,
                    'metadata': _uploaded_file_metadata(f),
                } for f in files],
            })

    loose_files = UploadedFiles.query.filter(
        UploadedFiles.project_id == project_id,
        UploadedFiles.chat_thread_id.is_(None),
        UploadedFiles.folder_upload_id.is_(None),
        or_(UploadedFiles.source != 'search', UploadedFiles.source.is_(None)),
    ).all()
    loose_data = [{
        'unique_id': f.unique_id,
        'filename': f.original_filename,
        'file_size': f.file_size,
        'metadata': _uploaded_file_metadata(f),
    } for f in loose_files]

    return {'folders': folders_data, 'loose_files': loose_data}, 200


@bp.post('/get-uploaded-files-search')
@cognito_auth_required
def get_uploaded_files_search():
    """Get matter-level files uploaded via smart-search (source='search')."""
    data = request.get_json()
    chk = project_ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk

    project_id = int(data['projectId'])

    def _file_tags(file_id):
        rows = db.session.query(FileTagAssociation, FileTag).join(
            FileTag, FileTagAssociation.tag_id == FileTag.id
        ).filter(FileTagAssociation.uploaded_file_id == file_id).all()
        return [{'tag_id': tag.id, 'tag_name': tag.tag_name, 'tag_color': tag.tag_color} for _, tag in rows]

    def _folder_tags(folder_id):
        rows = db.session.query(FolderTagAssociation, FileTag).join(
            FileTag, FolderTagAssociation.tag_id == FileTag.id
        ).filter(FolderTagAssociation.folder_upload_id == folder_id).all()
        return [{'tag_id': tag.id, 'tag_name': tag.tag_name, 'tag_color': tag.tag_color} for _, tag in rows]

    folders = FolderUpload.query.filter_by(project_id=project_id, draft_id=None).all()
    folders_data = []
    for folder in folders:
        files = UploadedFiles.query.filter(
            UploadedFiles.folder_upload_id == folder.id,
            UploadedFiles.source == 'search',
        ).all()
        if files:
            folders_data.append({
                'folder_id': folder.id,
                'folder_name': folder.folder_name,
                'upload_date': f'{folder.upload_time.day} {folder.upload_time.strftime("%b %Y")}' if folder.upload_time else None,
                'tags': _folder_tags(folder.id),
                'files': [{
                    'unique_id': f.unique_id,
                    'filename': f.original_filename,
                    'file_size': f.file_size,
                    'upload_date': f'{f.upload_time.day} {f.upload_time.strftime("%b %Y")}' if f.upload_time else None,
                    'relative_path': f.relative_path,
                    'tags': _file_tags(f.id),
                    'metadata': _uploaded_file_metadata(f),
                } for f in files],
            })

    loose_files = UploadedFiles.query.filter(
        UploadedFiles.project_id == project_id,
        UploadedFiles.chat_thread_id.is_(None),
        UploadedFiles.folder_upload_id.is_(None),
        UploadedFiles.source == 'search',
    ).all()
    loose_data = [{
        'unique_id': f.unique_id,
        'filename': f.original_filename,
        'file_size': f.file_size,
        'upload_date': f'{f.upload_time.day} {f.upload_time.strftime("%b %Y")}' if f.upload_time else None,
        'tags': _file_tags(f.id),
        'metadata': _uploaded_file_metadata(f),
    } for f in loose_files]

    return {'folders': folders_data, 'loose_files': loose_data}, 200


@bp.post('/delete-uploaded-file-matter')
@cognito_auth_required
def delete_uploaded_file_matter():
    """Delete a matter-level uploaded file."""
    data = request.get_json()
    if 'projectId' not in data or 'unique_id' not in data:
        return {'msg': 'projectId and unique_id required'}, 400

    chk = project_ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk

    project_id = int(data['projectId'])
    unique_id = data['unique_id']

    from backend.util import document_preparation as preparation
    if preparation.enabled():
        from backend.models import DocumentPreparation
        doc = DocumentPreparation.query.filter_by(id=unique_id, scope_key=f'matter:{project_id}').with_for_update().first()
        if doc:
            preparation.cancel_document(doc)
            return {'msg': 'File removed'}, 200

    uploaded_file = UploadedFiles.query.filter(
        UploadedFiles.project_id == project_id,
        UploadedFiles.chat_thread_id.is_(None),
        UploadedFiles.unique_id == unique_id,
        or_(UploadedFiles.source != 'search', UploadedFiles.source.is_(None)),
    ).first()
    if not uploaded_file:
        return {'msg': 'File not found'}, 404

    db.session.delete(uploaded_file)
    db.session.commit()

    return {'msg': 'File deleted'}, 200


@bp.post('/delete-uploaded-file-search')
@cognito_auth_required
def delete_uploaded_file_search():
    """Delete a search-sourced uploaded file."""
    data = request.get_json()
    if 'projectId' not in data or 'unique_id' not in data:
        return {'msg': 'projectId and unique_id required'}, 400

    chk = project_ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk

    project_id = int(data['projectId'])
    unique_id = data['unique_id']

    uploaded_file = UploadedFiles.query.filter_by(
        project_id=project_id,
        draft_id=None,
        unique_id=unique_id,
        source='search',
    ).first()
    if not uploaded_file:
        return {'msg': 'File not found'}, 404

    db.session.delete(uploaded_file)
    db.session.commit()

    return {'msg': 'File deleted'}, 200


@bp.post('/delete-folder-matter')
@cognito_auth_required
def delete_folder_matter():
    """Delete a matter-level folder and all its files."""
    data = request.get_json()
    if 'projectId' not in data or 'folderId' not in data:
        return {'msg': 'projectId and folderId required'}, 400

    project_id = int(data['projectId'])
    folder_id = int(data['folderId'])

    # Verify project ownership
    project = Project.query.filter_by(id=project_id).first()
    if not project:
        return {'msg': 'Project not found'}, 404
    if current_user.id != project.userId:
        return {'msg': 'Current user does not own this project'}, 401

    folder = FolderUpload.query.filter_by(id=folder_id, project_id=project_id, draft_id=None).first()
    if not folder:
        return {'msg': 'Folder not found'}, 404

    from backend.util import document_preparation as preparation
    if preparation.enabled():
        from backend.models import DocumentPreparation
        for doc in DocumentPreparation.query.filter_by(folder_upload_id=folder_id, scope_key=f'matter:{project_id}').all():
            preparation.cancel_document(doc)

    # Delete all files in the folder (chunks cascade via delete-orphan on UploadedFiles.chunks)
    files = UploadedFiles.query.filter(
        UploadedFiles.folder_upload_id == folder_id,
    ).all()
    for f in files:
        db.session.delete(f)
    deleted_files = len(files)

    db.session.delete(folder)
    db.session.commit()

    return {'msg': 'Folder deleted', 'deleted_files': deleted_files}, 200


# ---------------------------------------------------------------------------
# Doc Search (Smart Search) — matter-level document corpus (source='search')
# ---------------------------------------------------------------------------
# Files are scoped to project_id with draft_id=NULL. Used by the Doc Search tab
# for upload, BM25/vector retrieval, tagging, and search sessions — separate
# from Case Assistant per-chat attachments and Case Background (source='everything').
#
# Upload flow (production):
#   POST /smart-search/upload-file   → loose multi-file request, one Celery job per staged file
#   POST /smart-search/upload-folder → folder request (webkitdirectory), one Celery job per staged file
#   GET  /result/<task_id>           → poll one-file summaries from smart_search_upload_file_task
#
# Wired in frontend: EverythingDrafter.js uploadSearchFiles / uploadSearchFolderItems
#   (batches by size, polls every uploads[].task_id).
#
# Related list/delete (not upload, but same corpus):
#   POST /smart-search/get-files, POST /smart-search/delete-file
# ---------------------------------------------------------------------------

def _register_smart_search_upload_task_meta(task_id: str, *, project_id: int, folder_id: int | None = None) -> None:
    meta = {
        "task_type": "smart_search_upload",
        "user_id": current_user.id,
        "project_id": int(project_id),
    }
    if folder_id is not None:
        meta["folder_id"] = folder_id
    _cancel_redis.set(f"task_meta:{task_id}", json.dumps(meta), ex=7200)


def _enqueue_smart_search_upload_file_tasks(
    *,
    project_id: int,
    staged_files: list[dict],
    folder_id: int | None = None,
) -> list[dict]:
    uploads = []
    for idx, staged_file in enumerate(staged_files):
        payload = {
            "project_id": int(project_id),
            "folder_id": folder_id,
            "file": staged_file,
        }
        try:
            result = smart_search_upload_file_task.delay(payload)
        except Exception:
            cleanup_staged_files(staged_files[idx:])
            raise
        _register_smart_search_upload_task_meta(
            result.id,
            project_id=int(project_id),
            folder_id=folder_id,
        )
        uploads.append({
            "filename": staged_file.get("filename"),
            "relative_path": staged_file.get("relative_path"),
            "task_id": result.id,
        })
    return uploads

@bp.post('/smart-search/upload-file')
@cognito_auth_required
def smart_search_upload_file():
    """
    **Doc Search — async upload for loose files (no folder container).**

    Accepts multipart files[] for a projectId. Stages each allowed file to
    backend/static/temp_* then enqueues one smart_search_upload_file_task
    (Celery) per staged file.

    Worker (smart_search_upload_file_task): OCR/parse → UploadedFiles (source='search',
    draft_id=NULL) → DocumentChunk rows; optional page limits (none for search).

    Skips unsupported extensions and Office lock files in the request thread;
    if nothing to stage, returns 200 with empty summary (no Celery job).

    Response 202: { uploads } — frontend polls GET /result/<task_id> per file.
    Response 200: immediate empty/skipped-only summary when no staged files.

    **Wiring:** Active — Doc Search “Upload docs” multi-file picker.
    """
    project_id = request.form.get('projectId')
    if not project_id:
        return {'msg': 'No projectId provided'}, 400

    chk = project_ownership_check(d={'projectId': int(project_id)}, jwt_id=current_user.id)
    if chk is not None:
        return chk

    files = request.files.getlist('files')
    if not files:
        return {'msg': 'No files uploaded'}, 400

    skipped_files = []
    staged_files = []
    for file in files:
        if not file.filename or not is_allowed_filetype(file.filename):
            skipped_files.append(file.filename or 'unknown')
            continue
        if is_office_lock_filename(file.filename):
            skipped_files.append(file.filename or 'unknown')
            continue
        safe_filename = os.path.basename(file.filename)
        stage_id = f"{uuid.uuid4()}_{safe_filename}"
        temp_path = os.path.join("backend/static", f"temp_{stage_id}")
        file.save(temp_path)
        staged_files.append({
            'filename': safe_filename,
            'relative_path': None,
            'temp_path': temp_path,
        })

    if not staged_files:
        return {
            'file_count': 0,
            'total_chunks': 0,
            'uploads': [],
            'files': [],
            'skipped_files': skipped_files,
            'failed_files': [],
            'ocr_warning_count': 0,
            'processed_files': len(skipped_files),
            'total_files': len(skipped_files),
            'timed_out': False,
        }, 200

    uploads = _enqueue_smart_search_upload_file_tasks(
        project_id=int(project_id),
        staged_files=staged_files,
        folder_id=None,
    )
    return {
        'uploads': uploads,
        'skipped_files': skipped_files,
        'failed_files': [],
    }, 202


@bp.post('/smart-search/upload-folder')
@cognito_auth_required
def smart_search_upload_folder():
    """
    **Doc Search — async upload for folder batches (webkitdirectory).**

    Accepts multipart files[] + relativePaths JSON + optional folderId for a
    projectId. Creates or reuses a FolderUpload row (project-level, draft_id=NULL),
    stages each allowed file to temp disk, then enqueues one Celery task per file.

    First batch without folderId: infers folder_name from relative path root.
    Later batches pass folderId to append files to the same FolderUpload.

    Worker sets relative_path on chunks/files; updates folder aggregate counts.

    Skips bad extensions / lock files; 200 with empty batch if nothing staged;
    202 { uploads, folder_id } when Celery jobs are queued.

    **Wiring:** Active — Doc Search folder picker (EverythingDrafter.js).
    Frontend size-batches large folders into multiple POSTs, each polled separately.

    Intentionally no OCR in gunicorn — reduces nginx proxy_read_timeout risk vs sync.
    """
    project_id = request.form.get('projectId')
    if not project_id:
        return {'msg': 'No projectId provided'}, 400

    chk = project_ownership_check(d={'projectId': int(project_id)}, jwt_id=current_user.id)
    if chk is not None:
        return chk

    # Pull all files from this multipart request batch (already size-batched in frontend).
    files = request.files.getlist('files')
    folder_id = request.form.get('folderId')
    relative_paths_json = request.form.get('relativePaths', '[]')
    try:
        relative_paths = json.loads(relative_paths_json)
    except (json.JSONDecodeError, TypeError):
        relative_paths = []

    if not files:
        return {'msg': 'No files uploaded'}, 400

    # Keep one relative-path slot per file so downstream index access is safe.
    while len(relative_paths) < len(files):
        relative_paths.append(None)

    folder_upload = None
    if folder_id:
        # Existing folder mode: append files into a previously created FolderUpload row.
        try:
            folder_id = int(folder_id)
        except (TypeError, ValueError):
            return {'msg': 'Invalid folderId'}, 400

        folder_upload = FolderUpload.query.filter_by(
            id=folder_id,
            project_id=int(project_id),
            draft_id=None,
        ).first()
        if not folder_upload:
            return {'msg': 'Folder not found'}, 404
        folder_name = folder_upload.folder_name
    else:
        # First batch for a new folder upload: infer folder name from relative path root.
        valid_paths = [p for p in relative_paths if p]
        if valid_paths:
            first_parts = [p.split('/')[0] for p in valid_paths]
            folder_name = first_parts[0] if first_parts else 'Uploaded Folder'
        else:
            folder_name = 'Uploaded Folder'

    if folder_upload is None:
        # Persist container row now so child files can reference folder_upload_id.
        folder_upload = FolderUpload(
            folder_name=folder_name,
            project_id=int(project_id),
        )
        db.session.add(folder_upload)
        db.session.commit()

    skipped_files = []
    staged_files = []
    for i, file in enumerate(files):
        rel_path = relative_paths[i]
        # Enforce backend extension allowlist even if frontend filtered earlier.
        if not file.filename or not is_allowed_filetype(file.filename):
            skipped_files.append(file.filename or 'unknown')
            continue
        if is_office_lock_filename(file.filename) or is_office_lock_filename(rel_path):
            skipped_files.append(rel_path or file.filename or 'unknown')
            continue

        # Stage accepted file to temp disk so request can return quickly and worker
        # can parse from stable path outside request lifecycle.
        safe_filename = os.path.basename(file.filename)
        stage_id = f"{uuid.uuid4()}_{safe_filename}"
        temp_path = os.path.join("backend/static", f"temp_{stage_id}")
        file.save(temp_path)
        staged_files.append({
            'filename': safe_filename,
            'relative_path': rel_path,
            'temp_path': temp_path,
        })

    if not staged_files:
        # No accepted files in this batch: return immediately with explicit summary
        # (no Celery task needed).
        return {
            'folder_name': folder_name,
            'folder_id': folder_upload.id,
            'batch_file_count': 0,
            'batch_total_chunks': 0,
            'file_count': folder_upload.file_count or 0,
            'total_chunks': folder_upload.total_chunks or 0,
            'uploads': [],
            'files': [],
            'skipped_files': skipped_files,
            'failed_files': [],
            'ocr_warning_count': 0,
            'processed_files': len(skipped_files),
            'total_files': len(skipped_files),
            'timed_out': False,
        }, 200

    uploads = _enqueue_smart_search_upload_file_tasks(
        project_id=int(project_id),
        staged_files=staged_files,
        folder_id=folder_upload.id,
    )
    # Frontend polls /result/<task_id> for each file and aggregates summaries.
    return {
        'uploads': uploads,
        'folder_id': folder_upload.id,
        'skipped_files': skipped_files,
        'failed_files': [],
    }, 202



@bp.get('/uploaded-doc/<unique_id>')
@cognito_auth_required
def serve_uploaded_doc(unique_id):
    """Serve an original uploaded document file for smart search."""
    uploaded_file = UploadedFiles.query.filter_by(unique_id=unique_id).first_or_404()
    chk = project_ownership_check({'projectId': uploaded_file.project_id}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    content = uploaded_file.content or ''
    return send_file(
        BytesIO(content.encode('utf-8')),
        download_name=uploaded_file.original_filename + '.txt',
        mimetype='text/plain',
        as_attachment=False,
    )


@bp.post('/smart-search/query')
@cognito_auth_required
def smart_search_query():
    """Trigger smart search query (phase1 → phase2 Celery chain)."""
    data = request.get_json()
    if not data or 'projectId' not in data:
        return {'msg': 'projectId is required'}, 400
    if not data.get('query'):
        return {'msg': 'query is required'}, 400

    chk = project_ownership_check(d={'projectId': int(data['projectId'])}, jwt_id=current_user.id)
    if chk is not None:
        return chk

    data['user_id'] = current_user.id
    data['project_id'] = int(data['projectId'])
    session_id = data.get('sessionId') or str(uuid.uuid4())
    data['session_id'] = session_id

    # Upsert SmartSearchSession in SQL
    existing = _SmartSearchSession.query.get(session_id)
    if existing:
        if existing.user_id != current_user.id:
            return {'msg': 'Session does not belong to current user'}, 403
        if existing.project_id != int(data['projectId']):
            return {'msg': 'Session does not belong to this matter'}, 400
    else:
        title = (data['query'][:80]).strip()
        new_session = _SmartSearchSession(
            id=session_id,
            user_id=current_user.id,
            project_id=int(data['projectId']),
            title=title,
        )
        db.session.add(new_session)
        db.session.commit()

    result = smart_search_task.delay(data)
    # Cancel bookkeeping (Smart Search):
    # This flow uses one Celery task id (no phase1/phase2 chain parent).
    # Shared /cancel/<id> uses this metadata to:
    # - authorize by user_id
    # - clear active_task_id in smart_search_state:<session_id>
    _cancel_redis.set(
        f"task_meta:{result.id}",
        json.dumps({
            "task_type": "smart_search",
            "session_id": session_id,
            "user_id": current_user.id,
            "project_id": int(data['projectId']),
        }),
        ex=900,
    )
    try:
        analytics_record_user_event(
            db,
            user_id=current_user.id,
            doc_type="smart_search",
            step=0,
            draft_id=0,
        )
    except Exception as e:
        current_app.logger.warning(
            "Smart Search analytics logging failed for user_id=%s session_id=%s: %s",
            current_user.id,
            session_id,
            e,
        )
    return {'result_id': result.id, 'session_id': session_id}, 200


@bp.get('/smart-search/sessions')
@cognito_auth_required
def smart_search_list_sessions():
    """List all smart search sessions for the current user in a project."""
    project_id = request.args.get('projectId')
    if not project_id:
        return {'msg': 'projectId is required'}, 400

    chk = project_ownership_check(d={'projectId': int(project_id)}, jwt_id=current_user.id)
    if chk is not None:
        return chk

    sessions = _SmartSearchSession.query.filter_by(
        user_id=current_user.id, project_id=int(project_id)
    ).order_by(_SmartSearchSession.updated_at.desc()).all()

    return {
        'sessions': [
            {
                'id': s.id,
                'title': s.title,
                'created_at': s.created_at.isoformat() if s.created_at else None,
                'updated_at': s.updated_at.isoformat() if s.updated_at else None,
            }
            for s in sessions
        ]
    }, 200


@bp.get('/smart-search/session/<session_id>')
@cognito_auth_required
def smart_search_get_session(session_id):
    """Load full message history for a smart search session."""
    session = _SmartSearchSession.query.get(session_id)
    if not session:
        return {'msg': 'Session not found'}, 404
    if session.user_id != current_user.id:
        return {'msg': 'Session does not belong to current user'}, 403

    from backend.models import SmartSearchMessage as _SmartSearchMessage
    messages = _SmartSearchMessage.query.filter_by(session_id=session_id)\
        .order_by(_SmartSearchMessage.created_at, _SmartSearchMessage.id).all()

    payload_messages = []
    for m in messages:
        metadata = {}
        if m.metadata_json:
            try:
                metadata = json.loads(m.metadata_json) or {}
            except (json.JSONDecodeError, TypeError):
                metadata = {}

        sources = metadata.get('sources') if isinstance(metadata.get('sources'), list) else []
        source_chunks = metadata.get('source_chunks') if isinstance(metadata.get('source_chunks'), list) else []
        top_chunk = metadata.get('top_chunk') if isinstance(metadata.get('top_chunk'), dict) else {}

        top_uid = str(top_chunk.get('unique_id') or '').strip()
        top_filename = str(top_chunk.get('filename') or '').strip()
        top_content = str(top_chunk.get('content') or '').strip()

        if top_uid and top_filename and not any((s or {}).get('unique_id') == top_uid for s in sources):
            sources.append({'filename': top_filename, 'unique_id': top_uid})

        # Legacy fallback: older rows stored only top_chunk.
        if not source_chunks and top_uid and top_filename and top_content:
            source_chunks = [{
                'unique_id': top_uid,
                'filename': top_filename,
                'chunk_index': top_chunk.get('chunk_index'),
                'content': top_content,
            }]

        payload_messages.append({
            'role': m.role,
            'content': m.content or '',
            'created_at': m.created_at.isoformat() if m.created_at else None,
            'sources': sources,
            'source_chunks': source_chunks,
        })

    return {
        'session': {
            'id': session.id,
            'title': session.title,
            'created_at': session.created_at.isoformat() if session.created_at else None,
            'updated_at': session.updated_at.isoformat() if session.updated_at else None,
        },
        'messages': payload_messages,
    }, 200


@bp.post('/smart-search/rename-session')
@cognito_auth_required
def smart_search_rename_session():
    """Rename a smart search session."""
    data = request.get_json() or {}
    session_id = data.get('sessionId')
    new_title = (data.get('title') or '').strip()
    if not session_id:
        return {'msg': 'sessionId is required'}, 400
    if not new_title:
        return {'msg': 'title is required'}, 400

    session = _SmartSearchSession.query.get(session_id)
    if not session:
        return {'msg': 'Session not found'}, 404
    if session.user_id != current_user.id:
        return {'msg': 'Session does not belong to current user'}, 403

    session.title = new_title
    db.session.commit()

    return {'msg': 'Session renamed', 'session_id': session_id, 'title': new_title}, 200


@bp.post('/smart-search/delete-session')
@cognito_auth_required
def smart_search_delete_session():
    """Delete a smart search session and all of its messages."""
    data = request.get_json() or {}
    session_id = data.get('sessionId')
    if not session_id:
        return {'msg': 'sessionId is required'}, 400

    session = _SmartSearchSession.query.get(session_id)
    if not session:
        return {'msg': 'Session not found'}, 404
    if session.user_id != current_user.id:
        return {'msg': 'Session does not belong to current user'}, 403

    db.session.delete(session)
    db.session.commit()
    try:
        _cancel_redis.delete(f"smart_search_state:{session_id}")
    except Exception:
        pass

    return {'msg': 'Session deleted', 'session_id': session_id}, 200


@bp.post('/smart-search/get-files')
@cognito_auth_required
def smart_search_get_files():
    """
    **Doc Search — list project corpus files (source='search', draft_id=NULL).**

    Request JSON: { projectId }. Returns uploaded_files metadata array.
    **Wiring:** Active — Doc Search file list refresh in EverythingDrafter.js.
    """
    data = request.get_json()
    project_id = data.get('projectId') if data else None
    if not project_id:
        return {'msg': 'projectId is required'}, 400

    chk = project_ownership_check(d={'projectId': int(project_id)}, jwt_id=current_user.id)
    if chk is not None:
        return chk

    files = UploadedFiles.query.filter_by(project_id=int(project_id), draft_id=None).all()
    return {
        'uploaded_files': [
            {
                'unique_id': f.unique_id,
                'filename': f.original_filename,
                'file_size': f.file_size,
                'upload_time': f.upload_time.isoformat() if f.upload_time else None,
                'relative_path': f.relative_path,
                'metadata': _uploaded_file_metadata(f),
            }
            for f in files
        ]
    }, 200


@bp.post('/smart-search/delete-file')
@cognito_auth_required
def smart_search_delete_file():
    """
    **Doc Search — delete one corpus file by unique_id (source='search' only).**

    Request JSON: { unique_id }. Project ownership checked via file row.
    **Wiring:** Active — Doc Search file removal in EverythingDrafter.js.
    Distinct from /delete-uploaded-file-everything (Case Assistant draft files).
    """
    data = request.get_json()
    unique_id = data.get('unique_id') if data else None
    if not unique_id:
        return {'msg': 'unique_id is required'}, 400

    f = UploadedFiles.query.filter(
        UploadedFiles.unique_id == unique_id,
        UploadedFiles.source == 'search',
    ).first()
    if not f:
        return {'msg': 'File not found'}, 404

    chk = project_ownership_check(d={'projectId': f.project_id}, jwt_id=current_user.id)
    if chk is not None:
        return chk

    db.session.delete(f)
    db.session.commit()
    return {'msg': 'File deleted'}, 200


@bp.get('/smart-search/tags/<int:project_id>')
@cognito_auth_required
def get_search_tags(project_id):
    """Get all tags defined for a project."""
    chk = project_ownership_check(d={'projectId': project_id}, jwt_id=current_user.id)
    if chk is not None:
        return chk
    tags = FileTag.query.filter_by(project_id=project_id).all()
    return {'tags': [{'tag_id': t.id, 'tag_name': t.tag_name, 'tag_color': t.tag_color} for t in tags]}, 200


@bp.post('/smart-search/tags')
@cognito_auth_required
def create_search_tag():
    """Create a new tag for a project."""
    data = request.get_json()
    if not data or 'projectId' not in data or 'tagName' not in data:
        return {'msg': 'projectId and tagName required'}, 400
    chk = project_ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk
    tag = FileTag(
        project_id=int(data['projectId']),
        tag_name=data['tagName'].strip(),
        tag_color=data.get('tagColor', '#6366f1'),
    )
    db.session.add(tag)
    db.session.commit()
    return {'tag_id': tag.id, 'tag_name': tag.tag_name, 'tag_color': tag.tag_color}, 201


@bp.post('/smart-search/tag-files')
@cognito_auth_required
def tag_search_files():
    """Attach a tag to one or more files (by unique_id)."""
    data = request.get_json()
    if not data or 'projectId' not in data or 'tagId' not in data or 'fileIds' not in data:
        return {'msg': 'projectId, tagId, and fileIds required'}, 400
    chk = project_ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk
    project_id = int(data['projectId'])
    tag_id = int(data['tagId'])
    file_ids = data['fileIds']

    tag = FileTag.query.filter_by(id=tag_id, project_id=project_id).first()
    if not tag:
        return {'msg': 'Tag not found'}, 404

    for uid in file_ids:
        f = UploadedFiles.query.filter_by(unique_id=uid, project_id=project_id, source='search').first()
        if not f:
            continue
        existing = FileTagAssociation.query.filter_by(uploaded_file_id=f.id, tag_id=tag_id).first()
        if not existing:
            db.session.add(FileTagAssociation(uploaded_file_id=f.id, tag_id=tag_id))

    db.session.commit()
    return {'ok': True}, 200


@bp.post('/smart-search/move-files-to-folder')
@cognito_auth_required
def move_search_files_to_folder():
    """Create a new folder and move selected loose files into it."""
    data = request.get_json()
    if not data or 'projectId' not in data or 'folderName' not in data or 'fileIds' not in data:
        return {'msg': 'projectId, folderName, and fileIds required'}, 400
    chk = project_ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk
    project_id = int(data['projectId'])
    folder_name = data['folderName'].strip()
    file_ids = data['fileIds']

    if not folder_name:
        return {'msg': 'folderName cannot be empty'}, 400

    folder = FolderUpload(folder_name=folder_name, project_id=project_id)
    db.session.add(folder)
    db.session.flush()

    moved = 0
    for uid in file_ids:
        f = UploadedFiles.query.filter_by(
            unique_id=uid, project_id=project_id, draft_id=None, source='search'
        ).first()
        if f:
            f.folder_upload_id = folder.id
            moved += 1

    folder.file_count = moved
    db.session.commit()
    return {'ok': True, 'folder_id': folder.id, 'folder_name': folder.folder_name, 'moved': moved}, 200


@bp.post('/smart-search/move-file-to-folder')
@cognito_auth_required
def move_file_to_existing_folder():
    """Drag-and-drop: move a single file into an existing folder."""
    data = request.get_json()
    if not data or 'projectId' not in data or 'fileId' not in data or 'targetFolderId' not in data:
        return {'msg': 'projectId, fileId, and targetFolderId required'}, 400
    chk = project_ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk
    project_id = int(data['projectId'])
    file_id = data['fileId']
    target_folder_id = int(data['targetFolderId'])

    target = FolderUpload.query.filter_by(id=target_folder_id, project_id=project_id).first()
    if not target:
        return {'msg': 'Target folder not found'}, 404

    f = UploadedFiles.query.filter_by(unique_id=file_id, project_id=project_id, source='search').first()
    if not f:
        return {'msg': 'File not found'}, 404

    f.folder_upload_id = target_folder_id
    f.relative_path = f.original_filename
    db.session.commit()
    return {'ok': True}, 200


@bp.post('/smart-search/tag-folders')
@cognito_auth_required
def tag_search_folders():
    """Attach a tag to one or more folders."""
    data = request.get_json()
    if not data or 'projectId' not in data or 'tagId' not in data or 'folderIds' not in data:
        return {'msg': 'projectId, tagId, and folderIds required'}, 400
    chk = project_ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk
    project_id = int(data['projectId'])
    tag_id = int(data['tagId'])
    folder_ids = data['folderIds']

    tag = FileTag.query.filter_by(id=tag_id, project_id=project_id).first()
    if not tag:
        return {'msg': 'Tag not found'}, 404

    for fid in folder_ids:
        folder = FolderUpload.query.filter_by(id=int(fid), project_id=project_id).first()
        if not folder:
            continue
        existing = FolderTagAssociation.query.filter_by(folder_upload_id=folder.id, tag_id=tag_id).first()
        if not existing:
            db.session.add(FolderTagAssociation(folder_upload_id=folder.id, tag_id=tag_id))

    db.session.commit()
    return {'ok': True}, 200


@bp.post('/smart-search/untag-files')
@cognito_auth_required
def untag_search_files():
    """Remove a tag from one or more files."""
    data = request.get_json()
    if not data or 'projectId' not in data or 'tagId' not in data or 'fileIds' not in data:
        return {'msg': 'projectId, tagId, and fileIds required'}, 400
    chk = project_ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk
    project_id = int(data['projectId'])
    tag_id = int(data['tagId'])
    for uid in data['fileIds']:
        f = UploadedFiles.query.filter_by(unique_id=uid, project_id=project_id).first()
        if f:
            FileTagAssociation.query.filter_by(uploaded_file_id=f.id, tag_id=tag_id).delete()
    db.session.commit()
    return {'ok': True}, 200


@bp.post('/smart-search/untag-folders')
@cognito_auth_required
def untag_search_folders():
    """Remove a tag from one or more folders."""
    data = request.get_json()
    if not data or 'projectId' not in data or 'tagId' not in data or 'folderIds' not in data:
        return {'msg': 'projectId, tagId, and folderIds required'}, 400
    chk = project_ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk
    project_id = int(data['projectId'])
    tag_id = int(data['tagId'])
    for fid in data['folderIds']:
        FolderTagAssociation.query.filter_by(folder_upload_id=int(fid), tag_id=tag_id).delete()
    db.session.commit()
    return {'ok': True}, 200


@bp.post('/smart-search/nest-folder')
@cognito_auth_required
def nest_search_folder():
    """Drag-and-drop: move a folder inside another folder (updates relative_path on all files)."""
    data = request.get_json()
    if not data or 'projectId' not in data or 'sourceFolderId' not in data or 'targetFolderId' not in data:
        return {'msg': 'projectId, sourceFolderId, and targetFolderId required'}, 400
    chk = project_ownership_check(d=data, jwt_id=current_user.id)
    if chk is not None:
        return chk
    project_id = int(data['projectId'])
    source_id = int(data['sourceFolderId'])
    target_id = int(data['targetFolderId'])

    if source_id == target_id:
        return {'msg': 'Source and target cannot be the same'}, 400

    source = FolderUpload.query.filter_by(id=source_id, project_id=project_id).first()
    target = FolderUpload.query.filter_by(id=target_id, project_id=project_id).first()
    if not source or not target:
        return {'msg': 'Folder not found'}, 404

    files = UploadedFiles.query.filter_by(folder_upload_id=source_id, source='search').all()
    for f in files:
        original = f.relative_path or f.original_filename
        # Strip the source folder name prefix if present, then re-prefix with it
        parts = original.split('/', 1)
        leaf = parts[1] if len(parts) > 1 else parts[0]
        f.relative_path = f'{source.folder_name}/{leaf}'
        f.folder_upload_id = target_id

    # Clean up source folder record and its tag associations
    FolderTagAssociation.query.filter_by(folder_upload_id=source_id).delete()
    db.session.delete(source)
    db.session.commit()
    return {'ok': True}, 200
