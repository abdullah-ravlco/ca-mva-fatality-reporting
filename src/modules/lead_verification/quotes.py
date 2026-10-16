"""Check that a quote the model gave really appears in the page it cites."""

import re
import unicodedata
from difflib import SequenceMatcher

MIN_FUZZY_QUOTE_CHARS = 25
FUZZY_MATCH_RATIO = 0.9


def _normalize(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def quote_in_text(quote, text):
    """True when the quote (ignoring case, punctuation and spacing) is in the text."""
    q, t = _normalize(quote), _normalize(text)
    if len(q) < 3:
        return False
    if q in t:
        return True
    if len(q) < MIN_FUZZY_QUOTE_CHARS:
        return False
    match = SequenceMatcher(None, q, t, autojunk=False).find_longest_match(0, len(q), 0, len(t))
    return match.size >= FUZZY_MATCH_RATIO * len(q)
