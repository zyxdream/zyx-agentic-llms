"""Sanitize text for Word document XML compatibility."""


def sanitize_word_text(text: str) -> str:
    if not text:
        return text
    text = text.replace("\x00", "")
    sanitized = []
    for char in text:
        code = ord(char)
        if code >= 32 or code in (9, 10, 13):
            sanitized.append(char)
    return "".join(sanitized)
