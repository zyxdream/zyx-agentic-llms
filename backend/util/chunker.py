import re
import tiktoken

ENC_NAME = 'cl100k_base'
_encoding = tiktoken.get_encoding(ENC_NAME)


def _count_tokens(text: str) -> int:
    return len(_encoding.encode(text))


def _split_paragraph_by_lines(paragraph: str, target_tokens: int) -> list[str]:
    """Split a paragraph by lines, then sentences, then words if needed."""
    lines = paragraph.split('\n')
    if len(lines) > 1:
        return _merge_segments(lines, target_tokens)
    # Single line — split by sentences
    sentences = re.split(r'(?<=[.!?])\s+', paragraph)
    if len(sentences) > 1:
        return _merge_segments(sentences, target_tokens)
    # Single sentence — split by words at token boundary
    return _split_by_words(paragraph, target_tokens)


def _merge_segments(segments: list[str], target_tokens: int) -> list[str]:
    """Merge small segments into chunks up to target_tokens. Segments that
    individually exceed target_tokens are sub-split further."""
    chunks = []
    buf = ""
    buf_tokens = 0
    for seg in segments:
        seg_tokens = _count_tokens(seg)
        if seg_tokens > target_tokens:
            # Flush buffer first
            if buf:
                chunks.append(buf)
                buf = ""
                buf_tokens = 0
            # Recursively sub-split the oversized segment
            sub = _split_paragraph_by_lines(seg, target_tokens) if '\n' in seg or re.search(r'[.!?]\s', seg) else _split_by_words(seg, target_tokens)
            chunks.extend(sub)
            continue
        if buf and buf_tokens + seg_tokens > target_tokens:
            chunks.append(buf)
            buf = seg
            buf_tokens = seg_tokens
        else:
            buf = f"{buf}\n{seg}" if buf else seg
            buf_tokens += seg_tokens
    if buf:
        chunks.append(buf)
    return chunks


def _split_by_words(text: str, target_tokens: int) -> list[str]:
    """Last-resort split: cut at word boundaries to stay under target_tokens."""
    words = text.split()
    chunks = []
    buf_words = []
    buf_tokens = 0
    for word in words:
        word_tokens = _count_tokens(word)
        if buf_words and buf_tokens + word_tokens > target_tokens:
            chunks.append(' '.join(buf_words))
            buf_words = [word]
            buf_tokens = word_tokens
        else:
            buf_words.append(word)
            buf_tokens += word_tokens
    if buf_words:
        chunks.append(' '.join(buf_words))
    return chunks


def chunk_text(text: str, target_tokens: int = 800) -> list[dict]:
    """Split *text* into chunks of approximately *target_tokens* tokens.

    Algorithm priority for split boundaries:
        paragraph (\\n\\n) > line (\\n) > sentence (.!?) > word

    Returns a list of dicts:
        [{"chunk_index": int, "content": str, "token_count": int, "char_count": int}, ...]
    """
    if not text or not text.strip():
        return []

    paragraphs = re.split(r'\n{2,}', text)

    raw_chunks: list[str] = []
    buf = ""
    buf_tokens = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        para_tokens = _count_tokens(para)

        # Single paragraph exceeds target — sub-split it
        if para_tokens > target_tokens:
            if buf:
                raw_chunks.append(buf)
                buf = ""
                buf_tokens = 0
            sub_chunks = _split_paragraph_by_lines(para, target_tokens)
            raw_chunks.extend(sub_chunks)
            continue

        # Adding this paragraph would exceed target — flush buffer
        if buf and buf_tokens + para_tokens > target_tokens:
            raw_chunks.append(buf)
            buf = para
            buf_tokens = para_tokens
        else:
            buf = f"{buf}\n\n{para}" if buf else para
            buf_tokens += para_tokens

    if buf:
        raw_chunks.append(buf)

    # Build result dicts
    result = []
    for i, chunk_text_content in enumerate(raw_chunks):
        content = chunk_text_content.strip()
        if not content:
            continue
        result.append({
            "chunk_index": len(result),
            "content": content,
            "token_count": _count_tokens(content),
            "char_count": len(content),
        })
    return result
