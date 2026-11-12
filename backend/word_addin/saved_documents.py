"""Exact, owner-scoped Word snapshots using existing draft storage (no migration)."""
import base64
import binascii
import hashlib
import io
import re
import uuid
import zipfile
import zlib
from datetime import datetime, timezone
from xml.etree import ElementTree

from flask import send_file
from flask_cognito import current_user
from sqlalchemy import select

from backend.database import db
from backend.models import ChatThread, Project

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_SAVE_REQUEST_BYTES = 14 * 1024 * 1024
MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def decode_document(encoded):
    if not isinstance(encoded, str) or len(encoded) > (MAX_DOCUMENT_BYTES + 2) // 3 * 4:
        raise ValueError("Save a Word document of 10 MB or less.")
    try:
        data = base64.b64decode(encoded, validate=True)
        if not data or len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if (len(entries) > 2000 or len(set(names)) != len(names)
                    or sum(item.file_size for item in entries) > 100 * 1024 * 1024
                    or any(item.file_size > 20 * 1024 * 1024 or item.flag_bits & 1 for item in entries)
                    or any("vbaproject" in name.lower() for name in names)):
                raise ValueError()
            types_xml = archive.read("[Content_Types].xml")
            document_xml = archive.read("word/document.xml")
            if any(b"<!DOCTYPE" in xml or b"<!ENTITY" in xml for xml in (types_xml, document_xml)):
                raise ValueError()
            types = ElementTree.fromstring(types_xml)
            if not any(item.get("PartName") == "/word/document.xml" and item.get("ContentType") ==
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
                       for item in types):
                raise ValueError()
            if ElementTree.fromstring(document_xml).tag != "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document":
                raise ValueError()
            if archive.testzip() is not None:
                raise ValueError()
        return data
    except (ValueError, binascii.Error, zipfile.BadZipFile, KeyError, ElementTree.ParseError, RuntimeError, NotImplementedError, zlib.error, OSError):
        raise ValueError("Word could not supply a valid .docx copy. Save as .docx in Word and try again.") from None


def register_saved_documents(bp, authenticated, body, entry, set_entry, operation_for_user):
    @bp.post("/documents")
    @authenticated
    def save_document():
        value = body()
        project_id = value.get("project_id")
        if isinstance(project_id, bool) or not isinstance(project_id, int):
            raise ValueError("Choose a matter for this copy.")
        # Serialize saves in this matter, including simultaneous retries. The
        # snapshot UUID is also the unique draft ID, so retries cannot duplicate it.
        project = db.session.execute(select(Project).where(
            Project.id == project_id, Project.userId == current_user.id).with_for_update()).scalar_one_or_none()
        if not project:
            return {"error": "Matter not found."}, 404
        try:
            document_id = str(uuid.UUID(value.get("request_id", "")))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid save request. Please start a new save.") from None
        filename = value.get("filename")
        if (not isinstance(filename, str) or not filename.strip() or len(filename) > 180
                or re.search(r'[\x00-\x1f\x7f/\\]', filename) or not filename.lower().endswith(".docx")):
            raise ValueError("Use a .docx filename of up to 180 characters without slashes.")
        filename = filename.strip()
        data = decode_document(value.get("content"))
        digest = hashlib.sha256(data).hexdigest()
        existing = ChatThread.query.filter_by(public_id=document_id).first()
        if existing:
            metadata = entry(existing.id, "word.document") if existing.projectId == project.id else None
            if not metadata or metadata["digest"] != digest or metadata["filename"] != filename:
                return {"error": "This save request was already used. Start a new save."}, 409
            return metadata
        draft = ChatThread(public_id=document_id, display_name=f"Word copy · {filename}", docCategory=69, projectId=project.id)
        db.session.add(draft)
        db.session.flush()
        metadata = {"id": document_id, "filename": filename, "size": len(data), "digest": digest,
                    "saved_at": datetime.now(timezone.utc).isoformat(), "project_id": project.id,
                    "matter_name": project.name, "path": f"/word-documents/{document_id}"}
        set_entry(draft.id, "word.document", metadata)
        set_entry(draft.id, "word.document.data", base64.b64encode(data).decode("ascii"))
        from backend.word_addin.routes import publish_workspace_result
        publish_workspace_result(draft.id, {"draft": {"title": draft.display_name, "sections": []},
                  "chat_history": [["Save a copy from Word", "Your Word document was saved as a separate copy, including its current tracked changes. "
                                    f"[Open and download the saved Word document](/word-documents/{document_id}). "
                                    "Later edits in Word are not synced automatically."]],
                  "chat_history_meta": [{"entry_point": "word_addin"}], "validated_cases": {},
                  "uploaded_files": {}, "constraints": {}})
        db.session.commit()
        return metadata, 201

    @bp.get("/documents/<document_id>")
    @authenticated
    def document_metadata(document_id):
        draft = operation_for_user(document_id)
        metadata = entry(draft.id, "word.document") if draft else None
        return metadata if metadata else ({"error": "Saved Word copy not found."}, 404)

    @bp.get("/documents/<document_id>/file")
    @authenticated
    def download_document(document_id):
        draft = operation_for_user(document_id)
        metadata = entry(draft.id, "word.document") if draft else None
        if not metadata:
            return {"error": "Saved Word copy not found."}, 404
        data = base64.b64decode(entry(draft.id, "word.document.data"))
        return send_file(io.BytesIO(data), mimetype=MIME, as_attachment=True,
                         download_name=metadata["filename"], max_age=0)
