from pathlib import Path

import backend.util.free_law_tools as fl_tools


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_normalize_text_for_citation_lookup_reflows_multiline_citations():
    raw = (
        "In re Iovate Health Scis. Int'l Inc., 2025 Bankr.\n"
        "LEXIS 2284, at *24.\n"
        "See also 2024\nWL 123456."
    )

    normalized = fl_tools.normalize_text_for_citation_lookup(raw)

    assert "\n" not in normalized
    assert "2025 Bankr. LEXIS 2284" in normalized
    assert "2024 WL 123456" in normalized


def test_normalize_text_for_citation_lookup_repairs_hyphen_line_wrap():
    raw = "Inter-\nnational Business Machines Corp."
    normalized = fl_tools.normalize_text_for_citation_lookup(raw)
    assert "International Business Machines Corp." in normalized


def test_filter_raw_citations_lookup_safe_uses_lookup_only_normalization(monkeypatch):
    captured = {}

    def fake_filter_raw_citations(lookup_text):
        captured["lookup_text"] = lookup_text
        return [{"type": "case", "displayName": "Example v. Case\t123 F.3d 456"}]

    monkeypatch.setattr(fl_tools, "filter_raw_citations", fake_filter_raw_citations)

    raw_text = "In re Iovate, 2025 Bankr.\nLEXIS 2284"
    result = fl_tools.filter_raw_citations_lookup_safe(raw_text)

    assert result and result[0]["type"] == "case"
    assert raw_text == "In re Iovate, 2025 Bankr.\nLEXIS 2284"
    assert "\n" not in captured["lookup_text"]
    assert "2025 Bankr. LEXIS 2284" in captured["lookup_text"]


def test_shared_hg_uses_lookup_text_but_keeps_file_text_for_prompt():
    path = _repo_root() / "backend/hallucination_guard.py"
    src = path.read_text()

    assert "verify_citations_with_lookup_tags_lookup_safe(file_text)" in src
    assert "draft_text=file_text" in src


def test_everything_hg_delegates_to_shared_standalone_engine():
    src = (_repo_root() / "backend/everything.py").read_text()
    prompt = (_repo_root() / "backend/ai/prompts/hg/HALLUCINATION_GUARD_CASES").read_text()

    assert "_hg.run_hallucination_guard_on_text(" in src
    assert "draft_text = _hg._format_draft_for_guard(draft)" in src
    assert "identify EVERY distinct drafted full-form citation variant" in prompt
