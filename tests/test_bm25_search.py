"""
Tests for BM25 search, chunking, keyword expansion, and the end-to-end
get_relevant_content_bm25 pipeline.

Run:
    python -m pytest tests/test_bm25_search.py -v
    # or run individual tests directly:
    python tests/test_bm25_search.py
"""

import sys
import os
import math

import pytest

# ── BM25 pure-function tests (no DB / no OpenAI required) ────────────────────

from backend.util.bm25_search import _tokenize, compute_bm25_scores as _compute_bm25_scores, format_bm25_results
from backend.util.chunker import chunk_text


# ---------------------------------------------------------------------------
# chunk_text tests
# ---------------------------------------------------------------------------

def test_chunk_text_empty():
    assert chunk_text("") == []
    assert chunk_text("   ") == []
    assert chunk_text(None) == []


def test_chunk_text_short():
    """Short text (<800 tokens) should produce a single chunk."""
    text = "This is a short paragraph about contract law."
    result = chunk_text(text)
    assert len(result) == 1
    assert result[0]["chunk_index"] == 0
    assert result[0]["content"] == text
    assert result[0]["token_count"] > 0
    assert result[0]["char_count"] == len(text)


def test_chunk_text_multiple_paragraphs():
    """Multiple short paragraphs should be grouped into chunks up to ~800 tokens."""
    # ~10 words per paragraph, need many paragraphs to exceed 800 tokens
    para = "The defendant breached the contract by failing to deliver goods on time."
    text = ("\n\n".join([para] * 200))  # ~200 paragraphs
    result = chunk_text(text, target_tokens=100)
    assert len(result) > 1
    # Verify chunk indices are sequential
    for i, r in enumerate(result):
        assert r["chunk_index"] == i
    # All content should be preserved (no overlap)
    reconstructed = "\n\n".join(r["content"] for r in result)
    # The reconstructed text should contain the same non-whitespace content
    assert reconstructed.replace("\n", " ").split() == text.replace("\n", " ").split()


def test_chunk_text_long_paragraph():
    """A single very long paragraph should be sub-split by sentences/words."""
    sentence = "The court held that the defendant was liable for breach of fiduciary duty. "
    long_para = sentence * 200  # one paragraph, many sentences
    result = chunk_text(long_para, target_tokens=100)
    assert len(result) > 1
    for r in result:
        assert r["token_count"] > 0


def test_chunk_text_unicode():
    """Unicode text should be handled correctly."""
    text = "El demandado incumplió el contrato.\n\nLe défendeur a rompu le contrat.\n\n被告违反了合同。"
    result = chunk_text(text)
    assert len(result) >= 1
    assert "被告" in result[-1]["content"] or "被告" in result[0]["content"]


# ---------------------------------------------------------------------------
# BM25 scoring tests
# ---------------------------------------------------------------------------

SAMPLE_CHUNKS = [
    {"id": 1, "content": "The plaintiff alleges breach of contract by the defendant corporation."},
    {"id": 2, "content": "Damages were calculated based on lost profits and consequential losses."},
    {"id": 3, "content": "The court reviewed the motion to dismiss for failure to state a claim."},
    {"id": 4, "content": "Contract terms included an arbitration clause and limitation of liability."},
    {"id": 5, "content": "The jury awarded compensatory damages of one million dollars."},
]


def test_tokenize():
    tokens = _tokenize("Hello, World! This is a Test-123.")
    assert tokens == ["hello", "world", "this", "is", "a", "test", "123"]


def test_bm25_empty():
    assert _compute_bm25_scores([], ["breach"]) == []
    assert _compute_bm25_scores(SAMPLE_CHUNKS, []) == []


def test_bm25_basic_ranking():
    """'breach contract' should rank chunk 1 and 4 higher than chunk 3."""
    results = _compute_bm25_scores(SAMPLE_CHUNKS, ["breach", "contract"])
    assert len(results) > 0
    # Chunk 1 mentions both "breach" and "contract"
    top_ids = [r["id"] for r in results[:2]]
    assert 1 in top_ids, f"Expected chunk 1 in top 2, got {top_ids}"


def test_bm25_damages():
    """'damages' should match chunks 2 and 5."""
    results = _compute_bm25_scores(SAMPLE_CHUNKS, ["damages"])
    result_ids = [r["id"] for r in results]
    assert 2 in result_ids
    assert 5 in result_ids


def test_bm25_no_match():
    """Keywords not present in any chunk should return empty."""
    results = _compute_bm25_scores(SAMPLE_CHUNKS, ["xyznonexistent"])
    assert results == []


def test_bm25_scores_decrease():
    """Results should be sorted by score descending."""
    results = _compute_bm25_scores(SAMPLE_CHUNKS, ["damages", "contract", "breach"])
    for i in range(len(results) - 1):
        assert results[i]["bm25_score"] >= results[i + 1]["bm25_score"]


@pytest.mark.parametrize('keywords', [
    ['deadline'], ['deadline', 'deadline'], ['deadline', 'DEADLINE', 'Deadline'],
])
def test_bm25_repeated_query_terms_preserve_matches_and_scores(keywords):
    chunks = [{'id': 1, 'content': 'The filing deadline is June 30.'}]
    expected = _compute_bm25_scores(chunks, ['deadline'])
    assert expected[0]['bm25_score'] == pytest.approx(math.log(4 / 3))
    assert _compute_bm25_scores(chunks, keywords) == expected


def test_bm25_mixed_query_repetitions_do_not_change_ranking_or_scores():
    expected = _compute_bm25_scores(SAMPLE_CHUNKS, ['contract', 'damages', 'breach'])
    repeated = ['Contract', 'damages', 'contract', 'breach', 'DAMAGES', 'breach']
    assert _compute_bm25_scores(SAMPLE_CHUNKS, repeated) == expected


@pytest.mark.parametrize('count', [1, 3])
def test_bm25_common_terms_keep_positive_finite_scores_when_query_repeats(count):
    chunks = [{'id': i, 'content': f'The deadline for filing is day {i}.'} for i in range(count)]
    results = _compute_bm25_scores(chunks, ['deadline'] * 20)
    assert len(results) == count
    assert all(math.isfinite(row['bm25_score']) and row['bm25_score'] > 0 for row in results)
    assert results == _compute_bm25_scores(chunks, ['deadline'])


def test_bm25_repeated_source_words_still_contribute_term_frequency():
    # Equal lengths isolate source term frequency from length normalization.
    chunks = [{'id': 'once', 'content': 'deadline filing notice report'},
              {'id': 'twice', 'content': 'deadline deadline notice report'}]
    results = _compute_bm25_scores(chunks, ['deadline'])
    assert [row['id'] for row in results] == ['twice', 'once']
    assert results[0]['bm25_score'] > results[1]['bm25_score'] > 0
    assert _compute_bm25_scores(chunks, ['deadline', 'DEADLINE']) == results


# ---------------------------------------------------------------------------
# format_bm25_results tests
# ---------------------------------------------------------------------------

def test_format_empty():
    assert format_bm25_results([]) == ""


def test_format_groups_by_file():
    results = [
        {"source_unique_id": "a", "source_filename": "contract.pdf", "source_relative_path": None,
         "chunk_index": 0, "content": "Chunk A0", "bm25_score": 5.0},
        {"source_unique_id": "b", "source_filename": "memo.docx", "source_relative_path": "folder/memo.docx",
         "chunk_index": 1, "content": "Chunk B1", "bm25_score": 3.0},
        {"source_unique_id": "a", "source_filename": "contract.pdf", "source_relative_path": None,
         "chunk_index": 2, "content": "Chunk A2", "bm25_score": 2.0},
    ]
    formatted = format_bm25_results(results)
    # contract.pdf should come first (highest score = 5.0)
    assert formatted.index("contract.pdf") < formatted.index("memo.docx")
    # Within contract.pdf, chunk 0 should come before chunk 2
    assert formatted.index("Chunk A0") < formatted.index("Chunk A2")
    # memo.docx should show relative path
    assert "folder/memo.docx" in formatted


# ---------------------------------------------------------------------------
# Keyword expansion fallback test (no OpenAI needed)
# ---------------------------------------------------------------------------

def test_fallback_keywords():
    """Test fallback keyword extraction without importing full backend."""
    _STOP_WORDS = {
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "need", "dare", "ought",
        "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
        "as", "into", "through", "during", "before", "after", "above", "below",
        "between", "out", "off", "over", "under", "again", "further", "then",
        "once", "here", "there", "when", "where", "why", "how", "all", "each",
        "every", "both", "few", "more", "most", "other", "some", "such", "no",
        "nor", "not", "only", "own", "same", "so", "than", "too", "very",
        "just", "because", "but", "and", "or", "if", "while", "about", "up",
        "that", "this", "these", "those", "what", "which", "who", "whom",
        "it", "its", "i", "me", "my", "we", "our", "you", "your", "he", "she",
        "they", "them", "their", "his", "her",
    }

    def _fallback_keywords(query):
        words = query.lower().split()
        return [w.strip("?.,!;:\"'()") for w in words if w.strip("?.,!;:\"'()") not in _STOP_WORDS and len(w) > 1]

    kws = _fallback_keywords("What are the damages for breach of contract?")
    assert "damages" in kws
    assert "breach" in kws
    assert "contract" in kws
    # Stop words should be removed
    assert "the" not in kws
    assert "are" not in kws
    assert "for" not in kws
    assert "of" not in kws
    assert "what" not in kws


def test_expand_search_keywords_defaults_to_sol(monkeypatch):
    from backend.ai import keyword_expander

    captured = {}

    def fake_chat_completion_parse(prompt, response_model, *, model, reasoning_effort, token_counter=None):
        captured.update(
            prompt=prompt,
            response_model=response_model,
            model=model,
            reasoning_effort=reasoning_effort,
            token_counter=token_counter,
        )
        return response_model(keywords=["breach", "contract"])

    token_counter = {}
    monkeypatch.setattr(keyword_expander.base, "chat_completion_parse", fake_chat_completion_parse)

    assert keyword_expander.expand_search_keywords(
        "breach of contract",
        token_counter=token_counter,
    ) == ["breach", "contract"]
    assert captured["model"] == "gpt-6.1-sol"
    assert captured["reasoning_effort"] is None
    assert captured["token_counter"] is token_counter


def test_expand_search_keywords_preserves_explicit_model_override(monkeypatch):
    from backend.ai import keyword_expander

    captured = {}

    def fake_chat_completion_parse(prompt, response_model, *, model, reasoning_effort, token_counter=None):
        captured["model"] = model
        return response_model(keywords=["damages"])

    monkeypatch.setattr(keyword_expander.base, "chat_completion_parse", fake_chat_completion_parse)

    assert keyword_expander.expand_search_keywords(
        "damages",
        model="gpt-5.5",
    ) == ["damages"]
    assert captured["model"] == "gpt-5.5"


def test_chat_history_retrieval_uses_local_ranking_without_model_calls(monkeypatch):
    from backend import everything

    expansion_calls = []

    def fake_expand_search_keywords(*args, **kwargs):
        expansion_calls.append((args, kwargs))
        return ["damages"]

    monkeypatch.setattr(everything, "expand_search_keywords", fake_expand_search_keywords)
    monkeypatch.setattr(everything, "compute_bm25_scores", lambda *_args: [])
    history = [("First question", "First answer"), ("Second question", "Second answer"),
               ("Third question", "Third answer")]

    assert everything.retrieve_relevant_chat_turns("damages", history) == ""
    assert everything.retrieve_relevant_chat_turns(
        "damages", history, model="gpt-6.1-sol"
    ) == ""
    # Local paragraph ranking needs no model call on ordinary follow-ups.
    assert expansion_calls == []


# ---------------------------------------------------------------------------
# Manual / interactive tests (require DB + optionally OpenAI)
# ---------------------------------------------------------------------------

def manual_test_keyword_expansion():
    """Call the real LLM to expand keywords. Requires OPENAI_API_KEY in env."""
    from backend.ai.keyword_expander import expand_search_keywords

    queries = [
        "What are the damages for breach of contract?",
        "Is there an arbitration clause?",
        "Summarize the plaintiff's allegations of fraud",
        "What did the court say about the statute of limitations?",
    ]
    for q in queries:
        keywords = expand_search_keywords(q)
        print(f"\nQuery: {q}")
        print(f"Keywords ({len(keywords)}): {keywords}")


def manual_test_bm25_with_db(draft_id: int):
    """Run BM25 search against real document chunks in the database.

    Usage:
        python tests/test_bm25_search.py --draft-id 42
    """
    from backend.app import app
    from backend.everything import bm25_search_chunks
    from backend.util.bm25_search import format_bm25_results
    from backend.models import DocumentChunk

    queries = [
        "breach of contract damages",
        "arbitration clause",
        "statute of limitations defense",
        "plaintiff's claims",
    ]

    with app.app_context():
        chunk_count = DocumentChunk.query.filter_by(draft_id=draft_id).count()
        print(f"\nDraft {draft_id} has {chunk_count} document chunks\n")
        if chunk_count == 0:
            print("No chunks found. Upload files first to populate chunks.")
            return

        for q in queries:
            print(f"\n{'='*60}")
            print(f"Query: {q}")
            print(f"{'='*60}")
            from backend.ai.keyword_expander import _fallback_keywords
            keywords = _fallback_keywords(q)
            print(f"Keywords: {keywords}")
            results = bm25_search_chunks(draft_id=draft_id, keywords=keywords, top_k=5)
            if results:
                for r in results:
                    print(f"  score={r['bm25_score']:.3f}  file={r['source_filename']}  chunk={r['chunk_index']}")
                    print(f"    {r['content'][:150]}...")
                formatted = format_bm25_results(results)
                print(f"\nFormatted output ({len(formatted)} chars):")
                print(formatted[:500])
            else:
                print("  No matching chunks found.")


def manual_test_end_to_end(draft_id: int):
    """Full pipeline test: keyword expansion (LLM) → BM25 search → formatted output.

    Requires OPENAI_API_KEY and populated document chunks in DB.

    Usage:
        python tests/test_bm25_search.py --draft-id 42 --e2e
    """
    from backend.app import app
    from backend.everything import get_relevant_content_bm25

    queries = [
        "What are the key terms of the contract?",
        "Summarize the plaintiff's claims",
        "Is there a limitation of liability clause?",
    ]

    with app.app_context():
        test_state = {}  # empty state for testing
        for q in queries:
            print(f"\n{'='*60}")
            print(f"Query: {q}")
            print(f"{'='*60}")
            result = get_relevant_content_bm25(q, draft_id=draft_id, state=test_state, top_k=10)
            if result:
                print(f"Result ({len(result)} chars):")
                print(result[:800])
            else:
                print("No relevant content found.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="BM25 search tests")
    parser.add_argument("--draft-id", type=int, help="Draft ID for DB-backed tests")
    parser.add_argument("--e2e", action="store_true", help="Run end-to-end test with LLM keyword expansion")
    parser.add_argument("--expand", action="store_true", help="Run keyword expansion test (requires OpenAI)")
    parser.add_argument("--unit", action="store_true", help="Run unit tests only (no DB / no OpenAI)")
    args = parser.parse_args()

    if args.unit:
        # Run the pure unit tests
        print("Running unit tests...")
        test_chunk_text_empty()
        test_chunk_text_short()
        test_chunk_text_multiple_paragraphs()
        test_chunk_text_long_paragraph()
        test_chunk_text_unicode()
        test_tokenize()
        test_bm25_empty()
        test_bm25_basic_ranking()
        test_bm25_damages()
        test_bm25_no_match()
        test_bm25_scores_decrease()
        test_format_empty()
        test_format_groups_by_file()
        test_fallback_keywords()
        print("All unit tests passed!")

    elif args.expand:
        manual_test_keyword_expansion()

    elif args.draft_id and args.e2e:
        manual_test_end_to_end(args.draft_id)

    elif args.draft_id:
        manual_test_bm25_with_db(args.draft_id)

    else:
        print("Usage:")
        print("  python tests/test_bm25_search.py --unit                 # pure unit tests")
        print("  python tests/test_bm25_search.py --expand               # test keyword expansion (needs OpenAI)")
        print("  python tests/test_bm25_search.py --draft-id 42          # BM25 search against DB (fallback keywords)")
        print("  python tests/test_bm25_search.py --draft-id 42 --e2e    # full pipeline with LLM expansion")
