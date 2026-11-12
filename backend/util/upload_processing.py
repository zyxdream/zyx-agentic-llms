import os
import uuid

from sqlalchemy import or_

from backend.database import db
from backend.models import UploadedFiles, DocumentChunk
from backend.util.chunker import chunk_text
from backend.util.data_tools import get_text_from_extended_formats
from backend.util.document_metadata import default_document_metadata, serialize_metadata
from backend.ai.document_parser import get_pagenum_from_pdf_fitz
from backend.util.account_access import project_account_id, require_enabled_account


def _estimate_page_count(safe_filename: str, temp_path: str, parsed_text: str) -> int:
    if safe_filename.lower().endswith(".pdf"):
        try:
            return get_pagenum_from_pdf_fitz(temp_path)
        except Exception:
            pass
    words = len(parsed_text.split()) if parsed_text else 0
    return max(1, round(words / 400)) if words else 0


def process_project_uploaded_file(
    *,
    temp_path: str,
    safe_filename: str,
    project_id: int,
    source: str,
    folder_upload_id: int | None = None,
    relative_path: str | None = None,
    max_total_pages: int | None = None,
    running_page_count: int = 0,
    max_project_file_count: int | None = None,
) -> dict:
    """
    End-to-end ingestion for one staged file.

    Concrete actions performed in order:
    1) Parse raw file bytes into normalized text (PDF/TXT/DOC/EML/IMG path-specific parsing).
    2) Derive page count (real PDF page count when possible; otherwise estimate from word count).
    3) Enforce project-level page budget guard before any DB writes.
    4) Insert one UploadedFiles row and commit immediately (per-file durability boundary).
    5) Chunk parsed text, insert DocumentChunk rows, and commit chunk writes.
    6) Return a compact payload describing the persisted file and updated running totals.

    Why commit per file:
    - If later files fail or task times out, already-committed files remain durable.
    """
    require_enabled_account(project_account_id(project_id))
    parsed_text, page_count = parse_project_uploaded_file(
        temp_path=temp_path,
        safe_filename=safe_filename,
    )

    return persist_project_uploaded_file(
        temp_path=temp_path,
        safe_filename=safe_filename,
        parsed_text=parsed_text,
        page_count=page_count,
        project_id=project_id,
        source=source,
        folder_upload_id=folder_upload_id,
        relative_path=relative_path,
        max_total_pages=max_total_pages,
        running_page_count=running_page_count,
        max_project_file_count=max_project_file_count,
    )


def parse_project_uploaded_file(*, temp_path: str, safe_filename: str) -> tuple[str, int]:
    """Parse a staged upload and derive its page count without writing DB rows."""
    parsed_text, _case_number, _is_image = get_text_from_extended_formats(temp_path)

    page_count = _estimate_page_count(safe_filename=safe_filename, temp_path=temp_path, parsed_text=parsed_text)
    return parsed_text, page_count


def _matter_background_file_count(project_id: int) -> int:
    return UploadedFiles.query.filter(
        UploadedFiles.project_id == int(project_id),
        UploadedFiles.draft_id.is_(None),
        or_(UploadedFiles.source != "search", UploadedFiles.source.is_(None)),
    ).count()


def persist_project_uploaded_file(
    *,
    temp_path: str,
    safe_filename: str,
    parsed_text: str,
    page_count: int,
    project_id: int,
    source: str,
    folder_upload_id: int | None = None,
    relative_path: str | None = None,
    max_total_pages: int | None = None,
    running_page_count: int = 0,
    max_project_file_count: int | None = None,
) -> dict:
    """Persist one parsed project upload, enforcing optional quota guards first."""
    require_enabled_account(project_account_id(project_id))
    if max_total_pages is not None and running_page_count + page_count > max_total_pages:
        return {
            "ok": False,
            "reason": "page_limit",
            "error": f"Would exceed {max_total_pages} page limit",
            "filename": safe_filename,
            "running_page_count": running_page_count,
        }

    if max_project_file_count is not None:
        current_file_count = _matter_background_file_count(project_id)
        if current_file_count >= max_project_file_count:
            return {
                "ok": False,
                "reason": "file_count_limit",
                "error": f"Maximum {max_project_file_count} files allowed per matter.",
                "filename": safe_filename,
                "running_page_count": running_page_count,
            }

    # Generate a unique, traceable source id. This id is reused by chunk rows so
    # downstream retrieval can map chunks back to the exact uploaded file.
    unique_id = f"{uuid.uuid4()}_{safe_filename}"

    # Persist one UploadedFiles record (canonical file-level row).
    # This commit is an intentional durability boundary: one file succeeds/fails
    # independently from other files in the batch.
    uploaded_file = UploadedFiles(
        unique_id=unique_id,
        original_filename=safe_filename,
        file_size=os.path.getsize(temp_path),
        page_count=page_count,
        content=parsed_text,
        metadata_json=serialize_metadata(default_document_metadata("pending")),
        project_id=project_id,
        folder_upload_id=folder_upload_id,
        relative_path=relative_path,
        source=source,
    )
    db.session.add(uploaded_file)
    db.session.commit()

    # Step 5: Build retrieval chunks from parsed text and persist them.
    # Chunks are written after UploadedFiles so they can reference uploaded_file.id.
    chunks = chunk_text(parsed_text)
    chunk_count = 0
    if chunks:
        for c in chunks:
            db.session.add(DocumentChunk(
                uploaded_file_id=uploaded_file.id,
                project_id=project_id,
                chunk_index=c["chunk_index"],
                content=c["content"],
                token_count=c["token_count"],
                char_count=c["char_count"],
                source_filename=safe_filename,
                source_relative_path=relative_path,
                source_unique_id=unique_id,
            ))
        db.session.commit()
        chunk_count = len(chunks)

    # Step 6: Return structured result for task-level aggregation/progress reporting.
    return {
        "ok": True,
        "filename": safe_filename,
        "uploaded_file_id": uploaded_file.id,
        "unique_id": unique_id,
        "relative_path": relative_path,
        "page_count": page_count,
        "chunk_count": chunk_count,
        "running_page_count": running_page_count + page_count,
    }
