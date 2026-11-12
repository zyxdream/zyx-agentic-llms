from flask_login import UserMixin  # is this needed?
from backend.database import db
from sqlalchemy.sql import func
from sqlalchemy import Text
from sqlalchemy.orm import synonym
from sqlalchemy.dialects.mysql import LONGTEXT
import uuid
import re


class User(UserMixin, db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    cognito_username = db.Column(db.String(500))
    contact_info = db.Column(db.String(2000))
    is_disabled = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

class Project(db.Model):
    __tablename__='projects'
    __table_args__ = (db.UniqueConstraint('userId', 'name', name='unique_user_project'),)
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(250))
    date = db.Column(db.DateTime(timezone=True), default=func.now())
    userId = db.Column(db.Integer, db.ForeignKey('user.id'))
    chat_threads = db.relationship('ChatThread', backref='parent', lazy=True, cascade="all, delete, delete-orphan")
    drafts = synonym('chat_threads')  # Legacy guided/Word modules still use this attribute.
    folder_uploads = db.relationship('FolderUpload', backref='project', lazy=True, cascade="all, delete, delete-orphan")
    uploaded_files = db.relationship('UploadedFiles', backref='project_ref', lazy=True,
                                     foreign_keys='UploadedFiles.project_id',
                                     cascade="all, delete")
    document_chunks = db.relationship('DocumentChunk', backref='project_ref', lazy=True,
                                      foreign_keys='DocumentChunk.project_id',
                                      cascade="all, delete")
    smart_search_sessions = db.relationship('SmartSearchSession', backref='project_ref', lazy=True,
                                            foreign_keys='SmartSearchSession.project_id',
                                            cascade="all, delete")
    file_tags = db.relationship('FileTag', backref='project_ref', lazy=True, cascade="all, delete, delete-orphan")
    document_upload_groups = db.relationship('UploadGroup', foreign_keys='UploadGroup.project_id', cascade='all, delete')

    def __repr__(self):
        return f"<user id={self.id}, project name={self.name}, date created={self.date}>"
    

class ChatThread(db.Model):
    """A matter-owned conversation/container; document content has its own identity."""
    __tablename__ = "chat_threads"
    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    date = db.Column(db.DateTime(timezone=True), default=func.now())
    docCategory = db.Column(db.Integer, nullable=False)
    display_name = location = db.Column(db.String(250), nullable=False)
    # location = db.Column(db.String(250), unique=True, nullable=False)
    location = db.Column(db.String(250))
    projectId = db.Column(db.Integer, db.ForeignKey('projects.id'))
    next_turn_position = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    textEntries = db.relationship('TextEntry', backref='parent', lazy=True, cascade="all, delete, delete-orphan")
    draft_document = db.relationship('DraftDocument', uselist=False, cascade='all, delete-orphan')
    workspace_draft_state = synonym('draft_document')
    conversation_turns = db.relationship('ConversationTurn', cascade='all, delete-orphan')
    workflows = db.relationship('CaseAssistantWorkflow', foreign_keys='CaseAssistantWorkflow.chat_thread_id', cascade='all, delete-orphan')
    uploaded_files = db.relationship('UploadedFiles', backref='draft', lazy=True,
                                     foreign_keys='UploadedFiles.chat_thread_id',
                                     cascade="all, delete, delete-orphan")
    folder_uploads = db.relationship('FolderUpload', backref='draft', lazy=True,
                                     foreign_keys='FolderUpload.chat_thread_id',
                                     cascade="all, delete, delete-orphan")
    document_upload_groups = db.relationship('UploadGroup', foreign_keys='UploadGroup.chat_thread_id', cascade='all, delete')

    @classmethod
    def pdf_filename(cls, public_id):
        """Return a display-name-based PDF filename for the given draft public_id."""
        draft = cls.query.filter_by(public_id=public_id).first()
        if draft and draft.display_name:
            safe = re.sub(r'[^A-Za-z0-9]+', '_', draft.display_name).strip('_')
            return f"LexText_{safe}_{public_id}.pdf"
        return f"{public_id}.pdf"

    @classmethod
    def resolve_id(cls, chat_id_or_public_id):
        """Resolve chat identity; legacy draftId inputs also always mean a chat."""
        try:
            return int(chat_id_or_public_id)
        except (ValueError, TypeError):
            chat = cls.query.filter_by(public_id=chat_id_or_public_id).first()
            if chat:
                return chat.id
            raise ValueError(f"Chat not found: {chat_id_or_public_id}")

    
class TextEntry(db.Model):
    __tablename__="textEntries"
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime(timezone=True), default=func.now())
    chat_thread_id = db.Column(db.Integer, db.ForeignKey('chat_threads.id'))
    draftId = synonym('chat_thread_id')
    textName = db.Column(db.String(250))  # e.g. plaintiff. filename
    content = db.Column(
        Text().with_variant(LONGTEXT(), 'mysql'),
        nullable=True
    )


class DraftDocument(db.Model):
    """One independently identified current document, owned by its creating chat."""
    __tablename__ = 'draft_documents'
    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    chat_thread_id = db.Column(db.Integer, db.ForeignKey('chat_threads.id', ondelete='CASCADE'), unique=True, nullable=False)
    draft_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
    draft_before_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    draft_version = db.Column(db.Integer, nullable=False, default=1, server_default='1')
    # Preserve the original state entries when auxiliary state later changes.
    legacy_snapshot_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    migration_report_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    updated_at = db.Column(db.DateTime(), nullable=False, default=func.now())


# Legacy guided/Word code uses this name for the same chat/container entity.
Drafts = ChatThread


class ConversationTurn(db.Model):
    """One durable user/assistant exchange and its metadata in workspace order."""
    __tablename__ = 'conversation_turns'
    __table_args__ = (
        db.UniqueConstraint('chat_thread_id', 'position', name='uq_conversation_turn_position'),
        db.UniqueConstraint('workflow_run_id', name='uq_conversation_turn_workflow'),
    )
    id = db.Column(db.String(80), primary_key=True)
    chat_thread_id = db.Column(db.Integer, db.ForeignKey('chat_threads.id', ondelete='CASCADE'), nullable=False, index=True)
    draft_id = synonym('chat_thread_id')
    workflow_run_id = db.Column(db.String(80), db.ForeignKey('case_assistant_workflows.id', ondelete='SET NULL'), nullable=True)
    position = db.Column(db.Integer, nullable=False)
    user_text = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
    assistant_text = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
    metadata_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False, default='{}')
    created_at = db.Column(db.DateTime(), nullable=False, default=func.now())


class StepAnalytics(db.Model):
    __tablename__="stepAnalytics"
    __table_args__ = (
        db.Index(
            "ix_step_analytics_draft_doc_step",
            "chat_thread_id",
            "docType",
            "step",
            "id",
        ),
    )
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime(timezone=True), default=func.now())
    userId = db.Column(db.Integer)
    chat_thread_id = db.Column(db.Integer)  # Analytics survive chat deletion.
    draftId = synonym('chat_thread_id')
    docType = db.Column(db.String(100)) # Complaint, Answer, etc
    step = db.Column(db.Integer)  # step 0, 1, 2, etc.
    status = db.Column(
        db.String(20),
        nullable=False,
        default="unknown",
        server_default="unknown",
    )
    completedAt = db.Column(db.DateTime(timezone=True), nullable=True)
    celeryTaskId = db.Column(db.String(100), nullable=True, index=True)
    llmCallCount = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    llmSuccessCount = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    llmFailureCount = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    totalTokens = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")


class StepLlmModelUsage(db.Model):
    """Per-model LLM usage for one tracked step execution."""

    __tablename__ = "stepLlmModelUsage"
    __table_args__ = (
        db.UniqueConstraint(
            "stepAnalyticsId",
            "model",
            name="uq_step_llm_model_usage_step_model",
        ),
        db.Index("ix_step_llm_model_usage_step_id", "stepAnalyticsId"),
        db.Index("ix_step_llm_model_usage_model", "model"),
    )

    id = db.Column(db.Integer, primary_key=True)
    stepAnalyticsId = db.Column(
        db.Integer,
        db.ForeignKey("stepAnalytics.id", ondelete="CASCADE"),
        nullable=False,
    )
    model = db.Column(db.String(200), nullable=False)
    llmCallCount = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    llmSuccessCount = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    llmFailureCount = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    totalTokens = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")


class TaskAnalytics(db.Model):
    """Aggregate Case Assistant analytics for one saved task."""

    __tablename__ = "taskAnalytics"
    __table_args__ = (
        db.UniqueConstraint("chat_thread_id", name="uq_task_analytics_draft_id"),
        db.Index("ix_task_analytics_user_id", "userId"),
        db.Index("ix_task_analytics_updated_at", "updatedAt"),
    )

    id = db.Column(db.Integer, primary_key=True)
    createdAt = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    updatedAt = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
        server_default=func.now(),
    )
    userId = db.Column(db.Integer, nullable=False)
    # Deliberately not a foreign key: analytics survive deletion of the task.
    chat_thread_id = db.Column(db.Integer, nullable=False)
    draftId = synonym('chat_thread_id')
    # Current title of the substantive right-panel document. NULL means that
    # the Case Assistant task does not yet have a substantive workspace draft.
    draftTitle = db.Column(db.Text, nullable=True)

    promptsSent = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    answersReturned = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    llmCallCount = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    totalTokens = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")

    hallucinationGuardAttempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    hallucinationGuardSuccesses = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    hallucinationGuardFailures = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    hallucinationGuardLlmCallCount = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    hallucinationGuardTokens = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")

    downloadAttempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    downloadSuccesses = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    downloadFailures = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    downloadLlmCallCount = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    downloadTokens = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")


class FreelawClusterCache(db.Model):
    __tablename__ = "freelaw_cluster_cache"
    __table_args__ = (
        db.Index("ix_freelaw_cluster_last_accessed", "last_accessed_at"),
        db.Index("ix_freelaw_cluster_hit_count", "hit_count"),
    )

    cluster_id = db.Column(db.Integer, primary_key=True)
    case_name = db.Column(db.String(1000), nullable=True)
    opinion_id = db.Column(db.Integer, nullable=True)
    opinion_url = db.Column(db.String(1000), nullable=True)
    docket_url = db.Column(db.String(1000), nullable=True)
    court = db.Column(db.String(250), nullable=True)
    freelaw_url = db.Column(db.String(1000), nullable=True)
    llm_holding = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    payload_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    hit_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    created_at = db.Column(db.DateTime(timezone=True), default=func.now(), server_default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), default=func.now(), onupdate=func.now(), server_default=func.now())
    last_accessed_at = db.Column(db.DateTime(timezone=True), default=func.now(), server_default=func.now())


class FolderUpload(db.Model):
    __tablename__ = "folder_uploads"
    id = db.Column(db.Integer, primary_key=True)
    folder_name = db.Column(db.String(500), nullable=False)
    chat_thread_id = db.Column(db.Integer, db.ForeignKey('chat_threads.id'), nullable=True)
    draft_id = synonym('chat_thread_id')
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)
    file_count = db.Column(db.Integer, default=0)
    total_chunks = db.Column(db.Integer, default=0)
    upload_time = db.Column(db.DateTime(timezone=True), default=func.now())
    files = db.relationship('UploadedFiles', backref='folder_upload', lazy=True, cascade="all, delete")


class UploadedFiles(db.Model):
    __tablename__ = "uploaded_files"
    id = db.Column(db.Integer, primary_key=True)
    unique_id = db.Column(db.String(100), unique=True, nullable=False)  # uploaded_file_name_unique_id
    original_filename = db.Column(db.String(500), nullable=False)
    file_size = db.Column(db.Integer, nullable=False)
    page_count = db.Column(db.Integer, nullable=True)
    content = db.Column(
        Text().with_variant(LONGTEXT(), 'mysql'),
        nullable=False
    )  # parsed text content
    metadata_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    upload_time = db.Column(db.DateTime(timezone=True), default=func.now())
    chat_thread_id = db.Column(db.Integer, db.ForeignKey('chat_threads.id'), nullable=True)
    draft_id = synonym('chat_thread_id')
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)
    folder_upload_id = db.Column(db.Integer, db.ForeignKey('folder_uploads.id'), nullable=True)
    relative_path = db.Column(db.String(1000), nullable=True)
    source = db.Column(db.String(20), nullable=True)  # 'everything' | 'search' | NULL (legacy = 'everything')
    chunks = db.relationship('DocumentChunk', backref='uploaded_file', lazy=True, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<UploadedFile id={self.id}, unique_id={self.unique_id}, filename={self.original_filename}, draft_id={self.draft_id}>"


class DocumentChunk(db.Model):
    __tablename__ = "document_chunks"
    __table_args__ = (
        db.Index('ix_chunk_file_index', 'uploaded_file_id', 'chunk_index'),
        db.Index('ix_chunk_draft', 'chat_thread_id'),
        db.Index('ix_chunk_project', 'project_id'),
        db.Index('ft_chunk_content', 'content', mysql_prefix='FULLTEXT'),
    )
    id = db.Column(db.Integer, primary_key=True)
    uploaded_file_id = db.Column(db.Integer, db.ForeignKey('uploaded_files.id'), nullable=False)
    chat_thread_id = db.Column(db.Integer, db.ForeignKey('chat_threads.id'), nullable=True)
    draft_id = synonym('chat_thread_id')
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)
    chunk_index = db.Column(db.Integer, nullable=False)
    content = db.Column(
        Text().with_variant(LONGTEXT(), 'mysql'),
        nullable=False
    )
    token_count = db.Column(db.Integer, nullable=False)
    char_count = db.Column(db.Integer, nullable=False)
    source_filename = db.Column(db.String(500), nullable=False)
    source_relative_path = db.Column(db.String(1000), nullable=True)
    source_unique_id = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())


class UploadGroup(db.Model):
    """A sealed selection; its members may arrive in several HTTP requests."""
    __tablename__ = 'document_upload_groups'
    __table_args__ = (db.UniqueConstraint('owner_id', 'request_key', name='uq_upload_group_request'),)
    id = db.Column(db.String(36), primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False)
    chat_thread_id = db.Column(db.Integer, db.ForeignKey('chat_threads.id', ondelete='CASCADE'), nullable=True)
    draft_id = synonym('chat_thread_id')
    scope_key = db.Column(db.String(64), nullable=False, index=True)
    request_key = db.Column(db.String(100), nullable=False)
    manifest_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    documents = db.relationship('DocumentPreparation', backref='group', cascade='all, delete-orphan')


class DocumentPreparation(db.Model):
    """Durable ingestion state. Workers never need to update a chat's JSON."""
    __tablename__ = 'document_preparations'
    __table_args__ = (
        db.UniqueConstraint('scope_key', 'client_upload_id', name='uq_preparation_client'),
        db.Index('ix_preparation_dispatch', 'dispatch_pending', 'lease_until'),
    )
    id = db.Column(db.String(100), primary_key=True)
    group_id = db.Column(db.String(36), db.ForeignKey('document_upload_groups.id', ondelete='CASCADE'), nullable=False)
    scope_key = db.Column(db.String(64), nullable=False, index=True)
    client_upload_id = db.Column(db.String(100), nullable=False)
    filename = db.Column(db.String(500), nullable=False)
    relative_path = db.Column(db.String(1000), nullable=True)
    folder_upload_id = db.Column(db.Integer, db.ForeignKey('folder_uploads.id', ondelete='SET NULL'), nullable=True)
    file_size = db.Column(db.BigInteger, nullable=False)
    uploaded_file_id = db.Column(db.Integer, db.ForeignKey('uploaded_files.id', ondelete='SET NULL'), unique=True, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='queued')
    checkpoint = db.Column(db.String(20), nullable=False, default='queued')
    generation = db.Column(db.Integer, nullable=False, default=1)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    source_key = db.Column(db.String(1000), nullable=True)
    source_hash = db.Column(db.String(64), nullable=True)
    lease_token = db.Column(db.String(36), nullable=True)
    lease_until = db.Column(db.DateTime, nullable=True)
    task_id = db.Column(db.String(100), nullable=True)
    dispatch_pending = db.Column(db.Boolean, nullable=False, default=False)
    dispatched_at = db.Column(db.DateTime, nullable=True)
    error_code = db.Column(db.String(80), nullable=True)
    error_message = db.Column(db.String(1000), nullable=True)
    warning = db.Column(db.String(1000), nullable=True)
    parsed_at = db.Column(db.DateTime, nullable=True)
    indexed_at = db.Column(db.DateTime, nullable=True)
    ready_at = db.Column(db.DateTime, nullable=True)
    first_attached_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False)
    coverage_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    coverage_summary_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    parse_manifest_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    artifact_id = db.Column(db.String(64), nullable=True)
    summary_segments = db.relationship('DocumentSummarySegment', cascade='all, delete-orphan')
    artifacts = db.relationship('DocumentPreparedArtifact', cascade='all, delete-orphan')


class DocumentPreparedArtifact(db.Model):
    """Immutable published preparation, referenced by accepted chat turns."""
    __tablename__ = 'document_prepared_artifacts'
    id = db.Column(db.String(64), primary_key=True)
    document_id = db.Column(db.String(100), db.ForeignKey('document_preparations.id', ondelete='CASCADE'), nullable=False, index=True)
    preparation_version = db.Column(db.String(80), nullable=False)
    source_hash = db.Column(db.String(64), nullable=False)
    result_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
    card_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False, default='{}')
    manifest_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False, default='{}')
    coverage_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)


class DocumentSummarySegment(db.Model):
    """Saved document analysis, shared by preparation and prompt extraction.

    document_id + segment_key identifies reusable output. Preparation writes
    section summaries here; Case Assistant also writes validated query quotes
    and compact answer evidence, distinguished by kind and versioned keys.
    CaseAssistantWork.result_id points here so task status and saved content
    have separate responsibilities. A new task attempt can reuse the content.
    """
    __tablename__ = 'document_summary_segments'
    __table_args__ = (db.UniqueConstraint('document_id', 'segment_key', name='uq_summary_segment'),
                     db.Index('ix_prepared_section_search', 'search_text', mysql_prefix='FULLTEXT'))
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.String(100), db.ForeignKey('document_preparations.id', ondelete='CASCADE'), nullable=False)
    segment_key = db.Column(db.String(64), nullable=False)
    result_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
    kind = db.Column(db.String(20), nullable=False, default='legacy', server_default='legacy')
    state = db.Column(db.String(20), nullable=False, default='ready', server_default='ready')
    generation = db.Column(db.Integer, nullable=False, default=1, server_default='1')
    attempts = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    lease_token = db.Column(db.String(36), nullable=True)
    lease_until = db.Column(db.DateTime, nullable=True)
    search_text = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)


class CaseAssistantWorkflow(db.Model):
    """SQL record of one submitted prompt, followed by the frontend across tasks.

    Shared by both execution versions. data_json holds the accepted request/
    document snapshot and saved conversation context; result_json holds the
    final response, pending question/confirmation, or failure.
    Version 0 uses intent_json for classification and payload_json for fixed
    planning/evidence. Version 1 uses payload_json.agent_context for initial
    input/state and agent_sources for supporting-document hints; AgentStep and
    AgentArtifact store its subsequent decisions and operation results.
    These are SQL records, not Redis results.
    Celery task IDs can change while this row's id remains the same.
    """
    __tablename__ = 'case_assistant_workflows'
    id = db.Column(db.String(80), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    chat_thread_id = db.Column(db.Integer, db.ForeignKey('chat_threads.id', ondelete='CASCADE'), nullable=False, index=True)
    draft_id = synonym('chat_thread_id')
    # This is set to chat_thread_id while active, then NULL on completion/failure/
    # cancellation. UNIQUE permits many historical NULLs, but one active prompt
    # per chat. This is separate from the temporary worker lease below.
    active_chat_thread_id = db.Column(db.Integer, unique=True, nullable=True)
    active_draft_id = synonym('active_chat_thread_id')
    draft_document_id = db.Column(db.Integer, db.ForeignKey('draft_documents.id', ondelete='SET NULL'), nullable=True)
    base_draft_version = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(32), nullable=False, default='running')
    phase = db.Column(db.String(20), nullable=False, default='classify')
    # Default 0 preserves already submitted workflows after migration.
    execution_version = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    data_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
    intent_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    payload_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    result_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    # Expected Celery message IDs let SQL reject older deliveries. Version 1
    # reuses planning_task_id for each advance task (not just initial planning).
    # Version 0 uses group_id for the chord and answer_task_id for its callback,
    # then for the answer when after_extraction calls dispatch_answer.
    planning_task_id = db.Column(db.String(100), nullable=True)
    answer_task_id = db.Column(db.String(100), nullable=True)
    group_id = db.Column(db.String(100), nullable=True)
    # Version 1: delivery remains pending until a worker claims the saved ID;
    # Beat may resend it. Version 0 clears this after sending and requires Retry.
    dispatch_pending = db.Column(db.Boolean, nullable=False, default=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    # Planning/callback/answer ownership expires at lease_until. Parallel source
    # tasks have their own CaseAssistantWork leases, rather than sharing this one.
    lease_token = db.Column(db.String(36), nullable=True)
    lease_until = db.Column(db.DateTime, nullable=True)
    # Compare before publishing to avoid overwriting a user's intervening edit.
    draft_hash = db.Column(db.String(64), nullable=False)
    evidence_budget = db.Column(db.Integer, nullable=False, default=64000)
    reduction_round = db.Column(db.Integer, nullable=False, default=0)
    last_progress_at = db.Column(db.DateTime, nullable=False)
    error_message = db.Column(db.String(1000), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False, index=True)
    # Whole-prompt deadline, independent of each Celery task's execution timeout.
    deadline = db.Column(db.DateTime, nullable=False)
    work = db.relationship('CaseAssistantWork', cascade='all, delete-orphan')
    agent_steps = db.relationship('AgentStep', cascade='all, delete-orphan')
    agent_artifacts = db.relationship('AgentArtifact', cascade='all, delete-orphan')


class AgentStep(db.Model):
    """Durable model decisions and sequential tool executions for one request."""
    __tablename__ = 'case_assistant_agent_steps'
    __table_args__ = (db.UniqueConstraint('workflow_run_id', 'sequence', name='uq_agent_step_sequence'),)
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_run_id = db.Column(db.String(80), db.ForeignKey('case_assistant_workflows.id', ondelete='CASCADE'), nullable=False, index=True)
    sequence = db.Column(db.Integer, nullable=False)
    kind = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    call_id = db.Column(db.String(200), nullable=True)
    arguments_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False, default='{}')
    result_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    summary_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    state = db.Column(db.String(20), nullable=False, default='queued')
    created_at = db.Column(db.DateTime, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False)
    artifacts = db.relationship('AgentArtifact', cascade='all, delete-orphan')


class AgentArtifact(db.Model):
    """Immutable candidate drafts/analysis; never editor state until publication."""
    __tablename__ = 'case_assistant_agent_artifacts'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_run_id = db.Column(db.String(80), db.ForeignKey('case_assistant_workflows.id', ondelete='CASCADE'), nullable=False, index=True)
    step_id = db.Column(db.String(36), db.ForeignKey('case_assistant_agent_steps.id', ondelete='CASCADE'), nullable=False)
    kind = db.Column(db.String(20), nullable=False)
    content_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)


class CaseAssistantWork(db.Model):
    """Legacy version-0 extraction/review/reduction; version 1 uses AgentStep.

    workflow_run_id is the durable link to CaseAssistantWorkflow, not a Celery
    parent-task relationship. material_json stores a source range for kind=query
    or saved evidence inputs for kind=reduce. kind=retrieval_review stores
    references to excerpts/child reviews or authorized candidate document IDs.
    Review output guides another search; it never replaces source evidence.
    Generated results use result_id to reference DocumentSummarySegment. Direct
    original-source ranges are ready with result_id=NULL: material_json already
    contains everything needed to read the source. A split parent is
    replaced by smaller required rows; it is not counted as completed evidence.
    """
    __tablename__ = 'case_assistant_work'
    __table_args__ = (db.UniqueConstraint('workflow_run_id', 'work_key', name='uq_case_assistant_work'),)
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_run_id = db.Column(db.String(80), db.ForeignKey('case_assistant_workflows.id', ondelete='CASCADE'), nullable=False, index=True)
    # Stable material identity within the prompt; task_id changes on explicit Retry.
    work_key = db.Column(db.String(64), nullable=False)
    kind = db.Column(db.String(20), nullable=False, default='query')
    document_id = db.Column(db.String(100), nullable=True)
    artifact_id = db.Column(db.String(64), nullable=True)
    material_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
    result_id = db.Column(db.Integer, db.ForeignKey('document_summary_segments.id', ondelete='SET NULL'), nullable=True)
    state = db.Column(db.String(20), nullable=False, default='queued')
    task_id = db.Column(db.String(100), nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    # A fresh token identifies each claim; an expired/replaced attempt cannot
    # publish merely because its provider call eventually returned successfully.
    lease_token = db.Column(db.String(36), nullable=True)
    lease_until = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.String(1000), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False)


class SmartSearchSession(db.Model):
    __tablename__ = "smart_search_sessions"
    id = db.Column(db.String(36), primary_key=True)  # UUID string = session_id
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)
    title = db.Column(db.String(500), nullable=False, default='')
    conversation_brief = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    constraints_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), default=func.now(), onupdate=func.now())
    messages = db.relationship('SmartSearchMessage', backref='session', lazy=True,
                               cascade="all, delete, delete-orphan",
                               order_by='SmartSearchMessage.created_at')


class SmartSearchMessage(db.Model):
    __tablename__ = "smart_search_messages"
    __table_args__ = (
        db.Index('ix_ss_msg_session_time', 'session_id', 'created_at'),
    )
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(db.String(36), db.ForeignKey('smart_search_sessions.id'), nullable=False)
    role = db.Column(db.String(10), nullable=False)  # 'user' or 'assistant'
    content = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
    metadata_json = db.Column(Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())


class FileTag(db.Model):
    __tablename__ = "file_tags"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    tag_name = db.Column(db.String(100), nullable=False)
    tag_color = db.Column(db.String(20), nullable=False, default='#6366f1')
    associations = db.relationship('FileTagAssociation', backref='tag', lazy=True, cascade="all, delete")


class FileTagAssociation(db.Model):
    __tablename__ = "file_tag_associations"
    id = db.Column(db.Integer, primary_key=True)
    uploaded_file_id = db.Column(db.Integer, db.ForeignKey('uploaded_files.id'), nullable=False)
    tag_id = db.Column(db.Integer, db.ForeignKey('file_tags.id'), nullable=False)


class FolderTagAssociation(db.Model):
    __tablename__ = "folder_tag_associations"
    id = db.Column(db.Integer, primary_key=True)
    folder_upload_id = db.Column(db.Integer, db.ForeignKey('folder_uploads.id'), nullable=False)
    tag_id = db.Column(db.Integer, db.ForeignKey('file_tags.id'), nullable=False)
