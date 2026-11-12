import os
import uuid
import logging

import redis as redis_lib
from celery import shared_task
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import func

from backend.constants import REDIS_URL
from backend.database import db
from backend.models import FolderUpload, UploadedFiles, DocumentChunk, ChatThread
from backend.document_metadata_tasks import document_metadata_task
from backend.util.data_tools import allowed_filetypes, is_office_lock_filename
from backend.util.upload_processing import (
    parse_project_uploaded_file,
    persist_project_uploaded_file,
    process_project_uploaded_file,
)
from backend.util.data_tools import get_complaint_text_from_file
from backend.ai.complaint_parser import get_pagenum_from_pdf_fitz, get_last_ocr_warning
from backend.util.chunker import chunk_text
from backend.util.db import DBMemory
from backend.util.document_metadata import default_document_metadata, parse_metadata, serialize_metadata
from backend.everything import load_state_from_memory, save_state_to_memory
from backend.util.matter_upload import MAX_FILES_PER_MATTER
from backend.util.account_access import (
    AccountUnavailable, chat_account_id, project_account_id, require_enabled_account,
)


_upload_redis = redis_lib.from_url(REDIS_URL)
MATTER_UPLOAD_LOCK_TTL_SECONDS = 300
MATTER_UPLOAD_LOCK_WAIT_SECONDS = 60
CASE_ASSISTANT_MAX_FILE_SIZE = 50 * 1024 * 1024
CASE_ASSISTANT_MAX_FILE_SIZE_MB = 50
CASE_ASSISTANT_MAX_FILES = 100
CASE_ASSISTANT_MAX_TOTAL_BYTES = 1024**3
logger = logging.getLogger(__name__)


def _upload_status(message: str, processed: int, total: int) -> dict:
    return {
        "status": message,
        "processed_files": processed,
        "total_files": total,
    }


def _folder_upload_counts(folder_id: int) -> tuple[int, int]:
    folder_file_count = UploadedFiles.query.filter_by(folder_upload_id=folder_id).count()
    folder_total_chunks = db.session.query(func.count(DocumentChunk.id)).join(
        UploadedFiles, DocumentChunk.uploaded_file_id == UploadedFiles.id
    ).filter(
        UploadedFiles.folder_upload_id == folder_id
    ).scalar() or 0
    folder_upload = FolderUpload.query.get(folder_id)
    if folder_upload:
        folder_upload.file_count = folder_file_count
        folder_upload.total_chunks = folder_total_chunks
        db.session.commit()
    return folder_file_count, folder_total_chunks


def _project_upload_result(
    *,
    folder_id: int | None,
    result_files: list[dict],
    failed_files: list,
    skipped_files: list,
    total_chunks: int,
    ocr_warning_count: int,
    timed_out: bool,
    total_files: int,
) -> dict:
    processed_files = len(result_files) + len(failed_files) + len(skipped_files)
    if folder_id:
        folder_file_count, folder_total_chunks = _folder_upload_counts(folder_id)
        return {
            "folder_id": folder_id,
            "batch_file_count": len(result_files),
            "batch_total_chunks": total_chunks,
            "file_count": folder_file_count,
            "total_chunks": folder_total_chunks,
            "files": result_files,
            "skipped_files": skipped_files,
            "failed_files": failed_files,
            "ocr_warning_count": ocr_warning_count,
            "timed_out": timed_out,
            "processed_files": processed_files,
            "total_files": total_files,
        }

    return {
        "file_count": len(result_files),
        "total_chunks": total_chunks,
        "files": result_files,
        "skipped_files": skipped_files,
        "failed_files": failed_files,
        "ocr_warning_count": ocr_warning_count,
        "timed_out": timed_out,
        "processed_files": processed_files,
        "total_files": total_files,
    }


@shared_task(bind=True, ignore_result=False, soft_time_limit=3600, time_limit=3660)
def upload_batch_task(self, payload: dict) -> dict:
    """
    Process one uploaded batch (one frontend request) inside Celery.

    Concrete workflow:
    A) Read staged file metadata from payload (temp paths + filenames + optional folder path).
    B) Iterate files one-by-one and update task progress for polling UIs.
    C) For each file, call the reusable single-file ingestion pipeline.
    D) Aggregate success/failure/skipped counters and keep processing remaining files.
    E) On soft timeout, stop processing new files and return partial results.
    F) Recompute/update folder aggregate counters (when folder mode is used).
    G) Return a compact summary object consumed by frontend batch summaries.

    Design intent:
    - Commit-per-file durability
    - Partial success even when some files fail or timeout
    - Same pipeline reusable by other features (e.g., everything.py upload paths)
    """
    project_id = int(payload["project_id"])
    source = payload.get("source", "search")
    folder_id = payload.get("folder_id")
    max_total_pages = payload.get("max_total_pages")
    running_page_count = int(payload.get("existing_page_count") or 0)
    files = list(payload.get("files") or [])
    skipped_files = list(payload.get("skipped_files") or [])

    result_files = []
    failed_files = list(payload.get("failed_files") or [])
    total_chunks = 0
    ocr_warning_count = 0
    processed_count = 0
    total_count = len(files)
    timed_out = False

    try:
        # Main loop: process every staged file in this batch independently.
        # Independent handling keeps one bad file from failing the whole batch.
        for file_meta in files:
            safe_filename = file_meta.get("filename") or "unknown"
            rel_path = file_meta.get("relative_path")
            temp_path = file_meta.get("temp_path")

            if is_office_lock_filename(safe_filename) or is_office_lock_filename(rel_path):
                skipped_files.append(rel_path or safe_filename)
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                continue

            processed_count += 1

            # Emit progress metadata so polling endpoints can show live status.
            self.update_state(
                state="PROGRESS",
                meta=_upload_status(
                    f"Processing {processed_count}/{total_count}: {safe_filename}",
                    processed=processed_count,
                    total=total_count,
                ),
            )

            # Defensive backend validation: even if frontend filtered extensions,
            # we still enforce allowlist at worker level.
            if not safe_filename or not allowed_filetypes(safe_filename):
                skipped_files.append(safe_filename or "unknown")
                continue

            try:
                require_enabled_account(project_account_id(project_id))
                # Validate staged temp file exists before parsing.
                # Missing temp files indicate staging or lifecycle issue.
                if not temp_path or not os.path.exists(temp_path):
                    failed_files.append({
                        "filename": safe_filename,
                        "error": "Temporary file missing before processing",
                        "reason": "temp_missing",
                    })
                    continue

                # Core ingestion step for one file (parse -> page count -> DB rows -> chunks).
                res = process_project_uploaded_file(
                    temp_path=temp_path,
                    safe_filename=safe_filename,
                    project_id=project_id,
                    source=source,
                    folder_upload_id=folder_id,
                    relative_path=rel_path,
                    max_total_pages=max_total_pages,
                    running_page_count=running_page_count,
                )
                if res.get("ok"):
                    # Success path: roll forward counters used by later files and final summary.
                    if get_last_ocr_warning():
                        ocr_warning_count += 1
                    running_page_count = int(res["running_page_count"])
                    total_chunks += int(res["chunk_count"])
                    result_files.append({
                        "unique_id": res["unique_id"],
                        "filename": res["filename"],
                        "relative_path": res.get("relative_path"),
                        "page_count": int(res["page_count"]),
                        "chunk_count": int(res["chunk_count"]),
                    })
                    try:
                        document_metadata_task.delay(int(res["uploaded_file_id"]))
                    except Exception as exc:
                        logger.warning(
                            "[document_metadata] enqueue failed: "
                            "uploaded_file_id=%s, error=%s",
                            res.get('uploaded_file_id'),
                            exc,
                        )
                    if folder_id:
                        logger.info(
                            "Smart search folder upload - processed: %s (%s pages, %s chunks)",
                            safe_filename,
                            res['page_count'],
                            res['chunk_count'],
                        )
                    else:
                        logger.info(
                            "Smart search file upload - processed: %s (%s pages, %s chunks)",
                            safe_filename,
                            res['page_count'],
                            res['chunk_count'],
                        )
                else:
                    # Business-rule rejection path (e.g., page limit) from shared processor.
                    failed_files.append({
                        "filename": safe_filename,
                        "error": res.get("error", "Processing failed"),
                        "reason": res.get("reason", "processing_error"),
                    })
            except Exception as exc:
                # Hard parser/DB error for this file only: rollback current transaction
                # and continue with remaining files.
                db.session.rollback()
                failed_files.append({
                    "filename": safe_filename,
                    "error": str(exc),
                    "reason": "account_unavailable" if isinstance(exc, AccountUnavailable) else "processing_error",
                })
            finally:
                # Always attempt cleanup to avoid orphaned temp files on disk.
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
    except SoftTimeLimitExceeded:
        # Soft timeout indicates worker should stop soon; return partial progress
        # rather than raising and losing batch-level summary visibility.
        timed_out = True

    if folder_id:
        # Folder mode requires refreshed aggregates across all files currently attached
        # to the folder (including files from previous batches).
        folder_file_count = UploadedFiles.query.filter_by(folder_upload_id=folder_id).count()
        folder_total_chunks = db.session.query(func.count(DocumentChunk.id)).join(
            UploadedFiles, DocumentChunk.uploaded_file_id == UploadedFiles.id
        ).filter(
            UploadedFiles.folder_upload_id == folder_id
        ).scalar() or 0
        folder_upload = FolderUpload.query.get(folder_id)
        if folder_upload:
            folder_upload.file_count = folder_file_count
            folder_upload.total_chunks = folder_total_chunks
            db.session.commit()

        # Folder response contains both current-batch and folder-total counters.
        return {
            "folder_id": folder_id,
            "batch_file_count": len(result_files),
            "batch_total_chunks": total_chunks,
            "file_count": folder_file_count,
            "total_chunks": folder_total_chunks,
            "files": result_files,
            "skipped_files": skipped_files,
            "failed_files": failed_files,
            "ocr_warning_count": ocr_warning_count,
            "timed_out": timed_out,
            "processed_files": len(result_files) + len(failed_files) + len(skipped_files),
            "total_files": total_count + len(skipped_files),
        }

    # Loose-file mode returns only current batch counters.
    return {
        "file_count": len(result_files),
        "total_chunks": total_chunks,
        "files": result_files,
        "skipped_files": skipped_files,
        "failed_files": failed_files,
        "ocr_warning_count": ocr_warning_count,
        "timed_out": timed_out,
        "processed_files": len(result_files) + len(failed_files) + len(skipped_files),
        "total_files": total_count + len(skipped_files),
    }


@shared_task(bind=True, ignore_result=False, soft_time_limit=3600, time_limit=3660)
def smart_search_upload_file_task(self, payload: dict) -> dict:
    """Asynchronously process one Doc Search / Smart Search file upload."""
    project_id = int(payload["project_id"])
    folder_id = payload.get("folder_id")
    if folder_id is not None:
        folder_id = int(folder_id)
    file_meta = payload.get("file") or {}
    safe_filename = file_meta.get("filename") or "unknown"
    rel_path = file_meta.get("relative_path")
    temp_path = file_meta.get("temp_path")

    result_files = []
    failed_files = []
    skipped_files = []
    total_chunks = 0
    ocr_warning_count = 0
    timed_out = False

    try:
        require_enabled_account(project_account_id(project_id))
        self.update_state(
            state="PROGRESS",
            meta=_upload_status(f"Processing 1/1: {safe_filename}", processed=1, total=1),
        )

        if is_office_lock_filename(safe_filename) or is_office_lock_filename(rel_path):
            skipped_files.append(rel_path or safe_filename)
        elif not safe_filename or not allowed_filetypes(safe_filename):
            skipped_files.append(safe_filename or "unknown")
        elif not temp_path or not os.path.exists(temp_path):
            failed_files.append({
                "filename": safe_filename,
                "error": "Temporary file missing before processing",
                "reason": "temp_missing",
            })
        else:
            res = process_project_uploaded_file(
                temp_path=temp_path,
                safe_filename=safe_filename,
                project_id=project_id,
                source="search",
                folder_upload_id=folder_id,
                relative_path=rel_path,
                max_total_pages=None,
                running_page_count=0,
            )
            if res.get("ok"):
                if get_last_ocr_warning():
                    ocr_warning_count += 1
                total_chunks = int(res["chunk_count"])
                result_files.append({
                    "unique_id": res["unique_id"],
                    "filename": res["filename"],
                    "relative_path": res.get("relative_path"),
                    "page_count": int(res["page_count"]),
                    "chunk_count": int(res["chunk_count"]),
                })
                try:
                    document_metadata_task.delay(int(res["uploaded_file_id"]))
                except Exception as exc:
                    logger.warning(
                        "[document_metadata] enqueue failed: "
                        "uploaded_file_id=%s, error=%s",
                        res.get('uploaded_file_id'),
                        exc,
                    )
                logger.info(
                    "Smart search upload - processed: %s (%s pages, %s chunks)",
                    safe_filename,
                    res['page_count'],
                    res['chunk_count'],
                )
            else:
                failed_files.append({
                    "filename": safe_filename,
                    "error": res.get("error", "Processing failed"),
                    "reason": res.get("reason", "processing_error"),
                })
    except SoftTimeLimitExceeded:
        timed_out = True
        failed_files.append({
            "filename": safe_filename,
            "error": "Upload worker timed out while processing this file.",
            "reason": "task_timeout",
        })
    except Exception as exc:
        db.session.rollback()
        failed_files.append({
            "filename": safe_filename,
            "error": str(exc),
            "reason": "account_unavailable" if isinstance(exc, AccountUnavailable) else "processing_error",
        })
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

    return _project_upload_result(
        folder_id=folder_id,
        result_files=result_files,
        failed_files=failed_files,
        skipped_files=skipped_files,
        total_chunks=total_chunks,
        ocr_warning_count=ocr_warning_count,
        timed_out=timed_out,
        total_files=1,
    )


@shared_task(bind=True, ignore_result=False, soft_time_limit=3600, time_limit=3660)
def case_background_upload_file_task(self, payload: dict) -> dict:
    """Asynchronously process one Case Background file upload."""
    project_id = int(payload["project_id"])
    folder_id = payload.get("folder_id")
    if folder_id is not None:
        folder_id = int(folder_id)
    file_meta = payload.get("file") or {}
    safe_filename = file_meta.get("filename") or "unknown"
    rel_path = file_meta.get("relative_path")
    temp_path = file_meta.get("temp_path")

    result_files = []
    failed_files = []
    skipped_files = []
    total_chunks = 0
    ocr_warning_count = 0
    timed_out = False

    try:
        require_enabled_account(project_account_id(project_id))
        self.update_state(
            state="PROGRESS",
            meta=_upload_status(f"Processing 1/1: {safe_filename}", processed=1, total=1),
        )

        if is_office_lock_filename(safe_filename) or is_office_lock_filename(rel_path):
            skipped_files.append(rel_path or safe_filename)
        elif not safe_filename or not allowed_filetypes(safe_filename):
            skipped_files.append(safe_filename or "unknown")
        elif not temp_path or not os.path.exists(temp_path):
            failed_files.append({
                "filename": safe_filename,
                "error": "Temporary file missing before processing",
                "reason": "temp_missing",
            })
        else:
            parsed_text, page_count = parse_project_uploaded_file(
                temp_path=temp_path,
                safe_filename=safe_filename,
            )
            self.update_state(
                state="PROGRESS",
                meta=_upload_status(f"Saving 1/1: {safe_filename}", processed=1, total=1),
            )

            lock = _upload_redis.lock(
                f"matter_background_upload:{project_id}:file_count",
                timeout=MATTER_UPLOAD_LOCK_TTL_SECONDS,
                blocking_timeout=MATTER_UPLOAD_LOCK_WAIT_SECONDS,
            )
            if not lock.acquire():
                failed_files.append({
                    "filename": safe_filename,
                    "error": "Could not acquire matter upload lock before processing",
                    "reason": "upload_lock_timeout",
                })
            else:
                try:
                    res = persist_project_uploaded_file(
                        temp_path=temp_path,
                        safe_filename=safe_filename,
                        parsed_text=parsed_text,
                        page_count=page_count,
                        project_id=project_id,
                        source="everything",
                        folder_upload_id=folder_id,
                        relative_path=rel_path,
                        max_total_pages=None,
                        running_page_count=0,
                        max_project_file_count=MAX_FILES_PER_MATTER,
                    )
                finally:
                    try:
                        lock.release()
                    except Exception:
                        pass

                if res.get("ok"):
                    if get_last_ocr_warning():
                        ocr_warning_count += 1
                    total_chunks = int(res["chunk_count"])
                    result_files.append({
                        "unique_id": res["unique_id"],
                        "filename": res["filename"],
                        "relative_path": res.get("relative_path"),
                        "page_count": int(res["page_count"]),
                        "chunk_count": int(res["chunk_count"]),
                    })
                    try:
                        document_metadata_task.delay(int(res["uploaded_file_id"]))
                    except Exception as exc:
                        logger.warning(
                            "[document_metadata] enqueue failed: "
                            "uploaded_file_id=%s, error=%s",
                            res.get('uploaded_file_id'),
                            exc,
                        )
                    logger.info(
                        "Case Background upload - processed: %s (%s pages, %s chunks)",
                        safe_filename,
                        res['page_count'],
                        res['chunk_count'],
                    )
                else:
                    failed_files.append({
                        "filename": safe_filename,
                        "error": res.get("error", "Processing failed"),
                        "reason": res.get("reason", "processing_error"),
                    })
    except SoftTimeLimitExceeded:
        timed_out = True
        failed_files.append({
            "filename": safe_filename,
            "error": "Upload worker timed out while processing this file.",
            "reason": "task_timeout",
        })
    except Exception as exc:
        db.session.rollback()
        failed_files.append({
            "filename": safe_filename,
            "error": str(exc),
            "reason": "account_unavailable" if isinstance(exc, AccountUnavailable) else "processing_error",
        })
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

    return _project_upload_result(
        folder_id=folder_id,
        result_files=result_files,
        failed_files=failed_files,
        skipped_files=skipped_files,
        total_chunks=total_chunks,
        ocr_warning_count=ocr_warning_count,
        timed_out=timed_out,
        total_files=1,
    )


@shared_task(bind=True, ignore_result=False, soft_time_limit=3600, time_limit=3660)
def case_assistant_upload_file_task(self, payload: dict) -> dict:
    """
    Asynchronously process a single Case Assistant task-specific upload.
    """
    draft_id_raw = payload.get("draft_id")
    client_upload_id = payload.get("client_upload_id")
    temp_path = payload.get("temp_path")
    original_filename = payload.get("filename") or "unknown"

    MAX_FILES_PER_DRAFT = CASE_ASSISTANT_MAX_FILES

    safe_filename = os.path.basename(original_filename)

    try:
        if not draft_id_raw:
            return {
                "ok": False,
                "clientUploadId": client_upload_id,
                "reason": "missing_draft_id",
                "error": "Missing draft id",
            }
        if not temp_path or not os.path.exists(temp_path):
            return {
                "ok": False,
                "clientUploadId": client_upload_id,
                "reason": "temp_missing",
                "error": "Temporary file missing before processing",
            }

        draft_id = ChatThread.resolve_id(draft_id_raw)
        owner_id = chat_account_id(draft_id)
        require_enabled_account(owner_id)

        # Re-check limits in worker to avoid edge-case races.
        existing_file_count = UploadedFiles.query.filter_by(chat_thread_id=draft_id).count()
        if existing_file_count >= MAX_FILES_PER_DRAFT:
            return {
                "ok": False,
                "clientUploadId": client_upload_id,
                "reason": "file_count_limit",
                "error": f"Maximum {MAX_FILES_PER_DRAFT} files allowed per chat.",
            }

        file_size = os.path.getsize(temp_path)
        if file_size > CASE_ASSISTANT_MAX_FILE_SIZE:
            return {
                "ok": False,
                "clientUploadId": client_upload_id,
                "reason": "file_too_large",
                "error": f"File too large ({file_size / (1024*1024):.1f}MB > {CASE_ASSISTANT_MAX_FILE_SIZE_MB}MB)",
            }

        parsed_text, _case_number, _is_image = get_complaint_text_from_file(temp_path)
        ocr_warning = get_last_ocr_warning()

        if safe_filename.lower().endswith(".pdf"):
            try:
                page_count = get_pagenum_from_pdf_fitz(temp_path)
            except Exception:
                words = len(parsed_text.split()) if parsed_text else 0
                page_count = max(1, round(words / 400)) if words else 0
        else:
            words = len(parsed_text.split()) if parsed_text else 0
            page_count = max(1, round(words / 400)) if words else 0

        # Parsing runs outside this short write transaction. Serialize the final
        # quota check and insertion so concurrent workers cannot exceed either cap.
        db.session.commit()
        require_enabled_account(owner_id)
        db.session.query(ChatThread).filter_by(id=draft_id).update({ChatThread.id: ChatThread.id})
        used_count, used_bytes = db.session.query(
            func.count(UploadedFiles.id), func.coalesce(func.sum(UploadedFiles.file_size), 0)
        ).filter_by(chat_thread_id=draft_id).one()
        if used_count >= MAX_FILES_PER_DRAFT:
            db.session.rollback()
            return {'ok': False, 'clientUploadId': client_upload_id, 'reason': 'file_count_limit',
                    'error': f'Maximum {MAX_FILES_PER_DRAFT} files allowed per chat.'}
        if used_bytes + file_size > CASE_ASSISTANT_MAX_TOTAL_BYTES:
            db.session.rollback()
            return {'ok': False, 'clientUploadId': client_upload_id, 'reason': 'total_size_limit',
                    'error': 'Maximum 1 GB total attachments per chat.'}

        unique_id = payload.get('unique_id') or f"{uuid.uuid4()}_{safe_filename}"

        uploaded_file = UploadedFiles(
            unique_id=unique_id,
            original_filename=safe_filename,
            file_size=file_size,
            page_count=page_count,
            content=parsed_text,
            metadata_json=serialize_metadata(default_document_metadata("pending")),
            chat_thread_id=draft_id,
        )
        db.session.add(uploaded_file)
        db.session.commit()

        chunks = chunk_text(parsed_text)
        chunk_count = 0
        if chunks:
            for c in chunks:
                db.session.add(DocumentChunk(
                    uploaded_file_id=uploaded_file.id,
                    chat_thread_id=draft_id,
                    chunk_index=c["chunk_index"],
                    content=c["content"],
                    token_count=c["token_count"],
                    char_count=c["char_count"],
                    source_filename=safe_filename,
                    source_relative_path=None,
                    source_unique_id=unique_id,
                ))
            db.session.commit()
            chunk_count = len(chunks)

        mem = DBMemory(db=db, doc_id=draft_id)
        state = load_state_from_memory(mem)
        state.setdefault("uploaded_files", {})
        mem[f"inputs/{unique_id}"] = parsed_text
        state["uploaded_files"][unique_id] = {
            "raw_content": parsed_text,
            "extractions": "",
            "page_count": page_count,
            "filename": safe_filename,
            "metadata": default_document_metadata("pending"),
        }
        total_page_count = sum(
            info.get("page_count", 0) for info in state["uploaded_files"].values()
        )
        state["total_page_count"] = total_page_count
        from backend.util.workspace_store import patch_auxiliary
        latest = patch_auxiliary(draft_id, {'uploaded_files': {unique_id: state['uploaded_files'][unique_id]}},
                                 merge=('uploaded_files',))
        patch_auxiliary(draft_id, {'total_page_count': sum(
            info.get('page_count', 0) for info in latest['uploaded_files'].values())})
        db.session.commit()

        try:
            document_metadata_task.delay(uploaded_file.id)
        except Exception as exc:
            logger.warning(
                "[document_metadata] enqueue failed: "
                "uploaded_file_id=%s, error=%s",
                uploaded_file.id,
                exc,
            )

        return {
            "ok": True,
            "clientUploadId": client_upload_id,
            "unique_id": unique_id,
            "filename": safe_filename,
            "file_size": file_size,
            "page_count": page_count,
            "total_page_count": total_page_count,
            "chunk_count": chunk_count,
            "ocr_warning": ocr_warning,
            "metadata": parse_metadata(uploaded_file.metadata_json),
        }
    except SoftTimeLimitExceeded:
        return {
            "ok": False,
            "clientUploadId": client_upload_id,
            "reason": "task_timeout",
            "error": "Upload worker timed out while processing this file.",
        }
    except Exception as exc:
        db.session.rollback()
        return {
            "ok": False,
            "clientUploadId": client_upload_id,
            "reason": "account_unavailable" if isinstance(exc, AccountUnavailable) else "processing_error",
            "error": str(exc),
        }
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
