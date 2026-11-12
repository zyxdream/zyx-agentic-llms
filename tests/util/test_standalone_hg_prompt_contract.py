from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_standalone_cases_prompt_enforces_full_form_wl_lexis_coverage():
    prompt = (_repo_root() / "backend/ai/prompts/hg/HALLUCINATION_GUARD_CASES").read_text()

    assert "distinct drafted full-form citation variants" in prompt
    assert "Westlaw citations" in prompt
    assert "LEXIS citations" in prompt
    assert "2025 Bankr. LEXIS 2284" in prompt
    assert "Prefer the first full/identifying mention" in prompt
    assert "earliest mention containing both a drafted case name" in prompt
    assert "full-form reporter/WL/LEXIS citation" in prompt
    assert "If the document never gives a drafted case name" in prompt
    assert "same drafted full-form citation variant" in prompt
    assert 'reason="Not found in our database"' in prompt
    assert "output record count MUST equal the number of distinct drafted full-form citation variants" in prompt
    assert '"case_name"' not in prompt
    assert "Do not exclude a full reporter/WL/LEXIS citation merely because the source text lacks a drafted case name" in prompt
    # A clustered citation is a valid case even when the drafted name differs; the
    # name discrepancy must NOT force is_applicable=false.
    assert "the case name as drafted conflicts with the validated case name" not in prompt
    # Valid-but-unassessable applicability is neutral (null), not an error (false).
    assert "null if the case is valid but applicability CANNOT be assessed" in prompt
    assert '"is_applicable": null' in prompt
    # The LLM emits a semantic name_mismatch flag the renderer uses to (re)decide CASE_NAME_MISMATCH.
    assert '"name_mismatch"' in prompt
    assert 'Infer the drafted case name from "citation_as_drafted"' in prompt
    assert 'does NOT include any drafted case name' in prompt
    assert '"name_mismatch": true' in prompt
    assert "refer to DIFFERENT cases" in prompt
    assert '"name_mismatch": false' in prompt


def test_cases_prompt_preserves_conflicting_drafted_citation_variants():
    prompt = (_repo_root() / "backend/ai/prompts/hg/HALLUCINATION_GUARD_CASES").read_text()

    assert "drafted citation variant is the drafted reporter/WL/LEXIS citation stripped only of pincite, court, and year" in prompt
    assert "Do NOT merge different full-form citation variants" in prompt
    assert "The same case entity does not cure a conflicting drafted citation" in prompt
    assert "must not rescue a conflicting drafted volume, reporter, or starting page" in prompt
    assert "For an invalid or conflicting drafted variant, keep the suspect drafted base citation" in prompt

    assert "Bryant v. Military Department of Mississippi" in prompt
    assert "597 F.3d 678" in prompt
    assert "97 F.3d 678" in prompt
    assert "Bryant, 597 F.3d at 694" in prompt
    assert 'return a separate row for the "97 F.3d 678" variant' in prompt


def test_everything_inline_cases_prompt_has_name_mismatch_flag():
    everything_text = (_repo_root() / "backend/everything.py").read_text()
    prompt = (_repo_root() / "backend/ai/prompts/hg/HALLUCINATION_GUARD_CASES").read_text()

    assert "_hg.run_hallucination_guard_on_text(" in everything_text
    assert '"name_mismatch"' in prompt
    assert '"name_mismatch": false' in prompt


def test_cases_prompt_distinguishes_parentheticals_from_quote_spans():
    prompt = (_repo_root() / "backend/ai/prompts/hg/HALLUCINATION_GUARD_CASES").read_text()

    assert "Do not treat ordinary explanatory parentheticals as quotes" in prompt
    assert "For mixed parentheticals, return only the text inside the quotation marks" in prompt
    assert "unquoted parenthetical text must not be sent in \"quotes\"" in prompt
    assert "Do not include lead-in words outside the quotation marks" in prompt
    assert "Do NOT include the citation itself, quotation marks, or surrounding non-quoted text" in prompt

    assert "Matta v. May, 118 F.3d 410, 413 (5th Cir. 1997)" in prompt
    assert "reversing § 1927 sanctions" in prompt
    assert "-> quotes: []" in prompt
    assert "Puckett v. United States, 556 U.S. 129, 135 (2009)" in prompt
    assert 'structural errors "affect the framework within which the trial proceeds"' in prompt
    assert '-> quotes: ["affect the framework within which the trial proceeds"]' in prompt
    assert '-> quotes: ["[T]he requirement applies only prospectively."]' in prompt


def test_citation_websearch_prompt_contract():
    prompt = (_repo_root() / "backend/ai/prompts/hg/HALLUCINATION_GUARD_CITATION_WEBSEARCH").read_text()
    low = prompt.lower()
    # Web search + the strict credibility/source-backed citation bar.
    assert "web search" in low or "search the web" in low
    assert "credible" in low
    assert "case name only" in low
    assert "source-backed citation" in low
    assert "do not force" in low
    assert "top results" in low
    assert "semantically the same" in low
    # Required output fields and the format placeholders.
    for field in ['"resolved"', '"resolved_casename"', '"resolved_reporter_citation"', '"url"', '"confidence_level"']:
        assert field in prompt
    assert "{case_name_candidates}" in prompt
    assert "{citation_as_drafted}" in prompt
    assert "{drafted_reporter_citation}" in prompt


def test_standalone_statutes_prompt_neutral_applicability_when_no_proposition():
    prompt = (_repo_root() / "backend/ai/prompts/hg/HALLUCINATION_GUARD_STATUTES").read_text()
    assert "null if the statute is valid but applicability CANNOT be assessed" in prompt
    assert '"quotes"' in prompt
    assert "Copy the wording exactly" in prompt
    assert "A separate source-text step" in prompt


def test_standalone_hg_prompt_source_is_separate_from_everything_prompt():
    standalone_hg_text = (_repo_root() / "backend/standalone_hg.py").read_text()
    shared_hg_text = (_repo_root() / "backend/hallucination_guard.py").read_text()

    # Standalone HG should load its own local prompt file, without fallback
    # to backend.everything.HALLUCINATION_GUARD_PROMPT.
    assert "from backend.everything import (\n        HALLUCINATION_GUARD_PROMPT," not in standalone_hg_text
    assert "sys.modules[__name__]" in standalone_hg_text
    assert "_load_local_hallucination_prompt().format(" in shared_hg_text
