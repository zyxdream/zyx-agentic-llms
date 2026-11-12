import logging
import pytest
import json
import re
from pathlib import Path
from types import SimpleNamespace

pytest.importorskip("flask_sqlalchemy")

from backend.standalone_hg import (
    _attach_citation_links_to_results,
    _attach_quote_verification,
    _attach_statute_quote_review,
    _build_citation_link_map,
    _enrich_validated_cases_for_standalone,
    _ensure_case_result_coverage,
    _ensure_docket_order_coverage,
    _ensure_statute_result_coverage,
    _extract_docket_order_citations,
    _extract_guard_results,
    _guard_case_status,
    _guard_compact_location,
    _guard_results_to_html,
    _guard_statute_status,
    _guard_statutes_to_html,
    _normalize_for_match,
    _quote_in_opinions,
    _quote_segments,
    _standalone_hg_progress,
)


def _caplog_messages(caplog) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_standalone_cases_prompt_enforces_full_form_wl_lexis_coverage():
    prompt = (_repo_root() / "backend/ai/prompts/hg/HALLUCINATION_GUARD_CASES").read_text()

    assert "distinct drafted full-form citation variants" in prompt
    assert "Westlaw citations" in prompt
    assert "LEXIS citations" in prompt
    assert "2025 Bankr. LEXIS 2284" in prompt
    assert "Prefer the first full/identifying mention" in prompt
    assert "same drafted full-form citation variant" in prompt
    assert 'reason="Not found in our database"' in prompt
    assert "output record count MUST equal the number of distinct drafted full-form citation variants" in prompt
    assert "grammatically supports the quoted language" in prompt
    assert "immediately followed by a case citation" in prompt
    assert 'leave the quote out of "quotes"' in prompt
    assert "Do not treat ordinary explanatory parentheticals as quotes" in prompt
    assert "For mixed parentheticals, return only the text inside the quotation marks" in prompt
    assert "Puckett v. United States, 556 U.S. 129, 135 (2009)" in prompt
    assert '-> quotes: ["affect the framework within which the trial proceeds"]' in prompt
    assert "Do NOT merge different full-form citation variants" in prompt
    assert "same case entity does not cure a conflicting drafted citation" in prompt
    assert "Bryant, 597 F.3d at 694" in prompt
    assert "97 F.3d 678" in prompt


def test_everything_adapter_uses_shared_standalone_prompt_rules():
    everything_src = (_repo_root() / "backend/everything.py").read_text()
    prompt = (_repo_root() / "backend/ai/prompts/hg/HALLUCINATION_GUARD_CASES").read_text()

    assert "_hg.run_hallucination_guard_on_text(" in everything_src
    assert "identify EVERY distinct drafted full-form citation variant" in prompt
    assert "Westlaw citations" in prompt
    assert "LEXIS citations" in prompt
    assert "Do NOT merge different full-form citation variants" in prompt
    assert 'reason="Not found in our database"' in prompt
    assert "output record count MUST equal the number of distinct drafted full-form citation variants" in prompt


def test_extract_guard_results():
    payload = {"results": [{"citation": "123 F.3d 456"}]}
    assert _extract_guard_results(payload) == [{"citation": "123 F.3d 456"}]
    assert _extract_guard_results('{"results":[{"citation":"123 F.3d 456"}]}') == [{"citation": "123 F.3d 456"}]
    assert _extract_guard_results("not-json") == []


def test_quote_matching_treats_standalone_bracket_edits_as_flexible_gaps():
    opinions = [("Opinion", _normalize_for_match(
        "The appellant does not tell us what the district court did wrong."
    ))]

    assert _quote_segments('[did] not tell us what the district court did wrong') == [
        "not tell us what the district court did wrong"
    ]
    assert _quote_in_opinions(
        "[did] not tell us what the district court did wrong",
        opinions,
    ) == {"status": "found", "opinion_type": "Opinion"}


def test_quote_matching_allows_standalone_bracket_subject_substitution():
    opinions = [("Opinion", _normalize_for_match(
        "Adverse rulings alone do not demonstrate judicial bias."
    ))]

    assert _quote_segments("[they] alone do not demonstrate judicial bias") == [
        "alone do not demonstrate judicial bias"
    ]
    assert _quote_in_opinions(
        "[they] alone do not demonstrate judicial bias",
        opinions,
    ) == {"status": "found", "opinion_type": "Opinion"}


def test_quote_matching_preserves_in_word_bracket_edits():
    opinions = [("Opinion", _normalize_for_match(
        "Ownership interests were governed by the contract."
    ))]

    assert _quote_segments("[O]wnership interests were govern[ed] by the contract") == [
        "ownership interests were governed by the contract"
    ]
    assert _quote_in_opinions(
        "[O]wnership interests were govern[ed] by the contract",
        opinions,
    ) == {"status": "found", "opinion_type": "Opinion"}


def test_quote_matching_still_rejects_wrong_non_bracket_language():
    opinions = [("Opinion", _normalize_for_match(
        "The appellant does not tell us what the district court did wrong."
    ))]

    assert _quote_in_opinions(
        "[did] tell us what the district court got exactly right",
        opinions,
    ) == {"status": "not_found", "opinion_type": None}


def test_quote_matching_allows_punctuation_soft_anchor_match_after_ellipses():
    opinions = [("Opinion", _normalize_for_match(
        "The Federal Rules reject the approach that pleading is a game of skill "
        "in which one misstep by counsel may be decisive to the outcome and accept "
        "the principle that the purpose of pleading is to facilitate. a proper "
        "decision on the merits."
    ))]

    assert _quote_segments(
        "The Federal Rules reject the approach that pleading is a game of skill . . . "
        "and accept the principle that the purpose of pleading is to facilitate a proper…"
    ) == [
        "the federal rules reject the approach that pleading is a game of skill",
        "and accept the principle that the purpose of pleading is to facilitate a proper",
    ]
    assert _quote_in_opinions(
        "The Federal Rules reject the approach that pleading is a game of skill . . . "
        "and accept the principle that the purpose of pleading is to facilitate a proper…",
        opinions,
    ) == {"status": "found", "opinion_type": "Opinion"}


def test_quote_matching_combines_bracket_gaps_with_punctuation_soft_anchors():
    opinions = [("Opinion", _normalize_for_match(
        "The original complaint in this cause and the amended complaint were not "
        "based upon different causes. of action."
    ))]

    assert _quote_in_opinions(
        "The original complaint in this [case] and the amended complaint were not "
        "based upon different causes of action",
        opinions,
    ) == {"status": "found", "opinion_type": "Opinion"}


def test_quote_matching_punctuation_soft_still_rejects_spelling_differences():
    opinions = [("Opinion", _normalize_for_match(
        "The original complaint in this cause and the amended complaint were not "
        "based upon different canses of action."
    ))]

    assert _quote_in_opinions(
        "The original complaint in this [case] and the amended complaint were not "
        "based upon different causes of action",
        opinions,
    ) == {"status": "not_found", "opinion_type": None}


def test_quote_check_logs_quote_opinion_anchors_and_exact_match(caplog):
    caplog.set_level(logging.INFO, logger="backend.hallucination_guard")
    opinions = [{
        "index": 3,
        "id": 987,
        "url": "https://www.courtlistener.com/api/rest/v4/opinions/987/",
        "type": "Opinion",
        "normalized_text": _normalize_for_match("The court affirmed the judgment."),
    }]

    assert _quote_in_opinions(
        "court affirmed the judgment",
        opinions,
        citation="123 F.3d 456",
        cluster_id=42,
    ) == {"status": "found", "opinion_type": "Opinion"}

    logs = _caplog_messages(caplog)
    assert "standalone_hg quote-check checking" in logs
    assert "citation='123 F.3d 456'" in logs
    assert "cluster_id=42" in logs
    assert "opinion_index=3" in logs
    assert "opinion_id=987" in logs
    assert "opinion_url='https://www.courtlistener.com/api/rest/v4/opinions/987/'" in logs
    assert "anchors=['court affirmed the judgment']" in logs
    assert "quote_preview='court affirmed the judgment'" in logs
    assert "opinion_length=" in logs
    assert "opinion_preview='the court affirmed the judgment.'" in logs
    assert "standalone_hg quote-check found method=exact" in logs


def test_quote_check_logs_punctuation_soft_match_method(caplog):
    caplog.set_level(logging.INFO, logger="backend.hallucination_guard")
    opinions = [{
        "id": 35541,
        "url": "https://www.courtlistener.com/api/rest/v4/opinions/35541/",
        "type": "Opinion",
        "normalized_text": _normalize_for_match(
            "The purpose of pleading is to facilitate. a proper decision on the merits."
        ),
    }]

    assert _quote_in_opinions(
        "the purpose of pleading is to facilitate a proper",
        opinions,
        citation="355 U.S. 41",
        cluster_id=105573,
    ) == {"status": "found", "opinion_type": "Opinion"}

    logs = _caplog_messages(caplog)
    assert "standalone_hg quote-check found method=punctuation_soft" in logs
    assert "exact_reason=missing_exact_anchor" in logs


def test_quote_check_logs_failed_anchor_reason_for_spelling_mismatch(caplog):
    caplog.set_level(logging.INFO, logger="backend.hallucination_guard")
    opinions = [{
        "type": "Opinion",
        "normalized_text": _normalize_for_match(
            "The original complaint in this cause and the amended complaint were not "
            "based upon different canses of action."
        ),
    }]

    assert _quote_in_opinions(
        "The original complaint in this [case] and the amended complaint were not "
        "based upon different causes of action",
        opinions,
        citation="303 U.S. 197",
        cluster_id=102964,
    ) == {"status": "not_found", "opinion_type": None}

    logs = _caplog_messages(caplog)
    assert "standalone_hg quote-check not_found" in logs
    assert "reason=missing_punctuation_soft_word_sequence" in logs
    assert "method=punctuation_soft" in logs
    assert "failed_anchor_index=2" in logs
    assert "failed_anchor_words=['and', 'the', 'amended', 'complaint'" in logs
    assert "exact_reason=missing_exact_anchor" in logs


def test_quote_check_logs_skipped_when_no_normalized_opinions(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="backend.hallucination_guard")
    monkeypatch.setattr("backend.standalone_hg.get_cluster", lambda _cluster_id: {"sub_opinions": []})
    monkeypatch.setattr("backend.standalone_hg.get_cluster_opinions", lambda _cluster: [])

    results = [{
        "citation": "355 U.S. 41",
        "clusterId": 105573,
        "quotes": ["quoted language"],
    }]

    _attach_quote_verification(results, {})

    assert results[0]["quotes"] == [{
        "text": "quoted language",
        "status": "not_checked",
        "opinion_type": None,
    }]
    logs = _caplog_messages(caplog)
    assert "standalone_hg quote-check skipped reason=no_opinions" in logs
    assert "citation='355 U.S. 41'" in logs
    assert "cluster_id=105573" in logs


def test_quote_matching_punctuation_soft_requires_order_within_same_opinion():
    opinions = [
        ("Majority", _normalize_for_match(
            "First required anchor appears in the majority opinion only."
        )),
        ("Dissent", _normalize_for_match(
            "Second required anchor appears in the dissenting opinion only."
        )),
        ("Concurrence", _normalize_for_match(
            "Second required anchor appears before first required anchor."
        )),
    ]

    assert _quote_in_opinions(
        "First required anchor ... Second required anchor",
        opinions,
    ) == {"status": "not_found", "opinion_type": None}


def test_quote_matching_does_not_verify_only_bracketed_or_tiny_anchors():
    opinions = [("Opinion", _normalize_for_match("The court affirmed the judgment."))]

    assert _quote_segments("[they]") == []
    assert _quote_in_opinions("[they]", opinions) == {"status": "not_found", "opinion_type": None}
    assert _quote_segments("[they] won") == []
    assert _quote_in_opinions("[they] won", opinions) == {"status": "not_found", "opinion_type": None}


class RecordingTask:
    def __init__(self):
        self.updates = []

    def update_state(self, **kwargs):
        self.updates.append(kwargs)


def test_standalone_hg_progress_hides_debug_details_outside_dev(monkeypatch):
    import backend.constants as constants

    monkeypatch.setattr(constants, "ENVIRONMENT", "production")
    task = RecordingTask()

    _standalone_hg_progress(
        task,
        "Checking quotations and cited propositions...",
        progress=80,
        dev_message="Analyzing within context... Case prompt: 12 tokens; statute prompt: 7 tokens.",
        token_counts=[12, 7],
    )

    meta = task.updates[0]["meta"]
    assert meta["message"] == "Checking quotations and cited propositions..."
    assert meta["status"] == "Checking quotations and cited propositions..."
    assert meta["progress"] == 80
    assert meta["show_dev_progress_details"] is False
    assert "dev_progress_details" not in meta
    assert "token_counts" not in meta
    assert "token_limit" not in meta


def test_standalone_hg_progress_includes_debug_details_for_dev_monica(monkeypatch):
    import backend.constants as constants

    monkeypatch.setattr(constants, "ENVIRONMENT", "dev-monica")
    task = RecordingTask()

    _standalone_hg_progress(
        task,
        "Finishing document preparation...",
        progress=70,
        dev_message="Saving extracted text for Hallucination Guard...",
        filename="Brief.pdf",
        file_size=1234,
    )

    meta = task.updates[0]["meta"]
    assert meta["message"] == "Finishing document preparation..."
    assert meta["show_dev_progress_details"] is True
    assert meta["dev_progress_details"] == {
        "message": "Saving extracted text for Hallucination Guard...",
        "filename": "Brief.pdf",
        "file_size": 1234,
    }


def test_citation_link_map_links_cases_and_statutes():
    filtered_citations = [
        {
            "prefix": "/CaseViewer",
            "form": "550 U.S. 544",
            "citationName": "550-U_dot_S_dot_-544",
            "clusterId": 112233,
            "displayName": "Bell Atlantic Corp. v. Twombly\t550 U.S. 544",
            "type": "case",
            "lookup_tag": "VERIFIED_FL",
        },
        {
            "prefix": "/CaseViewer",
            "form": "2010 WI 78",
            "citationName": "2010-WI-78",
            "clusterId": 8238267,
            "displayName": "Blum ex rel. Studinski v. 1st Auto & Casualty Insurance\t2010 WI 78",
            "type": "case",
            "lookup_tag": "CASE_NAME_MISMATCH",
        },
        {
            "prefix": "/CaseViewer",
            "form": "647 B.R. 440",
            "citationName": "647-B_dot_R_dot_-440",
            "clusterId": None,
            "displayName": "647 B.R. 440",
            "type": "case",
            "lookup_tag": "LACKS_OPINION_DATA",
        },
        {
            "prefix": "https://www.law.cornell.edu/uscode/text",
            "form": "42 U.S.C. § 1983",
            "citationName": "42/1983",
            "displayName": "42 U.S.C. § 1983",
            "type": "us",
        },
    ]

    link_map = _build_citation_link_map(filtered_citations)
    results = [
        {"citation": "550 U.S. 544, 555"},
        {"citation": "2010 WI 78", "citation_as_drafted": "Wrong Case, 2010 WI 78"},
        {"citation": "647 B.R. 440", "citation_as_drafted": "Three Arrows, 647 B.R. 440"},
        {"citation": "42 U.S.C. § 1983"},
    ]

    _attach_citation_links_to_results(results, link_map)

    assert results[0]["href"] == "/CaseViewer/550-U_dot_S_dot_-544&112233"
    assert results[1]["href"] == "/CaseViewer/2010-WI-78&8238267"
    assert "href" not in results[2]
    assert results[3]["href"] == "https://www.law.cornell.edu/uscode/text/42/1983"


def test_case_coverage_reconciliation_surfaces_parser_omission():
    results = [{"citation": "123 F.3d 456", "is_valid": True, "is_applicable": True}]
    diagnostics = [
        {
            "citation": "123 F.3d 456",
            "citation_as_drafted": "123 F.3d 456",
            "start_index": 10,
            "clusterId": 1,
        },
        {
            "citation": "789 F.3d 101",
            "citation_as_drafted": "789 F.3d 101",
            "drafted_case_name": "Doe v. Roe",
            "start_index": 40,
            "clusterId": 2,
        },
    ]

    out = _ensure_case_result_coverage(results, diagnostics)

    assert out is results
    assert len(results) == 2
    recovered = results[1]
    assert recovered["citation"] == "789 F.3d 101"
    assert recovered["citation_as_drafted"] == "Doe v. Roe, 789 F.3d 101"
    assert recovered["is_valid"] is True
    assert recovered["is_applicable"] is None
    assert recovered["coverage_recovered"] is True
    assert _guard_case_status(recovered) == "Needs Review"

    html = _guard_results_to_html(results)
    assert "Coverage safeguard - 1 citation(s)" in html
    assert "hg-review-row--needs-review" in html
    assert '<strong class="hg-summary-count hg-summary-count--review">1</strong> need review' in html


def test_case_coverage_reconciliation_skips_existing_and_broad_diagnostics():
    results = [{"citation": "123 F.3d 456, 460", "is_valid": True, "is_applicable": True}]
    diagnostics = [
        {
            "citation": "123 F.3d 456",
            "citation_as_drafted": "Smith v. Jones, 123 F.3d 456",
            "start_index": 10,
            "clusterId": 1,
        },
        {
            "citation": "",
            "citation_as_drafted": "",
            "start_index": 20,
        },
        {
            "citation": "Full draft text with 999 F.3d 1",
            "citation_as_drafted": "Full draft text with 999 F.3d 1",
            "start_index": None,
        },
    ]

    _ensure_case_result_coverage(results, diagnostics)

    assert len(results) == 1
    assert "coverage_recovered" not in results[0]


def test_named_docket_order_scanner_recovers_pdf_wrapped_authorities_only():
    draft = """
    See In re Three Arrows Capital, Ltd., No. 22-\n10920 (MG)
    (Bankr. S.D.N.Y. Jul. 12, 2022). See also Matter of BOS Solutions Ltd.,
    Case No. 20-32465, [Dkt. No. 17] (Bankr. S.D. Tex. May 5, 2020).
    The motion in this matter is at [D.I. 9].
    """

    detected = _extract_docket_order_citations(draft)

    assert [row["case_name"] for row in detected] == [
        "In re Three Arrows Capital, Ltd.",
        "Matter of BOS Solutions Ltd.",
    ]
    assert detected[0]["citation"] == "No. 22-10920 (MG)"
    assert detected[1]["citation"] == "Case No. 20-32465 [Dkt. No. 17]"
    assert all(row["citation_kind"] == "docket_order" for row in detected)
    assert all(row["is_valid"] is None for row in detected)
    assert not any(row["citation"] == "D.I. 9" for row in detected)


def test_named_docket_order_coverage_is_never_green():
    results = []
    _ensure_docket_order_coverage(
        results,
        "In re Example Corp., No. 24-12345 (ABC) [D.I. 17] "
        "(Bankr. S.D.N.Y. Jan. 2, 2025) (granting relief).",
    )

    assert len(results) == 1
    assert results[0]["coverage_recovered"] is True
    assert results[0]["citation_kind"] == "docket_order"
    assert results[0]["is_valid"] is None
    assert results[0]["is_applicable"] is None
    assert _guard_case_status(results[0]) == "Needs Review"

    html = _guard_results_to_html(results)
    assert "Manual check" in html
    assert "Needs review" in html
    assert "Found</span>" not in html
    assert "hg-status--confirmed" not in html


def test_named_docket_order_coverage_marks_existing_row_without_duplicate():
    results = [{
        "citation": "No. 24-12345 (ABC)",
        "citation_as_drafted": "In re Example Corp., No. 24-12345 (ABC)",
        "case_name": "In re Example Corp.",
        "is_valid": True,
        "is_applicable": True,
    }]

    _ensure_docket_order_coverage(
        results,
        "In re Example Corp., No. 24-12345 (ABC) [D.I. 17] "
        "(Bankr. S.D.N.Y. Jan. 2, 2025).",
    )

    assert len(results) == 1
    assert results[0]["citation_kind"] == "docket_order"
    assert results[0]["is_valid"] is None
    assert results[0]["is_applicable"] is None
    assert "coverage_recovered" not in results[0]


def test_statute_coverage_reconciliation_surfaces_parser_omission_as_review():
    results = [{"citation": "42 U.S.C. § 1983", "is_valid": True, "is_applicable": True}]
    filtered_citations = [
        {"type": "case", "form": "123 F.3d 456", "displayName": "Smith v. Jones\t123 F.3d 456"},
        {"type": "us", "form": "42 U.S.C. § 1983", "displayName": "42 U.S.C. § 1983"},
        {"type": "state", "form": "Cal. Penal Code § 187", "displayName": "Cal. Penal Code § 187"},
    ]

    out = _ensure_statute_result_coverage(results, filtered_citations)

    assert out is results
    assert len(results) == 2
    recovered = results[1]
    assert recovered["citation"] == "Cal. Penal Code § 187"
    assert recovered["is_valid"] is None
    assert recovered["is_applicable"] is None
    assert recovered["coverage_recovered"] is True
    assert _guard_statute_status(recovered) == "Needs Review"

    html = _guard_statutes_to_html(results)
    assert "Coverage safeguard - 1 statute(s)" in html
    assert '<strong class="hg-summary-count hg-summary-count--confirmed">1</strong> confirmed' in html
    assert '<strong class="hg-summary-count hg-summary-count--review">1</strong> need review' in html
    assert "hg-summary-count--issue" not in html
    assert "hg-review-row--needs-review" in html
    assert "Needs review" in html


def test_statute_coverage_reconciliation_skips_existing_base_matches():
    results = [{"citation": "11 U.S.C. § 1519(b)", "is_valid": True, "is_applicable": True}]
    filtered_citations = [
        {"type": "us", "form": "11 U.S.C. § 1519", "displayName": "11 U.S.C. § 1519"},
    ]

    _ensure_statute_result_coverage(results, filtered_citations)

    assert len(results) == 1
    assert "coverage_recovered" not in results[0]
    assert _guard_statute_status({"is_valid": False, "is_applicable": False}) == "Issue"
    assert _guard_statute_status({"is_valid": None, "is_applicable": None}) == "Needs Review"


def test_statute_quote_review_marks_llm_quotes_source_text_required():
    results = [{
        "citation": "11 U.S.C. § 1519(b)",
        "citation_as_drafted": "section 1519(b)",
        "location": "Section 3",
        "href": "https://www.law.cornell.edu/uscode/text/11/1519",
        "quotes": ["on an ex parte basis"],
        "is_valid": True,
        "is_applicable": True,
    }]

    _attach_statute_quote_review(
        results,
        'As section 1519(b) supposedly permits relief “on an ex parte basis”, relief is requested.',
    )

    assert results[0]["quotes"] == [{
        "text": "on an ex parte basis",
        "status": "not_checked",
        "opinion_type": None,
    }]
    assert results[0]["quote_check"] == {"total": 1, "found": 0}
    assert _guard_statute_status(results[0]) == "Needs Review"

    html = _guard_statutes_to_html(results)
    assert '<a href="https://www.law.cornell.edu/uscode/text/11/1519"' in html
    assert "Quotation review" in html
    assert "source-text check required" in html
    assert "on an ex parte basis" in html
    assert '<strong class="hg-summary-count hg-summary-count--review">1</strong> need review' in html
    assert "hg-summary-count--issue" not in html
    assert "hg-status--confirmed" not in html


def test_statute_quote_review_extracts_nearby_quotes_when_llm_omits_them():
    results = [{
        "citation": "11 U.S.C. § 362(a)(1)",
        "citation_as_drafted": "11 U.S.C. § 362(a)(1)",
        "location": "Section 2",
        "is_valid": True,
        "is_applicable": True,
    }]

    _attach_statute_quote_review(
        results,
        'The stay under 11 U.S.C. § 362(a)(1) bars “the commencement or continuation” of actions.',
    )

    assert results[0]["quotes"] == [{
        "text": "the commencement or continuation",
        "status": "not_checked",
        "opinion_type": None,
    }]
    assert results[0]["quote_check"] == {"total": 1, "found": 0}
    assert _guard_statute_status(results[0]) == "Needs Review"


def test_statute_quote_review_dedupes_llm_and_nearby_quotes():
    results = [{
        "citation": "11 U.S.C. § 362(a)",
        "citation_as_drafted": "11 U.S.C. § 362(a)",
        "location": "Section 2",
        "quotes": [
            "any act to obtain possession of property of the estate",
            {"text": "any act to obtain possession of property of the estate"},
        ],
        "is_valid": True,
        "is_applicable": True,
    }]

    _attach_statute_quote_review(
        results,
        'Section 11 U.S.C. § 362(a) prohibits “any act to obtain possession of property of the estate”.',
    )

    assert results[0]["quotes"] == [{
        "text": "any act to obtain possession of property of the estate",
        "status": "not_checked",
        "opinion_type": None,
    }]
    assert results[0]["quote_check"] == {"total": 1, "found": 0}


def test_guard_statutes_to_html_links_citation(monkeypatch):
    import backend.constants as constants
    from backend.everything import _guard_statutes_to_html as everything_statutes_to_html

    monkeypatch.setattr(constants, "ENVIRONMENT", "dev-monica")

    results = [
        {
            "citation": "42 U.S.C. § 1983",
            "href": "https://www.law.cornell.edu/uscode/text/42/1983",
            "is_valid": True,
            "is_applicable": True,
        }
    ]
    html = _guard_statutes_to_html(results)

    assert html == everything_statutes_to_html(results)
    assert '<a href="https://www.law.cornell.edu/uscode/text/42/1983"' in html
    assert "42 U.S.C. § 1983" in html
    assert "Quotation review" not in html
    assert "source-text check required" not in html
    assert "Possible Issues" in html
    assert 'class="hg-tooltip-cell"' in html
    assert "<th" in html and 'class="hg-tooltip-cell"' in html
    assert not re.search(r"<td\b[^>]*\bhg-tooltip-cell\b", html)
    assert html.count('data-tooltip="The authority Hallucination Guard matched for review."') == 1
    assert html.count('data-tooltip="Whether the cited statute appears to be a real provision."') == 1
    assert html.count('data-tooltip="Developer diagnostic details about lookup or citation matching."') == 1
    assert 'title="The authority Hallucination Guard matched for review."' not in html


def test_guard_statutes_to_html_hides_possible_issues_outside_monica(monkeypatch):
    import backend.constants as constants

    monkeypatch.setattr(constants, "ENVIRONMENT", "production")

    html = _guard_statutes_to_html([{
        "citation": "99 U.S.C. § 9999",
        "citation_as_drafted": "§ 9999",
        "location": "Section 5",
        "is_valid": False,
        "is_applicable": False,
        "reason": "Title 99 does not exist in the United States Code.",
    }])

    assert "Possible Issues" not in html
    assert "Title 99 does not exist in the United States Code." not in html


def test_shared_case_table_renders_lookup_possible_issue(monkeypatch):
    import backend.constants as constants
    from backend.everything import _guard_results_to_html

    monkeypatch.setattr(constants, "ENVIRONMENT", "dev-monica")

    html = _guard_results_to_html([{
        "case_name": "Three Arrows",
        "citation": "647 B.R. 440",
        "citation_as_drafted": "Three Arrows, 647 B.R. 440",
        "is_valid": False,
        "is_applicable": None,
        "lookup_tag": "LACKS_OPINION_DATA",
        "possible_issue": "We could not confirm this citation from CourtListener. It may be a database gap or an incorrect citation; check Westlaw or Lexis.",
    }])

    assert "Possible Issues" in html
    assert "LACKS_OPINION_DATA" in html
    assert "could not confirm this citation from CourtListener" in html
    assert 'class="hg-tooltip-cell"' in html
    assert "<th" in html and 'class="hg-tooltip-cell"' in html
    assert not re.search(r"<td\b[^>]*\bhg-tooltip-cell\b", html)
    assert html.count('data-tooltip="Developer diagnostic details about lookup or citation matching."') == 1


def test_shared_case_table_hides_possible_issues_outside_monica(monkeypatch):
    import backend.constants as constants
    from backend.everything import _guard_results_to_html

    monkeypatch.setattr(constants, "ENVIRONMENT", "production")

    html = _guard_results_to_html([{
        "case_name": "Three Arrows",
        "citation": "647 B.R. 440",
        "citation_as_drafted": "Three Arrows, 647 B.R. 440",
        "is_valid": False,
        "is_applicable": None,
        "lookup_tag": "LACKS_OPINION_DATA",
        "possible_issue": "We could not confirm this citation from CourtListener. It may be a database gap or an incorrect citation; check Westlaw or Lexis.",
    }])

    assert "Possible Issues" not in html
    assert "LACKS_OPINION_DATA" not in html
    assert "could not confirm this citation from CourtListener" not in html


def test_shared_case_table_renders_lookup_note_and_inapplicability_reason(monkeypatch):
    import backend.constants as constants
    from backend.everything import _guard_issue_text, _guard_results_to_html

    monkeypatch.setattr(constants, "ENVIRONMENT", "dev-monica")

    possible_issue = (
        "The reporter citation resolves to a case with a different name than the one drafted."
    )
    reason = "The case addresses bankruptcy stay relief, not preliminary injunction standards."
    result = {
        "case_name": "In re Soundview Elite, Ltd.",
        "citation": "503 B.R. 571",
        "citation_as_drafted": "In re Soundview Elite Ltd. , 503 B.R. 571, 588-90 (Bankr. S.D.N.Y. 2014)",
        "is_valid": True,
        "is_applicable": False,
        "lookup_tag": "CASE_NAME_MISMATCH",
        "possible_issue": possible_issue,
        "reason": reason,
    }
    html = _guard_results_to_html([result])

    assert "CASE_NAME_MISMATCH" in html
    assert possible_issue in html
    assert "Does not support:" in html
    assert reason in html
    assert html.count(reason) == 1
    assert _guard_issue_text(result) == f"CASE_NAME_MISMATCH: {possible_issue}"


def test_shared_case_table_shows_inapplicability_reason_outside_monica(monkeypatch):
    import backend.constants as constants
    from backend.everything import _guard_results_to_html

    monkeypatch.setattr(constants, "ENVIRONMENT", "production")

    reason = "The holding addresses jurisdiction, not the cited damages proposition."
    html = _guard_results_to_html([{
        "case_name": "Doe v. Roe",
        "citation": "123 F.2d 456",
        "citation_as_drafted": "Doe v. Roe, 123 F.2d 456",
        "is_valid": True,
        "is_applicable": False,
        "reason": reason,
    }])

    assert "Possible Issues" not in html
    assert "Does not support:" in html
    assert reason in html


def test_shared_case_table_neutral_when_applicability_unassessed():
    from backend.everything import _guard_results_to_html

    # A valid case listed without a proposition: is_applicable=null -> Needs Review,
    # NOT framed as "Not applicable" because it is not a confirmed applicability failure.
    html = _guard_results_to_html([{
        "case_name": "Blum v. 1st Auto & Cas. Ins. Co.",
        "citation": "2010 WI 78",
        "citation_as_drafted": "Blum v. 1st Auto & Cas.Ins. Co. 2010 WI 78",
        "is_valid": True,
        "is_applicable": None,
        "reason": "Listed without an accompanying proposition; applicability not assessed.",
    }])

    assert "<strong>1</strong> citation checked" in html
    assert '<strong class="hg-summary-count hg-summary-count--review">1</strong> need review' in html
    assert "Needs review" in html
    assert '<span class="hg-status hg-status--issue">Issue found</span>' not in html
    assert "Does not support:" not in html               # neutral, not an applicability failure
    assert "—" in html                                   # Applicable column shows the neutral dash


def test_shared_case_table_counts_real_inapplicability_as_issue():
    from backend.everything import _guard_results_to_html

    html = _guard_results_to_html([{
        "case_name": "Doe v. Roe",
        "citation": "123 F.2d 456",
        "is_valid": True,
        "is_applicable": False,
        "reason": "The holding addresses jurisdiction, not the cited damages proposition.",
    }])
    assert '<strong class="hg-summary-count hg-summary-count--issue">1</strong> issue found' in html


def test_guard_case_status_verified_requires_applicable_true():
    assert _guard_case_status({
        "citation": "123 F.2d 456",
        "is_valid": True,
        "is_applicable": True,
        "lookup_tag": "VERIFIED_FL",
        "possible_issue": "",
    }) == "Verified"

    assert _guard_case_status({
        "citation": "123 F.2d 456",
        "is_valid": True,
        "is_applicable": None,
        "lookup_tag": "VERIFIED_FL",
        "possible_issue": "",
    }) == "Needs Review"


def test_guard_case_status_issue_for_confirmed_failures():
    assert _guard_case_status({
        "citation": "123 F.2d 456",
        "is_valid": True,
        "is_applicable": False,
        "lookup_tag": "VERIFIED_FL",
    }) == "Issue"

    assert _guard_case_status({
        "citation": "123 F.2d 456",
        "is_valid": True,
        "is_applicable": True,
        "lookup_tag": "CITE_MISMATCH",
    }) == "Issue"

    assert _guard_case_status({
        "citation": "123 F.2d 456",
        "is_valid": True,
        "is_applicable": True,
        "quotes": [{"text": "wrong quote", "status": "not_found"}],
        "quote_check": {"total": 1, "found": 0},
    }) == "Issue"

    assert _guard_case_status({
        "citation": "999 F.9th 999",
        "is_valid": False,
        "is_applicable": False,
        "lookup_tag": "",
    }) == "Issue"


def test_guard_case_status_needs_review_for_uncertain_failures():
    assert _guard_case_status({
        "citation": "647 B.R. 440",
        "is_valid": False,
        "is_applicable": None,
        "lookup_tag": "LACKS_OPINION_DATA",
    }) == "Needs Review"

    assert _guard_case_status({
        "citation": "647 B.R. 440",
        "is_valid": False,
        "is_applicable": False,
        "lookup_tag": "AMBIGUOUS_CLUSTER",
    }) == "Needs Review"

    assert _guard_case_status({
        "citation": "647 B.R. 440",
        "is_valid": False,
        "is_applicable": False,
        "lookup_tag": "NO_CITATION_PARSED",
    }) == "Needs Review"

    assert _guard_case_status({
        "citation": "647 B.R. 440",
        "is_valid": False,
        "is_applicable": False,
        "lookup_tag": "API_ERROR_OR_PARTIAL",
    }) == "Needs Review"

    assert _guard_case_status({
        "citation": "123 F.2d 456",
        "is_valid": True,
        "is_applicable": True,
        "quotes": [{"text": "unavailable quote", "status": "not_checked"}],
        "quote_check": {"total": 1, "found": 0},
    }) == "Needs Review"

    assert _guard_case_status({
        "citation": "365 B.R. 401",
        "is_valid": False,
        "is_applicable": False,
        "did_you_mean": {
            "casename": "Nevada Power Co. v. Calpine Corp.",
            "reporter_citation": "365 B.R. 401",
            "url": "https://www.courtlistener.com/opinion/123/calpine/",
        },
    }) == "Needs Review"


def test_case_table_full_link_blank_note_when_name_matches():
    from backend.everything import _guard_results_to_html

    html = _guard_results_to_html([{
        "case_name": "Smith v. Jones",
        "citation": "123 F.2d 456",
        "citation_as_drafted": "Smith v. Jones, 123 F.2d 456",
        "is_valid": True,
        "is_applicable": True,
        "lookup_tag": "VERIFIED_FL",
        "possible_issue": "",
        "href": "/CaseViewer/123-F_dot_2d-456&99",
    }])
    # Whole "name citation" inside a single hyperlink; Possible Issues blank (no tag/note).
    assert ">Smith v. Jones 123 F.2d 456</a>" in html
    assert "VERIFIED_FL" not in html
    assert "<strong>1</strong> citation checked" in html
    assert '<strong class="hg-summary-count hg-summary-count--confirmed">1</strong> confirmed' in html


def test_case_table_partial_link_and_note_on_name_mismatch(monkeypatch):
    import backend.constants as constants
    from backend.everything import _guard_results_to_html

    monkeypatch.setattr(constants, "ENVIRONMENT", "dev-monica")

    html = _guard_results_to_html([{
        "case_name": "Meyer v. U.S. Fire Ins. Co.",
        "citation": "218 Wis.2d 499",
        "citation_as_drafted": "Meyer v. U.S. Fire Ins. Co. 218 Wis.2d 499",
        "is_valid": True,
        "is_applicable": None,
        "lookup_tag": "CASE_NAME_MISMATCH",
        "possible_issue": "The reporter citation resolves to a case with a different name than the one drafted.",
        "href": "/CaseViewer/218-Wis_dot_2d-499&2023658",
    }])
    # Only the citation is linked; the drafted case name stays plain text outside the <a>.
    assert ">218 Wis.2d 499</a>" in html
    assert "Meyer v. U.S. Fire Ins. Co. <a href=" in html
    # The mismatch note is shown for a partial link.
    assert "CASE_NAME_MISMATCH" in html
    assert "different name than the one drafted" in html
    assert "Different case" in html


def test_case_table_renders_mismatch_authority_labels():
    from backend.everything import _guard_results_to_html

    html = _guard_results_to_html([
        {
            "case_name": "Wrong Name v. Party",
            "citation": "123 F.2d 456",
            "citation_as_drafted": "Wrong Name v. Party, 123 F.2d 456",
            "is_valid": True,
            "is_applicable": True,
            "lookup_tag": "CASE_NAME_MISMATCH",
            "possible_issue": "The reporter citation resolves to a case with a different name.",
        },
        {
            "case_name": "Smith v. Jones",
            "citation": "999 F.9th 999",
            "citation_as_drafted": "Smith v. Jones, 999 F.9th 999",
            "is_valid": False,
            "is_applicable": False,
            "lookup_tag": "CITE_MISMATCH",
            "possible_issue": "The drafted citation resolves to a different authority.",
        },
    ])

    assert '<strong class="hg-summary-count hg-summary-count--issue">2</strong> issues found' in html
    assert "hg-review-row--issue" in html
    assert "Different case" in html
    assert "Different authority" in html
    assert "Not found" not in html


def test_case_table_verified_authority_keeps_found_label():
    from backend.everything import _guard_results_to_html

    html = _guard_results_to_html([{
        "case_name": "Smith v. Jones",
        "citation": "123 F.2d 456",
        "citation_as_drafted": "Smith v. Jones, 123 F.2d 456",
        "is_valid": True,
        "is_applicable": True,
        "lookup_tag": "VERIFIED_FL",
        "possible_issue": "",
    }])

    assert "Found" in html
    assert "Different case" not in html
    assert "Different authority" not in html


def test_case_table_no_link_shows_note(monkeypatch):
    import backend.constants as constants
    from backend.everything import _guard_results_to_html

    monkeypatch.setattr(constants, "ENVIRONMENT", "dev-monica")

    html = _guard_results_to_html([{
        "case_name": "Three Arrows",
        "citation": "647 B.R. 440",
        "is_valid": False,
        "is_applicable": None,
        "lookup_tag": "LACKS_OPINION_DATA",
        "possible_issue": "We could not confirm this citation from CourtListener. It may be a database gap or an incorrect citation; check Westlaw or Lexis.",
    }])
    assert "<a href" not in html          # nothing is hyperlinked
    assert "LACKS_OPINION_DATA" in html   # note shown for an unlinked citation


def test_extract_drafted_case_name_isolated_by_lower_bound():
    from backend.util.free_law_tools import _extract_drafted_case_name

    # Lookup text has newlines collapsed, so a citation list is one line; the name window must not
    # bleed backward across the previous citation.
    text = ("Blum v. 1st Auto & Cas.Ins. Co. 2010 WI 78 "
            "Meyer v. U.S. Fire Ins. Co. 218 Wis.2d 499")
    meyer_start = text.index("218 Wis.2d 499")
    prev_end = text.index("2010 WI 78") + len("2010 WI 78")

    polluted = _extract_drafted_case_name(text, meyer_start)              # no bound -> bleeds
    assert "2010" in polluted
    clean = _extract_drafted_case_name(text, meyer_start, lower_bound=prev_end)
    assert "2010" not in clean and "Meyer" in clean and "Fire" in clean


def test_case_name_match_status_word_based_abbreviations():
    from backend.util.free_law_tools import _case_name_match_status

    def cl(name):
        return {"case_name": name}

    # Abbreviation / minor differences should MATCH (strong overlap on a party side).
    assert _case_name_match_status("Meyer v. U.S. Fire Ins. Co.",
                                   cl("Meyer v. United States Fire Insurance"))[0]
    assert _case_name_match_status("Plastics Engineering Co. v. Liberty Mutual Insurance Co.",
                                   cl("Plastics Engineering Co. v. Liberty Mutual Insurance"))[0]
    assert _case_name_match_status("Blum v. 1st Auto & Cas.Ins. Co.",
                                   cl("Blum ex rel. Studinski v. 1st Auto & Casualty Insurance"))[0]
    # Short single-party form contained in the resolved name -> match (flagged short).
    ok, is_short = _case_name_match_status("Blum", cl("Blum ex rel. Studinski v. 1st Auto & Casualty Insurance"))
    assert ok and is_short
    # Genuinely different parties -> mismatch.
    assert not _case_name_match_status("Smith v. Jones", cl("Doe v. Roe"))[0]


def test_reconcile_rescues_false_case_name_mismatch():
    from backend.everything import _guard_attach_lookup_diagnostics

    # Calpine: the lexical pass flagged a mismatch ("In re Calpine Corp." vs the full caption), but the
    # LLM semantically confirms it's the same case (name_mismatch=False) -> rescue to a full match.
    results = [{
        "citation": "365 B.R. 401",
        "citation_as_drafted": "In re Calpine Corp., 365 B.R. 401, 409 (S.D.N.Y. 2007)",
        "case_name": "In re Calpine Corp.",
        "name_mismatch": False,
        "is_valid": True,
    }]
    diagnostics = [{
        "citation": "365 B.R. 401",
        "lookup_tag": "CASE_NAME_MISMATCH",
        "possible_issue": "The reporter citation resolves to a case with a different name than the one drafted.",
        "clusterId": 12345,
        "case_name": "Nevada Power Co. v. Calpine Corp. (In re Calpine Corp.)",
    }]
    out = _guard_attach_lookup_diagnostics(results, diagnostics)[0]
    assert out["lookup_tag"] == "VERIFIED_FL"
    assert out["possible_issue"] == ""
    assert out["resolved_case_name"] == "Nevada Power Co. v. Calpine Corp. (In re Calpine Corp.)"
    assert out["resolved_citation"] == "365 B.R. 401"


def test_reconcile_downgrades_on_semantic_mismatch():
    from backend.everything import _guard_attach_lookup_diagnostics

    # The lexical pass passed (VERIFIED_FL) but the LLM says the cases differ -> authoritative downgrade.
    results = [{
        "citation": "123 F.2d 456",
        "case_name": "Smith v. Jones",
        "name_mismatch": True,
        "is_valid": True,
    }]
    diagnostics = [{
        "citation": "123 F.2d 456",
        "lookup_tag": "VERIFIED_FL",
        "possible_issue": "",
        "clusterId": 99,
        "case_name": "Doe v. Roe",
    }]
    out = _guard_attach_lookup_diagnostics(results, diagnostics)[0]
    assert out["lookup_tag"] == "CASE_NAME_MISMATCH"
    assert out["possible_issue"]  # a note is surfaced


def test_reconcile_keeps_deterministic_tag_when_flag_absent():
    from backend.everything import _guard_attach_lookup_diagnostics

    # No usable LLM flag -> the deterministic Free Law tag stands as the fallback.
    results = [{
        "citation": "218 Wis.2d 499",
        "case_name": "Meyer v. U.S. Fire Ins. Co.",
        "is_valid": True,
    }]
    diagnostics = [{
        "citation": "218 Wis.2d 499",
        "lookup_tag": "CASE_NAME_MISMATCH",
        "possible_issue": "drafted name may not match",
        "clusterId": 7,
        "case_name": "Meyer v. United States Fire Insurance",
    }]
    out = _guard_attach_lookup_diagnostics(results, diagnostics)[0]
    assert out["lookup_tag"] == "CASE_NAME_MISMATCH"
    assert out["possible_issue"] == "drafted name may not match"


def test_reconcile_noop_when_no_cluster_to_compare():
    from backend.everything import _guard_attach_lookup_diagnostics

    # An unresolved row (no clusterId) has no Free Law name to compare; the flag must be ignored.
    results = [{
        "citation": "999 F.3d 1",
        "case_name": "Ghost v. Phantom",
        "name_mismatch": True,
        "is_valid": False,
    }]
    diagnostics = [{
        "citation": "999 F.3d 1",
        "lookup_tag": "LACKS_OPINION_DATA",
        "possible_issue": "no opinion data",
        "clusterId": None,
        "case_name": "",
    }]
    out = _guard_attach_lookup_diagnostics(results, diagnostics)[0]
    assert out["lookup_tag"] == "LACKS_OPINION_DATA"


def test_case_table_uses_freelaw_resolved_name_and_citation():
    from backend.everything import _guard_results_to_html

    html = _guard_results_to_html([{
        "case_name": "In re Calpine Corp.",  # LLM/drafted form
        "citation": "365 B.R. 401",
        "citation_as_drafted": "In re Calpine Corp., 365 B.R. 401, 409 (S.D.N.Y. 2007)",
        "resolved_case_name": "Nevada Power Co. v. Calpine Corp. (In re Calpine Corp.)",
        "resolved_citation": "365 B.R. 401",
        "is_valid": True,
        "is_applicable": True,
        "lookup_tag": "VERIFIED_FL",
        "possible_issue": "",
        "href": "/CaseViewer/365-B_dot_R-401&1",
    }])
    # "Citation reviewed" shows the resolved name (not the LLM case_name), fully linked.
    assert ">Nevada Power Co. v. Calpine Corp. (In re Calpine Corp.) 365 B.R. 401</a>" in html
    # "Citation as drafted" shows the verbatim LLM extraction from the original document.
    assert "In re Calpine Corp., 365 B.R. 401, 409 (S.D.N.Y. 2007)" in html


def test_case_name_only_for_web_search_strips_untrusted_citations():
    from backend.standalone_hg import _case_name_only_for_web_search

    assert _case_name_only_for_web_search(
        "In re Calpine Corp., 365 B.R. 401, 409 (S.D.N.Y. 2007)"
    ) == "In re Calpine Corp."
    assert _case_name_only_for_web_search(
        "Blum v. 1st Auto & Cas.Ins. Co. 2010 WI 78"
    ) == "Blum v. 1st Auto & Cas.Ins. Co."
    assert _case_name_only_for_web_search(
        "In re Iovate Health Scis. Int'l Inc., No. 25-11958 (MG), 2025 Bankr. LEXIS 2284"
    ) == "In re Iovate Health Scis. Int'l Inc."
    assert _case_name_only_for_web_search(
        "Smith v. Jones, 2024 WL 123456"
    ) == "Smith v. Jones"
    assert _case_name_only_for_web_search(
        "Doe v. Roe, 2025 U.S. Dist. LEXIS 12345"
    ) == "Doe v. Roe"


def test_hg_web_search_inherits_shared_model_and_effort_defaults(monkeypatch):
    from backend.ai import base as ai_base
    import backend.standalone_hg as shg

    captured = {}

    def fake_web_search(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return json.dumps({
            "resolved": True,
            "resolved_casename": "Smith v. Jones",
            "resolved_reporter_citation": "123 F.3d 456",
            "url": "https://example.com/smith",
            "confidence_level": "high",
        })

    monkeypatch.setattr(ai_base, "web_search_completion", fake_web_search)
    monkeypatch.setattr(
        shg,
        "LOCAL_CITATION_WEBSEARCH_PROMPT_PATH",
        SimpleNamespace(
            read_text=lambda: (
                "Names: {case_name_candidates}\n"
                "Draft: {citation_as_drafted}\n"
                "Reporter: {drafted_reporter_citation}"
            )
        ),
    )

    result = shg._resolve_citation_via_web_search(
        ["Smith v. Jones"],
        citation_as_drafted="Smith v. Jones, 123 F.3d 456",
        drafted_reporter_citation="123 F.3d 456",
    )

    assert result["resolved"] is True
    assert "Smith v. Jones" in captured["prompt"]
    assert set(captured) == {"prompt"}


def test_shared_guard_keeps_quotes_with_their_citations(monkeypatch):
    from backend.ai import base as ai_base
    import backend.hallucination_guard as hg

    draft_text = (
        'Alpha v. Beta, 101 F.3d 1, states "the first rule." '
        'Gamma v. Delta, 202 F.3d 2, states "the second rule."'
    )
    case_rows = [
        {
            "case_name": "Alpha v. Beta",
            "citation": "101 F.3d 1",
            "is_valid": True,
            "is_applicable": True,
            "quotes": ["the first rule"],
        },
        {
            "case_name": "Gamma v. Delta",
            "citation": "202 F.3d 2",
            "is_valid": True,
            "is_applicable": False,
            "reason": "The second cited proposition is unsupported.",
            "quotes": ["the second rule"],
        },
    ]
    provider_calls = []

    monkeypatch.setattr(hg, "verify_citations_with_lookup_tags_lookup_safe", lambda _text: ([], []))
    monkeypatch.setattr(hg, "extract_validated_cases_from_citations", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(hg, "_enrich_validated_cases_for_standalone", lambda _cases: None)
    monkeypatch.setattr(hg, "_build_citation_link_map", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        hg,
        "_load_local_hallucination_prompt",
        lambda: "Cases: {validated_cases_text}\nDraft: {draft_text}",
    )
    monkeypatch.setattr(
        hg,
        "LOCAL_STATUTES_PROMPT_PATH",
        SimpleNamespace(read_text=lambda: "Statutes: {draft_text}"),
    )
    monkeypatch.setattr(ai_base, "num_tokens_from_string", lambda _prompt: 5)

    def create(**kwargs):
        provider_calls.append(kwargs)
        prompt = kwargs["input"][-1]["content"]
        output = {"results": case_rows if prompt.startswith("Cases:") else []}
        return SimpleNamespace(
            id="guard-route", model=kwargs["model"], status="completed",
            output_text=json.dumps(output), output=[], usage=None,
        )

    monkeypatch.setattr(ai_base, "get_openai_client", lambda: SimpleNamespace(
        responses=SimpleNamespace(create=create),
    ))
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(ai_base, "record_guided_llm_response", lambda *_a, **_kw: None)

    result = hg.run_hallucination_guard_on_text(draft_text)

    assert result.get("error") is not True
    assert len(provider_calls) == 2
    assert all(call["model"] == "gpt-6.1-sol" for call in provider_calls)
    assert all(call["reasoning"] == {"effort": "high"} for call in provider_calls)
    assert all(draft_text in call["input"][-1]["content"] for call in provider_calls)
    rows_by_citation = {row["citation"]: row for row in result["case_results"]}
    assert set(rows_by_citation) == {"101 F.3d 1", "202 F.3d 2"}
    assert rows_by_citation["101 F.3d 1"]["quotes"] == [{
        "text": "the first rule", "status": "not_checked", "opinion_type": None,
    }]
    assert rows_by_citation["202 F.3d 2"]["quotes"] == [{
        "text": "the second rule", "status": "not_checked", "opinion_type": None,
    }]
    assert rows_by_citation["202 F.3d 2"]["reason"] == "The second cited proposition is unsupported."


def test_shared_guard_sends_prompts_above_former_200k_cap(monkeypatch):
    from backend.ai import base as ai_base
    import backend.hallucination_guard as hg

    sent_prompts = []
    task = RecordingTask()

    monkeypatch.setattr(hg, "verify_citations_with_lookup_tags_lookup_safe", lambda _text: ([], []))
    monkeypatch.setattr(hg, "extract_validated_cases_from_citations", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(hg, "_enrich_validated_cases_for_standalone", lambda _cases: None)
    monkeypatch.setattr(hg, "_build_citation_link_map", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        hg, "_load_local_hallucination_prompt", lambda: "Cases: {validated_cases_text}\nDraft: {draft_text}"
    )
    monkeypatch.setattr(
        hg, "LOCAL_STATUTES_PROMPT_PATH", SimpleNamespace(read_text=lambda: "Statutes: {draft_text}")
    )
    monkeypatch.setattr(
        ai_base,
        "num_tokens_from_string",
        lambda prompt: 200_001 if prompt.startswith("Cases:") else 200_002,
    )
    monkeypatch.setattr(hg, "standalone_hg_show_dev_progress_details", lambda: True)

    def fake_completion(prompt, model, reasoning_effort=None):
        sent_prompts.append((prompt, model, reasoning_effort))
        return '{"results": []}'

    monkeypatch.setattr(ai_base, "chat_completion_j", fake_completion)

    result = hg.run_hallucination_guard_on_text("Draft text", task=task)

    assert result.get("error") is not True
    assert result["prompt_token_counts"] == [200_001, 200_002]
    assert "token_limit" not in result
    assert len(sent_prompts) == 2
    assert all(model == "gpt-6.1-sol" and effort == "high" for _, model, effort in sent_prompts)
    analysis_progress = [
        update["meta"] for update in task.updates if update["meta"].get("progress") == 80
    ]
    assert len(analysis_progress) == 1
    assert analysis_progress[0]["dev_progress_details"]["token_counts"] == [200_001, 200_002]
    assert "token_limit" not in analysis_progress[0]["dev_progress_details"]


def test_attach_web_search_suggestions_only_searches_invalid_rows(monkeypatch):
    import backend.standalone_hg as shg

    searched = []

    def fake_resolve(case_name_candidates, citation_as_drafted="", drafted_reporter_citation=""):
        searched.append(list(case_name_candidates))
        if any("Calpine" in c for c in case_name_candidates):
            return {
                "resolved": True,
                "resolved_casename": "Nevada Power Co. v. Calpine Corp.",
                "resolved_reporter_citation": "365 B.R. 401",
                "url": "https://www.courtlistener.com/opinion/123/calpine/",
                "confidence_level": "high",
            }
        return {
            "resolved": False,
            "resolved_casename": "",
            "resolved_reporter_citation": "",
            "url": "",
            "confidence_level": "low",
        }

    monkeypatch.setattr(shg, "_resolve_citation_via_web_search", fake_resolve)

    results = [
        {"citation": "100 U.S. 1", "citation_as_drafted": "Good v. Valid, 100 U.S. 1", "is_valid": True},
        {"citation": "365 B.R. 401", "citation_as_drafted": "In re Calpine Corp., 365 B.R. 401", "is_valid": False},
        {"citation": "999 F.3d 9", "citation_as_drafted": "Ghost v. Phantom, 999 F.3d 9", "is_valid": False},
        {
            "citation": "No. 24-12345 (ABC)",
            "citation_as_drafted": "In re Example Corp., No. 24-12345 (ABC)",
            "citation_kind": "docket_order",
            "is_valid": None,
        },
    ]
    out = shg._attach_web_search_suggestions(results, None)

    # Only ordinary is_valid=false case rows are searched; validated and docket-order rows are skipped.
    flat_searched = [candidate for candidates in searched for candidate in candidates]
    assert "Good v. Valid" not in flat_searched
    assert "In re Calpine Corp." in flat_searched
    assert "Ghost v. Phantom" in flat_searched
    assert "In re Example Corp." not in flat_searched
    assert all("100 U.S. 1" not in s for s in flat_searched)
    assert all("365 B.R. 401" not in s for s in flat_searched)
    assert all("999 F.3d 9" not in s for s in flat_searched)
    # A resolved high-confidence hit attaches the did_you_mean payload (no confidence stored).
    assert out[1]["did_you_mean"] == {
        "casename": "Nevada Power Co. v. Calpine Corp.",
        "reporter_citation": "365 B.R. 401",
        "url": "https://www.courtlistener.com/opinion/123/calpine/",
    }
    # An unresolved hit and the valid row carry no suggestion.
    assert "did_you_mean" not in out[2]
    assert "did_you_mean" not in out[0]
    assert "did_you_mean" not in out[3]


def test_attach_web_search_suggestions_skips_rows_without_case_name_candidates(monkeypatch):
    import backend.standalone_hg as shg

    searched = []
    monkeypatch.setattr(shg, "_resolve_citation_via_web_search", lambda *a, **k: searched.append(a) or None)

    out = shg._attach_web_search_suggestions(
        [{"citation": "1 U.S. 1", "citation_as_drafted": "1 U.S. 1", "is_valid": False}], None
    )

    assert searched == []
    assert "did_you_mean" not in out[0]


def test_attach_web_search_suggestions_requires_url_name_and_citation(monkeypatch):
    import backend.standalone_hg as shg

    # "resolved" + high but missing the URL -> not surfaced (we never link without a real source).
    monkeypatch.setattr(shg, "_resolve_citation_via_web_search", lambda *a, **k: {
        "resolved": True, "resolved_casename": "Some Case", "resolved_reporter_citation": "1 F.3d 1",
        "url": "", "confidence_level": "high",
    })
    out = shg._attach_web_search_suggestions(
        [{"citation": "1 F.3d 1", "citation_as_drafted": "Some Case, 1 F.3d 1", "is_valid": False}], None
    )
    assert "did_you_mean" not in out[0]


def test_attach_web_search_suggestions_only_high_confidence(monkeypatch):
    import backend.standalone_hg as shg

    # A fully-resolved hit with everything present but only medium confidence is NOT surfaced.
    monkeypatch.setattr(shg, "_resolve_citation_via_web_search", lambda *a, **k: {
        "resolved": True, "resolved_casename": "Some Case", "resolved_reporter_citation": "1 F.3d 1",
        "url": "https://www.courtlistener.com/opinion/9/some-case/", "confidence_level": "medium",
    })
    out = shg._attach_web_search_suggestions(
        [{"citation": "1 F.3d 1", "citation_as_drafted": "Some Case, 1 F.3d 1", "is_valid": False}], None
    )
    assert "did_you_mean" not in out[0]


def test_attach_web_search_suggestions_swallows_errors(monkeypatch):
    import backend.standalone_hg as shg

    def boom(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr(shg, "_resolve_citation_via_web_search", boom)
    results = [{"citation": "1 U.S. 1", "citation_as_drafted": "X v. Y, 1 U.S. 1", "is_valid": False}]
    out = shg._attach_web_search_suggestions(results, None)  # must not raise
    assert "did_you_mean" not in out[0]


def test_case_table_did_you_mean_column_present_when_suggested():
    from backend.everything import _guard_results_to_html

    html = _guard_results_to_html([{
        "case_name": "In re Calpine Corp.",
        "citation": "365 B.R. 401",
        "citation_as_drafted": "In re Calpine Corp., 365 B.R. 401",
        "is_valid": False,
        "is_applicable": False,
        "reason": "Not found in our database",
        "did_you_mean": {
            "casename": "Nevada Power Co. v. Calpine Corp.",
            "reporter_citation": "365 B.R. 401",
            "url": "https://www.courtlistener.com/opinion/123/calpine/",
            "confidence_level": "high",
        },
    }])
    assert "Possible match" in html
    assert '<a href="https://www.courtlistener.com/opinion/123/calpine/"' in html
    assert "Nevada Power Co. v. Calpine Corp. 365 B.R. 401</a>" in html
    assert html.count(
        'data-tooltip="A possible matching authority when the citation could not be confidently located."'
    ) == 1
    assert html.count('class="hg-cell-status hg-cell-status--negative"') == 1
    assert "—" in html
    # Confidence gates the row but is never shown to the user.
    assert "confidence" not in html.lower()


def test_case_table_column_order_and_renamed_headers():
    from backend.everything import _guard_results_to_html

    html = _guard_results_to_html([{
        "case_name": "Smith v. Jones",
        "citation": "123 F.2d 456",
        "citation_as_drafted": "Smith v. Jones, 123 F.2d 456",
        "is_valid": True,
        "is_applicable": True,
        "lookup_tag": "VERIFIED_FL",
    }])
    assert "Case Citation" not in html
    assert "Validated" not in html
    headers = [
        "Citation as drafted",
        "Where cited",
        "Citation reviewed",
        "Result",
        "Authority found",
        "Supports proposition",
    ]
    positions = [html.index(header) for header in headers]
    assert positions == sorted(positions)
    assert 'class="hg-tooltip-cell"' in html
    assert "<th" in html and 'class="hg-tooltip-cell"' in html
    assert not re.search(r"<td\b[^>]*\bhg-tooltip-cell\b", html)
    assert html.count('data-tooltip="The citation text exactly as it appears in your document."') == 1
    assert html.count('data-tooltip="Whether the cited authority was located in our legal database."') == 1
    assert html.count('data-tooltip="Whether the authority supports the proposition in your document."') == 1
    assert 'title="The citation text exactly as it appears in your document."' not in html


def test_guard_compact_location_uses_legal_pinpoint_format():
    assert _guard_compact_location(
        "The Standards for a Preliminary Injunction Are Satisfied, paragraph 27; "
        "The Debtors Will Suffer Irreparable Harm, paragraph 33; paragraph 35; "
        "paragraph 37; Discovery Authorization, paragraph 55"
    ) == "¶¶ 27, 33, 35, 37, 55"
    assert _guard_compact_location("paragraphs 12, 14, and 19") == "¶¶ 12, 14, 19"
    assert _guard_compact_location("Background, paragraph 21") == "¶ 21"
    assert _guard_compact_location("pages 12-14") == "pp. 12–14"
    assert _guard_compact_location("Section II.A") == "§ II.A"


def test_case_table_optional_column_order_quote_before_possible_lookup():
    from backend.everything import _guard_results_to_html

    html = _guard_results_to_html([{
        "case_name": "In re Calpine Corp.",
        "citation": "365 B.R. 401",
        "citation_as_drafted": "In re Calpine Corp., 365 B.R. 401",
        "is_valid": False,
        "is_applicable": False,
        "quotes": [{"text": "quoted language", "status": "not_checked"}],
        "quote_check": {"total": 1, "found": 0},
        "did_you_mean": {
            "casename": "Nevada Power Co. v. Calpine Corp.",
            "reporter_citation": "365 B.R. 401",
            "url": "https://www.courtlistener.com/opinion/123/calpine/",
        },
    }])

    assert html.index("Quotation review") < html.index("Possible match")
    assert not re.search(r"<td\b[^>]*\bhg-tooltip-cell\b", html)
    assert html.count(
        'data-tooltip="Whether quoted language was verified against the matched authority text."'
    ) == 1
    assert html.count(
        'data-tooltip="A possible matching authority when the citation could not be confidently located."'
    ) == 1


def test_guard_issue_text_drops_generic_not_found_when_specific_note():
    from backend.everything import _guard_issue_text

    # Specific lookup note present -> the generic "Not found in our database" is suppressed.
    text = _guard_issue_text({
        "lookup_tag": "LACKS_OPINION_DATA",
        "possible_issue": "We could not confirm this citation from CourtListener. It may be a database gap or an incorrect citation; check Westlaw or Lexis.",
        "reason": "Not found in our database",
        "is_valid": False,
    })
    assert "LACKS_OPINION_DATA" in text
    assert "database gap" in text
    assert "Not found in our database" not in text


def test_guard_issue_text_keeps_not_found_when_no_note():
    from backend.everything import _guard_issue_text

    # No specific note -> the generic reason still shows for a plain unvalidated citation.
    text = _guard_issue_text({
        "lookup_tag": "",
        "possible_issue": "",
        "reason": "Not found in our database",
        "is_valid": False,
    })
    assert text == "Not found in our database"


def test_case_table_no_did_you_mean_column_when_absent():
    from backend.everything import _guard_results_to_html

    html = _guard_results_to_html([{
        "case_name": "Smith v. Jones",
        "citation": "123 F.2d 456",
        "citation_as_drafted": "Smith v. Jones, 123 F.2d 456",
        "is_valid": True,
        "is_applicable": True,
        "lookup_tag": "VERIFIED_FL",
    }])
    # No suggestion on any row -> the column is omitted entirely (in-draft guard table unchanged).
    assert "Possible match" not in html


def test_standalone_hg_uses_diagnostic_lookup_but_keeps_file_text_for_prompt():
    src = (_repo_root() / "backend/hallucination_guard.py").read_text()

    assert "verify_citations_with_lookup_tags_lookup_safe(file_text)" in src
    assert "draft_text=file_text" in src
    assert "standalone_hg/lookup_diagnostics" in src


def test_everything_hg_uses_diagnostic_lookup_and_both_summary_tables():
    src = (_repo_root() / "backend/everything.py").read_text()
    shared_src = (_repo_root() / "backend/hallucination_guard.py").read_text()

    assert "_hg.run_hallucination_guard_on_text(" in src
    assert "verify_citations_with_lookup_tags_lookup_safe(file_text)" in shared_src
    case_extract_idx = shared_src.index("case_results = _extract_guard_results(cases_llm_output)")
    statute_extract_idx = shared_src.index("statute_results = _extract_guard_results(statutes_llm_output)")
    case_coverage_idx = shared_src.index("_ensure_case_result_coverage(case_results, lookup_diagnostics)")
    docket_coverage_idx = shared_src.index("_ensure_docket_order_coverage(case_results, file_text)")
    statute_coverage_idx = shared_src.index("_ensure_statute_result_coverage(statute_results, filtered_citations)")
    diagnostics_idx = shared_src.index("_guard_attach_lookup_diagnostics(case_results, lookup_diagnostics)")
    case_quote_idx = shared_src.index("_attach_quote_verification(case_results, validated_cases)")
    statute_quote_idx = shared_src.index("_attach_statute_quote_review(statute_results, file_text)")
    web_search_idx = shared_src.index("_attach_web_search_suggestions(case_results, task)")
    render_idx = shared_src.index("case_results_html = _guard_results_to_html(case_results)")
    assert case_extract_idx < case_coverage_idx < docket_coverage_idx < statute_coverage_idx < diagnostics_idx
    assert statute_extract_idx < statute_coverage_idx < diagnostics_idx
    assert case_quote_idx < statute_quote_idx < web_search_idx < render_idx
    assert "_guard_results_to_html(case_results)" in shared_src
    assert "_guard_statutes_to_html(statute_results)" in shared_src


def test_enrich_validated_cases_dedupes_cluster(monkeypatch):
    calls = {"cluster": 0, "opinion": 0}

    def fake_get_cluster(cluster_id):
        calls["cluster"] += 1
        return {
            "case_name": "Imax Corporation v. Cinema Technologies, Inc.",
            "absolute_url": "/opinion/757263/imax-corporation-v-cinema-technologies-inc/",
            "sub_opinions": ["https://www.courtlistener.com/api/rest/v4/opinions/2669095/"],
            "docket": "https://www.courtlistener.com/api/rest/v4/dockets/3045845/",
        }

    def fake_get_docket(_url):
        return {"court": "https://www.courtlistener.com/api/rest/v4/courts/ca9/"}

    def fake_get_court(_url):
        return {"citation_string": "9th Cir."}

    def fake_get_opinion(_url):
        calls["opinion"] += 1
        return {"html_with_citations": "<p>Opinion text</p>"}

    def fake_extract_holding(_opinion):
        return "The court affirmed dismissal."

    monkeypatch.setattr("backend.standalone_hg.get_cluster", fake_get_cluster)
    monkeypatch.setattr("backend.standalone_hg.get_docket", fake_get_docket)
    monkeypatch.setattr("backend.standalone_hg.get_court", fake_get_court)
    monkeypatch.setattr("backend.standalone_hg.get_opinion", fake_get_opinion)
    monkeypatch.setattr("backend.standalone_hg.extract_holding_from_opinion", fake_extract_holding)
    monkeypatch.setattr("backend.standalone_hg.get_freelaw_cluster_cache", lambda _cluster_id: None)
    monkeypatch.setattr("backend.standalone_hg.upsert_freelaw_cluster_cache", lambda _snapshot: None)

    validated_cases = {
        "152 F.3d 1161": {"clusterId": 757263, "citation": "152 F.3d 1161", "case_name": ""},
        "152 F.3d 1161 n2": {"clusterId": 757263, "citation": "152 F.3d 1161 n2", "case_name": ""},
    }

    _enrich_validated_cases_for_standalone(validated_cases)

    # One external enrichment call for shared cluster, both entries updated.
    assert calls["cluster"] == 1
    assert calls["opinion"] == 1
    assert validated_cases["152 F.3d 1161"]["llm_holding"] == "The court affirmed dismissal."
    assert validated_cases["152 F.3d 1161"]["opinion_id"] == 2669095
    assert validated_cases["152 F.3d 1161"]["court"] == "9th Cir."
    assert validated_cases["152 F.3d 1161 n2"]["llm_holding"] == "The court affirmed dismissal."
    assert validated_cases["152 F.3d 1161 n2"]["clusterId"] == 757263
    assert validated_cases["152 F.3d 1161 n2"]["citation"] == "152 F.3d 1161 n2"


def test_enrich_validated_cases_isolation_on_failure(monkeypatch):
    def fake_get_cluster(cluster_id):
        if cluster_id == 1:
            raise RuntimeError("boom")
        return {
            "case_name": "Valid Case",
            "absolute_url": "/opinion/2/valid-case/",
            "sub_opinions": [],
            "docket": None,
        }

    monkeypatch.setattr("backend.standalone_hg.get_cluster", fake_get_cluster)
    monkeypatch.setattr("backend.standalone_hg.get_docket", lambda _url: None)
    monkeypatch.setattr("backend.standalone_hg.get_court", lambda _url: None)
    monkeypatch.setattr("backend.standalone_hg.get_opinion", lambda _url: None)
    monkeypatch.setattr(
        "backend.standalone_hg.extract_holding_from_opinion",
        lambda _opinion, **_kwargs: "",
    )
    monkeypatch.setattr("backend.standalone_hg.get_freelaw_cluster_cache", lambda _cluster_id: None)
    monkeypatch.setattr("backend.standalone_hg.upsert_freelaw_cluster_cache", lambda _snapshot: None)

    validated_cases = {
        "bad case": {"clusterId": 1, "citation": "bad case"},
        "good case": {"clusterId": 2, "citation": "good case"},
    }

    _enrich_validated_cases_for_standalone(validated_cases)

    # The failing case does not block other entries.
    assert validated_cases["good case"]["case_name"] == "Valid Case"
    assert validated_cases["good case"]["freelaw_url"] == "https://www.courtlistener.com/opinion/2/valid-case/"


def test_enrich_validated_cases_uses_freelaw_cluster_cache(monkeypatch):
    calls = {"cluster": 0, "hit": 0}

    class CachedRow:
        cluster_id = 757263
        llm_holding = "The cached holding controls."
        payload_json = json.dumps({
            "clusterId": 757263,
            "cluster_id": 757263,
            "id": 757263,
            "citation": "cached citation",
            "case_name": "Cached Case",
            "opinion_id": 2669095,
            "opinion_url": "https://www.courtlistener.com/api/rest/v4/opinions/2669095/",
            "docket_url": "https://www.courtlistener.com/api/rest/v4/dockets/3045845/",
            "llm_holding": "The cached holding controls.",
            "freelaw_url": "https://www.courtlistener.com/opinion/757263/cached-case/",
            "court": "9th Cir.",
            "source": "freelaw_cache",
        })

    def fake_get_cluster(_cluster_id):
        calls["cluster"] += 1
        return {}

    def fake_record_hit(_row):
        calls["hit"] += 1

    monkeypatch.setattr("backend.standalone_hg.get_cluster", fake_get_cluster)
    monkeypatch.setattr("backend.standalone_hg.get_opinion", lambda _url: pytest.fail("opinion should not be fetched"))
    monkeypatch.setattr(
        "backend.standalone_hg.extract_holding_from_opinion",
        lambda _opinion, **_kwargs: pytest.fail("holding should not be generated"),
    )
    monkeypatch.setattr("backend.standalone_hg.get_freelaw_cluster_cache", lambda _cluster_id: CachedRow())
    monkeypatch.setattr("backend.standalone_hg.record_freelaw_cluster_cache_hit", fake_record_hit)

    validated_cases = {
        "152 F.3d 1161": {"clusterId": "757263", "citation": "152 F.3d 1161", "case_name": ""},
        "parallel cite": {"clusterId": 757263, "citation": "parallel cite", "case_name": ""},
    }

    _enrich_validated_cases_for_standalone(validated_cases)

    assert calls["cluster"] == 0
    assert calls["hit"] == 1
    assert validated_cases["152 F.3d 1161"]["llm_holding"] == "The cached holding controls."
    assert validated_cases["152 F.3d 1161"]["citation"] == "152 F.3d 1161"
    assert validated_cases["parallel cite"]["llm_holding"] == "The cached holding controls."
    assert validated_cases["parallel cite"]["citation"] == "parallel cite"


def test_enrich_validated_cases_incomplete_cache_falls_back_and_upserts(monkeypatch):
    calls = {"cluster": 0, "opinion": 0}
    upserts = []

    class IncompleteCachedRow:
        cluster_id = 757263
        llm_holding = None
        payload_json = json.dumps({"clusterId": 757263})

    def fake_get_cluster(cluster_id):
        calls["cluster"] += 1
        return {
            "case_name": "Fresh Case",
            "absolute_url": "/opinion/757263/fresh-case/",
            "sub_opinions": ["https://www.courtlistener.com/api/rest/v4/opinions/2669095/"],
            "docket": None,
        }

    def fake_get_opinion(_url):
        calls["opinion"] += 1
        return {"html_with_citations": "<p>Opinion text</p>"}

    monkeypatch.setattr("backend.standalone_hg.get_freelaw_cluster_cache", lambda _cluster_id: IncompleteCachedRow())
    monkeypatch.setattr("backend.standalone_hg.get_cluster", fake_get_cluster)
    monkeypatch.setattr("backend.standalone_hg.get_docket", lambda _url: None)
    monkeypatch.setattr("backend.standalone_hg.get_court", lambda _url: None)
    monkeypatch.setattr("backend.standalone_hg.get_opinion", fake_get_opinion)
    monkeypatch.setattr(
        "backend.standalone_hg.extract_holding_from_opinion",
        lambda _opinion, **_kwargs: "Fresh holding.",
    )
    monkeypatch.setattr("backend.standalone_hg.upsert_freelaw_cluster_cache", lambda snapshot: upserts.append(snapshot))

    validated_cases = {
        "152 F.3d 1161": {"clusterId": 757263, "citation": "152 F.3d 1161", "case_name": ""},
    }

    _enrich_validated_cases_for_standalone(validated_cases)

    assert calls["cluster"] == 1
    assert calls["opinion"] == 1
    assert validated_cases["152 F.3d 1161"]["llm_holding"] == "Fresh holding."
    assert upserts[0]["clusterId"] == 757263
    assert upserts[0]["llm_holding"] == "Fresh holding."


def test_standalone_hg_task_returns_friendly_error_on_unhandled_exception(monkeypatch):
    """An unhandled exception in the task body becomes a clean, retryable error (not a hard FAILURE
    surfaced to the UI as the opaque generic message)."""
    import backend.standalone_hg as shg

    def _boom(*_args, **_kwargs):
        raise RuntimeError("internal boom")

    # Force a failure at the very first body step.
    monkeypatch.setattr(shg.ChatThread, "resolve_id", _boom)

    result = shg.standalone_hg_task.run({"draftId": "any-id"})

    assert result.get("error") is True
    assert "Please try again" in result.get("error_message", "")
    assert "boom" not in result.get("error_message", "")  # internal detail stays in logs, not the UI


def test_get_active_standalone_hg_task_id_clears_completed_task(monkeypatch):
    import backend.standalone_hg as shg

    mem = {shg.STANDALONE_HG_ACTIVE_UPLOAD_TASK_KEY: "upload-task-1"}

    class ReadyResult:
        def ready(self):
            return True

    monkeypatch.setattr(shg, "DBMemory", lambda db, doc_id: mem)
    monkeypatch.setattr(
        shg,
        "import_module",
        lambda name: SimpleNamespace(AsyncResult=lambda task_id: ReadyResult()),
    )

    assert shg.get_active_standalone_hg_task_id(123, shg.STANDALONE_HG_ACTIVE_UPLOAD_TASK_KEY) is None
    assert shg.STANDALONE_HG_ACTIVE_UPLOAD_TASK_KEY not in mem


def test_route_standalone_hg_stores_active_task_id(monkeypatch):
    import backend.standalone_hg as shg

    mem = {}
    request = SimpleNamespace(get_json=lambda silent=True: {"draftId": "draft-public-id"}, form={})
    uploaded_file = SimpleNamespace(content="See 123 F.3d 456.")

    monkeypatch.setattr(shg.ChatThread, "resolve_id", lambda draft_id: 123)
    monkeypatch.setattr(
        shg,
        "UploadedFiles",
        SimpleNamespace(query=SimpleNamespace(filter_by=lambda **kwargs: SimpleNamespace(first=lambda: uploaded_file))),
    )
    monkeypatch.setattr(shg, "DBMemory", lambda db, doc_id: mem)
    monkeypatch.setattr(shg, "get_active_standalone_hg_task_id", lambda doc_id, key: None)
    monkeypatch.setattr(shg, "clear_standalone_hg_result_state", lambda mem, reset_validated_cases=True: None)
    monkeypatch.setattr(shg.standalone_hg_task, "delay", lambda payload: SimpleNamespace(id="hg-task-1"))

    response, status = shg.route_standalone_hg(request)

    assert status == 200
    assert response["result_id"] == "hg-task-1"
    assert mem[shg.STANDALONE_HG_ACTIVE_HG_TASK_KEY] == "hg-task-1"


def test_route_standalone_hg_reuses_active_task_without_enqueuing(monkeypatch):
    import backend.standalone_hg as shg

    request = SimpleNamespace(
        get_json=lambda silent=True: {"draftId": "draft-public-id"}, form={}
    )
    uploaded_file = SimpleNamespace(content="See 123 F.3d 456.")

    monkeypatch.setattr(shg.ChatThread, "resolve_id", lambda _draft_id: 123)
    monkeypatch.setattr(
        shg,
        "UploadedFiles",
        SimpleNamespace(
            query=SimpleNamespace(
                filter_by=lambda **_kwargs: SimpleNamespace(
                    first=lambda: uploaded_file
                )
            )
        ),
    )
    monkeypatch.setattr(shg, "DBMemory", lambda db, doc_id: {})
    monkeypatch.setattr(
        shg,
        "get_active_standalone_hg_task_id",
        lambda _doc_id, _key: "existing-task",
    )
    monkeypatch.setattr(
        shg.standalone_hg_task,
        "delay",
        lambda _payload: pytest.fail("an already-running Guard must not be enqueued"),
    )

    response, status = shg.route_standalone_hg(request)

    assert status == 200
    assert response["result_id"] == "existing-task"
    assert response["already_running"] is True
