"""add right-panel draft title to Case Assistant task analytics

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-18
"""

import json

from alembic import op
import sqlalchemy as sa


revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def _analytics_draft_title(raw_state):
    """Derive the analytics title from persisted Case Assistant state."""
    if not raw_state:
        return None
    try:
        state = json.loads(raw_state)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    draft = state.get("draft") if isinstance(state, dict) else None
    if not isinstance(draft, dict):
        return None

    title = str(draft.get("title") or "").strip()
    sections = draft.get("sections", [])
    has_substantive_section = isinstance(sections, list) and any(
        str(section.get("section_name") or "").strip()
        or str(section.get("section_content") or "").strip()
        for section in sections
        if isinstance(section, dict)
    )
    if not title and not has_substantive_section:
        return None
    return title or "Untitled document"


def upgrade():
    op.add_column(
        "taskAnalytics",
        sa.Column("draftTitle", sa.Text(), nullable=True),
    )

    connection = op.get_bind()
    task_analytics = sa.table(
        "taskAnalytics",
        sa.column("draftId", sa.Integer()),
        sa.column("draftTitle", sa.Text()),
    )
    text_entries = sa.table(
        "textEntries",
        sa.column("id", sa.Integer()),
        sa.column("draftId", sa.Integer()),
        sa.column("textName", sa.String()),
        sa.column("content", sa.Text()),
    )

    # DBMemory historically reads the first matching state row. Select the
    # lowest id explicitly so legacy duplicates are handled deterministically.
    canonical_states = (
        sa.select(
            text_entries.c.draftId.label("draft_id"),
            sa.func.min(text_entries.c.id).label("state_entry_id"),
        )
        .where(text_entries.c.textName == "state")
        .group_by(text_entries.c.draftId)
        .subquery()
    )
    rows = connection.execute(
        sa.select(task_analytics.c.draftId, text_entries.c.content)
        .select_from(
            task_analytics.outerjoin(
                canonical_states,
                canonical_states.c.draft_id == task_analytics.c.draftId,
            ).outerjoin(
                text_entries,
                text_entries.c.id == canonical_states.c.state_entry_id,
            )
        )
    ).all()

    for draft_id, raw_state in rows:
        draft_title = _analytics_draft_title(raw_state)
        if draft_title is None:
            continue
        connection.execute(
            sa.update(task_analytics)
            .where(task_analytics.c.draftId == draft_id)
            .values(draftTitle=draft_title)
        )


def downgrade():
    op.drop_column("taskAnalytics", "draftTitle")
