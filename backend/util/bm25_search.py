"""
BM25-based full-text search scoring.

Generic BM25 utilities — no database dependencies.
Application-specific chunk retrieval lives in everything.py.

Usage:
    from backend.util.bm25_search import compute_bm25_scores, format_bm25_results

    scored = compute_bm25_scores(chunks, keywords)
    text = format_bm25_results(scored[:10])
"""

import math
import re
from collections import Counter


# ── BM25 parameters ──────────────────────────────────────────────────────────
K1 = 1.5   # term-frequency saturation
B = 0.75   # length normalization


def _tokenize(text: str) -> list[str]:
    """Lowercase and split on non-alphanumeric boundaries."""
    return re.findall(r'[a-z0-9]+', text.lower())


def compute_bm25_scores(
    chunks: list[dict],
    keywords: list[str],
) -> list[dict]:
    """Pure BM25 scoring over in-memory chunk dicts.

    Args:
        chunks: list of {"id", "content", ...} dicts
        keywords: search terms (expanded); case and repetitions are ignored

    Returns:
        Same chunk dicts with an added "bm25_score" key, sorted descending.
    """
    if not chunks or not keywords:
        return []

    # Query repetitions must not inflate document frequency beyond N or add
    # query weight. Preserve first-occurrence order for deterministic scoring.
    keywords_lower = list(dict.fromkeys(kw.lower() for kw in keywords))

    # Pre-tokenize all documents
    doc_tokens = []
    for chunk in chunks:
        tokens = _tokenize(chunk["content"])
        doc_tokens.append(tokens)

    N = len(chunks)
    avg_dl = sum(len(t) for t in doc_tokens) / N if N else 1

    # Document frequency for each keyword
    df = Counter()
    for tokens in doc_tokens:
        token_set = set(tokens)
        for kw in keywords_lower:
            if kw in token_set:
                df[kw] += 1

    # Score each document
    scored = []
    for i, chunk in enumerate(chunks):
        tokens = doc_tokens[i]
        dl = len(tokens)
        tf_counter = Counter(tokens)

        score = 0.0
        for kw in keywords_lower:
            tf = tf_counter.get(kw, 0)
            if tf == 0:
                continue
            idf = math.log((N - df[kw] + 0.5) / (df[kw] + 0.5) + 1.0)
            tf_norm = (tf * (K1 + 1)) / (tf + K1 * (1 - B + B * dl / avg_dl))
            score += idf * tf_norm

        if score > 0:
            result = dict(chunk)
            result["bm25_score"] = score
            scored.append(result)

    scored.sort(key=lambda x: x["bm25_score"], reverse=True)
    return scored


def format_bm25_results(results: list[dict]) -> str:
    """Format BM25 search results into file_extractions_str format.

    Groups chunks by source document, orders documents by their best
    chunk score, and orders chunks within each document by chunk_index.

    Returns a string like:
        ====
        [file name]: contract.pdf
        [chunk 0]: ...text...
        [chunk 3]: ...text...
        ====
        [file name]: memo.docx
        [chunk 1]: ...text...
    """
    if not results:
        return ""

    # Group by source file
    by_file: dict[str, list[dict]] = {}
    best_score: dict[str, float] = {}

    for r in results:
        key = r["source_unique_id"]
        by_file.setdefault(key, []).append(r)
        if r["bm25_score"] > best_score.get(key, -1):
            best_score[key] = r["bm25_score"]

    # Sort files by their best chunk score (highest first)
    sorted_files = sorted(by_file.keys(), key=lambda k: best_score[k], reverse=True)

    parts = []
    for file_key in sorted_files:
        file_chunks = by_file[file_key]
        # Sort chunks within file by chunk_index for reading order
        file_chunks.sort(key=lambda c: c["chunk_index"])

        filename = file_chunks[0]["source_filename"]
        rel_path = file_chunks[0].get("source_relative_path")
        display_name = rel_path if rel_path else filename

        lines = [f"[file name]: {display_name}"]
        for c in file_chunks:
            lines.append(f"[chunk {c['chunk_index']}]:\n{c['content']}")

        parts.append('\n'.join(lines))

    return '\n\n====\n\n'.join(parts)
