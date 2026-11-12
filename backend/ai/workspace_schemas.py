"""Structured Responses results used by Workspace's LLM steps.

These models describe provider output only.  The existing dictionaries stored in
session state and returned to the frontend remain unchanged.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _StructuredResult(BaseModel):
    model_config = ConfigDict(extra="forbid")


DraftAction = Literal[
    "answer_only",
    "create_new_draft",
    "edit_current_draft",
    "revise_uploaded_draft",
    "verify_draft",
]
RevisionMode = Literal["general_revision", "preserve_original"]


class IntentConfirmationOption(_StructuredResult):
    label: str
    description: str
    draft_action: DraftAction
    revision_mode: RevisionMode
    primary_draft_file_id: str | None
    selected_file_ids: list[str]


class IntentSelectedFile(_StructuredResult):
    file_id: str
    role: Literal["primary_draft", "supporting_context", "citation_source", "background_context"]
    method: Literal["verbatim", "relevant"]


class IntentDecisionResult(_StructuredResult):
    draft_action: DraftAction
    needs_confirmation: bool
    confirmation_reason: str | None
    confirmation_options: list[IntentConfirmationOption]
    revision_mode: RevisionMode
    selected_files: list[IntentSelectedFile]
    context_policy: Literal["none", "reuse", "refresh"]
    retrieval_queries: list[str] = Field(default_factory=list)
    coverage_mode: Literal['focused', 'whole_document', 'corpus_wide'] = 'focused'
    selection_confidence: Literal['low', 'medium', 'high'] = 'medium'
    requires_complete_results: bool = False


class RelevantFileExtractionResult(_StructuredResult):
    file_name: str
    file_id: str
    file_scope: str
    relevant_excerpts: str
    source_anchors: list[str]


class BriefCaseConstraints(_StructuredResult):
    case_label: str
    jurisdiction: str | None
    court: str | None
    case_type: str | None
    party_names: list[str]
    key_exhibits: list[str]
    deadlines: list[str]


class BriefConstraints(_StructuredResult):
    tone: str | None
    exclusions: list[str]
    special_instructions: str | None
    cases: list[BriefCaseConstraints]


class BriefUpdateResult(_StructuredResult):
    brief: str
    constraints: BriefConstraints


class PreserveOriginalTargetResult(_StructuredResult):
    edit_scope: Literal[
        "targeted_blocks",
        "section",
        "global_find_replace",
        "whole_document_batch",
        "needs_confirmation",
    ]
    anchors: list[str]
    confidence: Literal["high", "medium", "low"]
    reason: str
    needs_confirmation: bool


class PreserveOriginalCitationChange(_StructuredResult):
    original: str
    action: Literal["corrected", "removed", "kept"]
    replacement: str
    reason: str


class PreserveOriginalBlockOperation(_StructuredResult):
    op: Literal["edit_preserved_block"]
    anchor: str
    replacement_text: str
    change_summary: str
    citation_changes: list[PreserveOriginalCitationChange]


class PreserveOriginalEditResult(_StructuredResult):
    operations: list[PreserveOriginalBlockOperation]
    response: str


class DraftSectionResult(_StructuredResult):
    section_id: str
    section_name: str
    section_content: str


class DraftResult(_StructuredResult):
    title: str
    sections: list[DraftSectionResult]


class DraftCreateResult(_StructuredResult):
    response: str
    draft: DraftResult


class EditBlockOperation(_StructuredResult):
    op: Literal["edit_block"]
    section_id: str
    block_id: str
    text: str


class AddBlockOperation(_StructuredResult):
    op: Literal["add_block"]
    section_id: str
    after_block_id: str
    block_type: Literal["heading", "paragraph"]
    text: str


class DeleteBlockOperation(_StructuredResult):
    op: Literal["delete_block"]
    section_id: str
    block_id: str


class MoveBlockOperation(_StructuredResult):
    op: Literal["move_block"]
    section_id: str
    block_id: str
    target_section_id: str
    after_block_id: str


class EditSectionOperation(_StructuredResult):
    op: Literal["edit_section"]
    section_id: str
    section_name: str
    section_content: str


class AddSectionOperation(_StructuredResult):
    op: Literal["add_section"]
    section_id: str
    section_name: str
    section_content: str
    after_section_id: str


class DeleteSectionOperation(_StructuredResult):
    op: Literal["delete_section"]
    section_id: str


class ReorderSectionsOperation(_StructuredResult):
    op: Literal["reorder_sections"]
    new_sections_order: list[str]


class ReoutlineDraftOperation(_StructuredResult):
    op: Literal["re-outline_draft"]
    title: str
    sections: list[DraftSectionResult]


class NoChangeOperation(_StructuredResult):
    op: Literal["no_change"]


DraftEditOperation = (
    EditBlockOperation
    | AddBlockOperation
    | DeleteBlockOperation
    | MoveBlockOperation
    | EditSectionOperation
    | AddSectionOperation
    | DeleteSectionOperation
    | ReorderSectionsOperation
    | ReoutlineDraftOperation
    | NoChangeOperation
)


class DraftEditResult(_StructuredResult):
    operations: list[DraftEditOperation]
    response: str
