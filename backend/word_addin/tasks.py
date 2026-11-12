"""Word jobs never publish confidential results to the legacy Celery poll API."""
import json

from celery import shared_task
from flask import current_app
from sqlalchemy import select

from backend.database import db
from backend.models import ChatThread, Project, TextEntry, User
from backend.util.account_access import require_enabled_account
from backend.word_addin.routes import RELEASE, RENEW, TTL, entry, redis_client, set_entry, transition, publish_workspace_result
from backend.word_addin.service import execute, owned_files, review_text


class WordTaskStopped(Exception):
    pass


@shared_task(ignore_result=True, soft_time_limit=780, time_limit=840)
def word_operation(draft_id):
    draft = db.session.get(ChatThread, draft_id)
    if draft is None:
        return
    project = db.session.get(Project, draft.projectId)
    user = db.session.get(User, project.userId) if project else None
    if not user:
        return
    claimed = False
    try:
        if not transition(draft.id, {"queued"}, "running", message="Reading the passage and selected sources…"):
            if (entry(draft.id, "word.status") or {}).get("state") == "cancelled":
                redis_client().eval(RELEASE, 1, f"word:active:{user.id}", draft.public_id)
            return
        claimed = True
        # A delayed queue delivery must still own the user's slot. Renew it
        # atomically for longer than the worker's hard time limit, so queue
        # waiting time cannot allow a second model call while this one runs.
        if not redis_client().eval(RENEW, 1, f"word:active:{user.id}", draft.public_id, TTL):
            raise ValueError("This queued task expired. Please start a new task.")
        require_enabled_account(user.id)
        request = entry(draft.id, "word.request")
        owned_files(project.id, request["file_ids"])
        def checkpoint(message):
            require_enabled_account(user.id)
            if not current_app.config.get("WORD_ADDIN_ENABLED", False):
                raise ValueError("The Word service was disabled. Please retry after it is enabled.")
            if not transition(draft.id, {"running"}, "running", message=message):
                raise WordTaskStopped()

        from backend.util import workspace_store
        base_draft_version = workspace_store.load(draft.id)['draft_version']
        db.session.commit()
        from backend.word_addin.document_workflow import is_document_request, execute_document
        result = execute_document(request, project.id, checkpoint) if is_document_request(request) else execute(request, project.id, checkpoint)
        if request.get("parent_operation_id"):
            result["lineage"] = {"parent_operation_id": request["parent_operation_id"],
                                 "round": len(request["refinement_context"]["lawyer_followups"]),
                                 "instruction": request["followup_instructions"]}
        result["review_text"] = review_text(result)
        # Publication allocates chat turn order; acquire its parent lock before
        # the Word status and owned document locks, after provider I/O finishes.
        ChatThread.query.filter_by(id=draft.id).with_for_update().one()
        # Lock and check terminal state so cancellation wins over late model output.
        row = db.session.execute(select(TextEntry).where(TextEntry.chat_thread_id == draft.id,
                                 TextEntry.textName == "word.status").with_for_update()).scalar_one_or_none()
        if row is None or json.loads(row.content)["state"] != "running":
            db.session.rollback()
            return
        # Account status may have changed while the provider was working.
        require_enabled_account(user.id)
        state = json.loads(row.content)
        set_entry(draft.id, "word.result", result)
        # Reuse the Case Assistant storage contract, in this operation's OWN draft.
        sections = [{"section_id": "word-result", "section_name": "Proposed text", "section_content": result["text"]}] if result["kind"] in {"revision", "draft"} else []
        publish_workspace_result(draft.id, {"draft": {"title": draft.display_name, "sections": sections},
                  "chat_history": [[request.get("followup_instructions") or request["instructions"] or request["action"], result["review_text"]]],
                  "chat_history_meta": [{"entry_point": "word_addin"}], "validated_cases": {},
                  "uploaded_files": {}, "constraints": {"jurisdiction": request["jurisdiction"]}},
                  expected_version=base_draft_version)
        row.content = json.dumps({**state, "state": "complete", "message": "Ready for your review."})
        db.session.commit()
    except WordTaskStopped:
        db.session.rollback()
    except Exception as exc:
        db.session.rollback()
        message = str(exc) if isinstance(exc, ValueError) else "LexText could not finish this task. Retry or contact support."
        transition(draft_id, {"queued", "running"}, "failed", message=message)
        # Do not log documents, model responses, bearer tokens, or exception bodies.
        current_app.logger.warning("Word operation failed (%s)", type(exc).__name__)
    finally:
        try:
            if claimed:
                redis_client().eval(RELEASE, 1, f"word:active:{user.id}", draft.public_id)
        except Exception:
            current_app.logger.warning("Word active lease cleanup deferred to expiry")
