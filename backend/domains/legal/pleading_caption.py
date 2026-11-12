"""FRCP Rule 10 caption page for Workspace court export."""

from docx import Document

from backend.domains.legal.pleading_format import get_structural_spec
from backend.util.word_sanitize import sanitize_word_text

CAPTION_TEMPLATE_NO_LINES = (
    "backend/util/Federal-Pleading-Template-Google-Doc-Compatible-new-no-verticalines.docx"
)
CAPTION_TEMPLATE_VERTICAL_LINES = (
    "backend/util/Federal-Pleading-Template-Google-Doc-Compatible-new.docx"
)

# Backward-compatible alias for tests and callers expecting a single default path.
EVERYTHING_CAPTION_TEMPLATE = CAPTION_TEMPLATE_NO_LINES


def resolve_caption_template_path(export_spec: dict) -> str:
    """Pick caption Word template based on export structural flags."""
    structural = get_structural_spec(export_spec)
    if structural.get("use_vertical_lines"):
        return CAPTION_TEMPLATE_VERTICAL_LINES
    return CAPTION_TEMPLATE_NO_LINES


def build_caption_fields(
    structural: dict,
    *,
    draft_title: str = "",
    user_contact: str | None = None,
) -> dict:
    """Map structural.caption to create_cover_page arguments."""
    cap = (structural or {}).get("caption") or {}
    court_line_1 = (cap.get("court_line_1") or "UNITED STATES DISTRICT COURT").strip()
    court_line_2 = (cap.get("court_line_2") or "").strip()
    if not court_line_2:
        court_line_2 = "___________"

    venue = f"{court_line_1}\n{court_line_2}"
    plaintiff = cap.get("plaintiff") or "(Plaintiff's Name)"
    defendant = cap.get("defendant") or "(Defendant's Name)"
    pleading_title = (cap.get("pleading_title") or "").strip()
    if pleading_title in ("", "TITLE OF PLEADING"):
        pleading_title = (draft_title or "").strip() or "TITLE OF PLEADING"
    attorney = user_contact or cap.get("attorney_contact") or "(your name & contact info)"

    return {
        "attorneys_contact_info": sanitize_word_text(attorney),
        "venue": sanitize_word_text(venue),
        "plaintiff": sanitize_word_text(plaintiff),
        "defendant": sanitize_word_text(defendant),
        "title": sanitize_word_text(pleading_title),
        "case_number": sanitize_word_text(cap.get("case_number") or "___________"),
    }


def create_document_with_caption(
    export_spec: dict,
    *,
    draft_title: str = "",
    user_contact: str | None = None,
    is_defendant: bool = False,
) -> Document:
    """Start export from federal pleading template with Rule 10 caption page."""
    from backend.domains.legal.caption_template import create_cover_page

    structural = get_structural_spec(export_spec)
    fields = build_caption_fields(structural, draft_title=draft_title, user_contact=user_contact)
    template_path = resolve_caption_template_path(export_spec)

    doc = Document(template_path)
    doc = create_cover_page(
        doc,
        fields["attorneys_contact_info"],
        fields["venue"],
        fields["plaintiff"],
        fields["defendant"],
        title=fields["title"],
        is_defendant=is_defendant,
        case_number=fields["case_number"],
        use_new_template=True,
    )
    return doc
