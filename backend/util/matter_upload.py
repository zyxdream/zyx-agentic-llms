"""Helpers for Case Background (matter-level) file uploads."""

from __future__ import annotations

import os
import uuid

from sqlalchemy import func, or_

from backend.database import db
from backend.models import UploadedFiles
from backend.util.data_tools import allowed_filetypes as is_allowed_filetype, is_office_lock_filename

MAX_MATTER_FILE_SIZE = 100 * 1024 * 1024
MAX_MATTER_FILE_SIZE_MB = 100
MAX_FILES_PER_MATTER = 30
MAX_MATTER_TOTAL_PAGES = None


def matter_background_files_filter(project_id: int):
    """SQLAlchemy filter: project-level Case Background corpus (excludes Doc Search)."""
    return (
        UploadedFiles.project_id == int(project_id),
        UploadedFiles.draft_id.is_(None),
        or_(UploadedFiles.source != "search", UploadedFiles.source.is_(None)),
    )


def matter_background_counts(project_id: int) -> tuple[int, int]:
    """Return (file_count, page_count) for Case Background files only."""
    filt = matter_background_files_filter(project_id)
    file_count = UploadedFiles.query.filter(*filt).count()
    page_count = db.session.query(
        func.coalesce(func.sum(UploadedFiles.page_count), 0)
    ).filter(*filt).scalar()
    return file_count, int(page_count or 0)


def classify_matter_upload_indices(
    files: list,
    relative_paths: list | None = None,
) -> tuple[list[int], list]:
    """
    Return (allowed_indices, skipped_names) for a multipart file list.
    relative_paths optional; when provided, lock files on path are skipped too.
    """
    if relative_paths is None:
        relative_paths = [None] * len(files)
    while len(relative_paths) < len(files):
        relative_paths.append(None)

    allowed_indices = []
    skipped_files = []
    for i, file in enumerate(files):
        rel_path = relative_paths[i]
        if not file.filename or not is_allowed_filetype(file.filename):
            skipped_files.append(file.filename or "unknown")
        elif is_office_lock_filename(file.filename) or is_office_lock_filename(rel_path):
            skipped_files.append(rel_path or file.filename or "unknown")
        else:
            allowed_indices.append(i)
    return allowed_indices, skipped_files


def stage_matter_upload_files(
    files: list,
    allowed_indices: list[int],
    relative_paths: list | None = None,
    *,
    max_file_size: int = MAX_MATTER_FILE_SIZE,
) -> tuple[list[dict], list]:
    """
    Save allowed files to temp disk for Celery processing.

    Returns (staged_files, failed_files).
    staged_files entries: {filename, relative_path, temp_path}
    """
    if relative_paths is None:
        relative_paths = [None] * len(files)
    while len(relative_paths) < len(files):
        relative_paths.append(None)

    staged_files = []
    failed_files = []

    for i in allowed_indices:
        file = files[i]
        rel_path = relative_paths[i]
        safe_filename = os.path.basename(file.filename)

        file.seek(0, os.SEEK_END)
        file_size = file.tell()
        file.seek(0)
        if max_file_size is not None and file_size > max_file_size:
            failed_files.append({
                "filename": file.filename,
                "error": f"File too large ({file_size / (1024*1024):.1f}MB > {MAX_MATTER_FILE_SIZE_MB}MB)",
                "reason": "file_too_large",
            })
            continue

        stage_id = f"{uuid.uuid4()}_{safe_filename}"
        temp_path = os.path.join("backend/static", f"temp_{stage_id}")
        file.save(temp_path)
        staged_files.append({
            "filename": safe_filename,
            "relative_path": rel_path,
            "temp_path": temp_path,
        })

    return staged_files, failed_files


def cleanup_staged_files(staged_files: list[dict]) -> None:
    for f in staged_files:
        p = f.get("temp_path")
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def enqueue_matter_upload_batch(
    *,
    project_id: int,
    staged_files: list[dict],
    skipped_files: list,
    existing_page_count: int,
    folder_id: int | None = None,
    max_total_pages: int | None = MAX_MATTER_TOTAL_PAGES,
    failed_files: list | None = None,
):
    """
    Build payload and enqueue upload_batch_task. Caller must register task_meta in Redis.
    Returns Celery AsyncResult.
    """
    from backend.upload_tasks import upload_batch_task

    payload = {
        "project_id": int(project_id),
        "source": "everything",
        "folder_id": folder_id,
        "existing_page_count": existing_page_count,
        "max_total_pages": max_total_pages,
        "files": staged_files,
        "skipped_files": list(skipped_files),
        "failed_files": list(failed_files or []),
    }
    return upload_batch_task.delay(payload)


def enqueue_matter_upload_file_tasks(
    *,
    project_id: int,
    staged_files: list[dict],
    folder_id: int | None = None,
) -> list[tuple[dict, object]]:
    """
    Enqueue one Case Background Celery task per staged file.

    Returns [(staged_file, AsyncResult), ...].
    """
    from backend.upload_tasks import case_background_upload_file_task

    enqueued = []
    for idx, staged_file in enumerate(staged_files):
        payload = {
            "project_id": int(project_id),
            "folder_id": folder_id,
            "file": staged_file,
        }
        try:
            enqueued.append((staged_file, case_background_upload_file_task.delay(payload)))
        except Exception:
            cleanup_staged_files(staged_files[idx:])
            raise
    return enqueued
