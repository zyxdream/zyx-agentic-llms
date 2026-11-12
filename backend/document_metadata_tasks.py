from celery import shared_task

from backend.ai import base as ai_base
from backend.database import db
from backend.models import UploadedFiles
from backend.util.document_metadata import (
    default_document_metadata,
    extract_document_metadata,
    serialize_metadata,
)
from backend.util.account_access import (
    AccountUnavailable, chat_account_id, project_account_id, require_enabled_account,
)


@shared_task(bind=True, ignore_result=False, soft_time_limit=300, time_limit=360)
def document_metadata_task(self, uploaded_file_id: int) -> dict:
    """Best-effort metadata extraction for an already parsed/uploaded file."""
    from backend.util.document_preparation import enabled, dispatch_document
    if enabled():
        from backend.models import DocumentPreparation
        preparation = DocumentPreparation.query.filter_by(uploaded_file_id=uploaded_file_id).first()
        if preparation:
            dispatch_document(preparation.id)
            return {'ok': preparation.status == 'ready', 'reason': 'managed_document_preparation'}
    uploaded_file = UploadedFiles.query.get(uploaded_file_id)
    if not uploaded_file:
        return {
            "ok": False,
            "uploaded_file_id": uploaded_file_id,
            "reason": "file_not_found",
        }

    from backend.models import Drafts
    draft = db.session.get(Drafts, uploaded_file.draft_id) if uploaded_file.draft_id else None
    if ((draft and draft.docCategory == 69) or (uploaded_file.project_id and not uploaded_file.draft_id)) and uploaded_file.source != 'search':
        return {'ok': False, 'uploaded_file_id': uploaded_file_id, 'reason': 'backfill_required'}

    try:
        owner_id = (chat_account_id(uploaded_file.chat_thread_id) if uploaded_file.chat_thread_id
                    else project_account_id(uploaded_file.project_id))
        require_enabled_account(owner_id)
        metadata_kwargs = {
            "parsed_text": uploaded_file.content or "",
            "filename": uploaded_file.original_filename or "",
        }
        metadata_kwargs["model"] = ai_base.DEFAULT_MODEL
        metadata_kwargs["reasoning_effort"] = "medium"
        metadata = extract_document_metadata(**metadata_kwargs)
        require_enabled_account(owner_id)
        uploaded_file.metadata_json = serialize_metadata(metadata)
        db.session.commit()
        print(
            "[document_metadata] ready: "
            f"uploaded_file_id={uploaded_file.id}, unique_id={uploaded_file.unique_id}, "
            f"document_kind={metadata.get('document_kind')}"
        )
        return {
            "ok": True,
            "uploaded_file_id": uploaded_file.id,
            "unique_id": uploaded_file.unique_id,
            "metadata": metadata,
        }
    except AccountUnavailable as exc:
        db.session.rollback()
        return {'ok': False, 'uploaded_file_id': uploaded_file_id,
                'reason': 'account_unavailable', 'error': str(exc)}
    except Exception as exc:
        db.session.rollback()
        failed = default_document_metadata("failed")
        try:
            uploaded_file = UploadedFiles.query.get(uploaded_file_id)
            if uploaded_file:
                uploaded_file.metadata_json = serialize_metadata(failed)
                db.session.commit()
        except Exception:
            db.session.rollback()
        print(
            "[document_metadata] failed: "
            f"uploaded_file_id={uploaded_file_id}, error={exc}"
        )
        return {
            "ok": False,
            "uploaded_file_id": uploaded_file_id,
            "reason": "metadata_failed",
            "error": str(exc),
            "metadata": failed,
        }
