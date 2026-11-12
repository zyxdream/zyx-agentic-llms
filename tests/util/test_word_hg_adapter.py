"""The Word surface must call the real shared engine and not upgrade uncertainty."""
from backend.word_addin import service
from backend import hallucination_guard as hg


def test_word_calls_shared_guard_and_preserves_review_states(monkeypatch):
    seen = []
    valid_tag = next(iter(hg.LOOKUP_TAGS_VALIDATED_CASES))
    def guard(text, **kwargs):
        seen.append((text, kwargs))
        return {"case_results": [
            {"citation": "Citation A", "lookup_tag": valid_tag, "is_valid": True, "is_applicable": True,
             "quotes": [{"text": "Actual words", "status": "found"}]},
            {"citation": "Citation B", "is_valid": True, "is_applicable": True},
            {"citation": "Citation C", "lookup_tag": valid_tag, "is_valid": True, "is_applicable": True,
             "quotes": [{"text": "Unfetched words", "status": "not_checked"}]},
        ], "statute_results": [{"citation": "Statute D", "is_valid": True, "is_applicable": True}]}
    monkeypatch.setattr(hg, "run_hallucination_guard_on_text", guard)
    result = service.guard_result('Citation A; Citation B; Citation C; Statute D')
    assert seen[0][1]["source_label"] == "Word passage"
    assert [f["status"] for f in result["findings"]] == ["verified", "review", "review", "review"]
    assert result["findings"][0]["quotes"][0]["text"] == "Actual words"


def test_empty_guard_output_is_not_an_all_clear(monkeypatch):
    monkeypatch.setattr(service, "run_guard", lambda text: {"case_results": [], "statute_results": []})
    result = service.guard_result('Text')
    assert "not a finding" in result["text"]
    assert result["findings"] == []
