"""
Expand a user's query into search keywords using an LLM.

The LLM suggests variations, synonyms, and removes stop words so that the
resulting keyword list is suitable for BM25 lexical search over document chunks.
"""

from pydantic import BaseModel
from celery.exceptions import SoftTimeLimitExceeded

from backend.ai import base


class KeywordExpansion(BaseModel):
    keywords: list[str]


KEYWORD_EXPANSION_PROMPT = """You are a legal search assistant. Given the user's query, produce a JSON object with a single key "keywords" whose value is a flat list of search keywords.

Rules:
1. Remove stop words (the, is, a, an, of, for, in, on, to, and, or, etc.)
2. Keep all meaningful nouns, verbs, legal terms, names, dates, and numbers from the query.
3. Add 3-5 keyword variations: synonyms, related legal terms, alternate phrasings.
   For example: "breach of contract" → also add "contractual breach", "contract violation".
4. If the query mentions a legal concept, include the formal and informal term.
5. Keep each keyword 1-3 words. No full sentences.
6. Return 5-15 keywords total.
7. All keywords should be lowercase.

Return ONLY valid JSON, no markdown fences, no explanation.

Example:
Query: "What are the damages for breach of contract?"
Output: {{"keywords": ["damages", "breach", "contract", "contractual breach", "contract violation", "remedy", "compensatory damages", "consequential damages", "monetary relief"]}}

User query: {query}"""


def expand_search_keywords(
    query: str,
    model: str | None = None,
    token_counter=None,
    *,
    reasoning_effort: str | None = None,
) -> list[str]:
    """Call the LLM to expand a user query into BM25 search keywords.

    Args:
        query: the user's natural-language question or request
        model: OpenAI model to use; defaults to the shared base model
        reasoning_effort: optional reasoning effort override

    Returns:
        List of keyword strings. Falls back to naive word splitting if
        the LLM call fails.
    """
    if not query or not query.strip():
        return []

    prompt = KEYWORD_EXPANSION_PROMPT.format(query=query)
    selected_model = base.DEFAULT_MODEL if model is None else model

    try:
        parsed = base.chat_completion_parse(
            prompt,
            KeywordExpansion,
            model=selected_model,
            reasoning_effort=reasoning_effort,
            token_counter=token_counter,
        )
        if parsed is None:
            fallback = _fallback_keywords(query)
            print(f"[keyword_expander] LLM returned empty, fallback keywords: {fallback}")
            return fallback
        if parsed.keywords:
            result = [keyword.lower().strip() for keyword in parsed.keywords if keyword.strip()]
            if not result:
                fallback = _fallback_keywords(query)
                print(f"[keyword_expander] LLM returned empty, fallback keywords: {fallback}")
                return fallback
            print(f"[keyword_expander] Expanded keywords ({len(result)}): {result}")
            return result
    except SoftTimeLimitExceeded:
        raise
    except Exception as e:
        print(f"WARNING: keyword expansion failed: {e}")

    fallback = _fallback_keywords(query)
    print(f"[keyword_expander] Using fallback keywords: {fallback}")
    return fallback


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


def _fallback_keywords(query: str) -> list[str]:
    """Naive fallback: split query, remove stop words."""
    words = query.lower().split()
    return [w.strip("?.,!;:\"'()") for w in words if w.strip("?.,!;:\"'()") not in _STOP_WORDS and len(w) > 1]
