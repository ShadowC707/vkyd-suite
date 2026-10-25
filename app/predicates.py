"""Shared canonical text and message predicates."""

import re

# Consistently strips ASCII whitespace, Unicode whitespace, and zero-width formatting characters:
# - ASCII whitespace: space, tab, newline, carriage return, vertical tab (\x0b), form feed (\x0c)
# - Unicode whitespace: \u00a0 (no-break space), \u1680, \u2000-\u200a, \u2028, \u2029,
#   \u202f, \u205f, \u3000, etc. (covered by \s in Python re)
# - Zero-width formatting characters: \u200b (zero-width space), \u200c (zero-width non-joiner),
#   \u200d (zero-width joiner), \ufeff (BOM / zero-width no-break space)
WHITESPACE_AND_ZERO_WIDTH_RE = re.compile(r"[\s\u200b\u200c\u200d\ufeff]+")


def is_supported_text_content(text: str | None) -> bool:
    """
    Determine whether text contains non-whitespace content.
    Consistently strips ASCII whitespace, Unicode whitespace, and zero-width formatting characters.
    """
    if not text:
        return False
    cleaned = WHITESPACE_AND_ZERO_WIDTH_RE.sub("", text)
    return len(cleaned) > 0


def is_supported_message(kind: str | None, text: str | None) -> bool:
    """
    Canonical predicate determining whether a stored message row is supported text.
    - Service records are never supported text (even if they have text).
    - Other kinds ('message', 'sticker', 'media') are supported text if and only if
      they contain non-whitespace text under is_supported_text_content.
    """
    if kind == "service":
        return False
    return is_supported_text_content(text)
