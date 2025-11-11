import re

# Keep the list small & obvious. You can expand later.
# We block common slurs and explicit sexual terms/phrases.
BAD_WORDS = [
    # slurs / hateful terms (sample; expand as you wish)
    "homo", "nazi", "kkk",
    # profanity
    "fuck", "shit", "bitch", "cunt",
    # sexual/explicit
    "sex", "porn", "anal", "dick", "cock", "pussy", "boobs", "tits", "cum",
    "asshole", "blowjob", "handjob", "semen", "orgasm",
]

# Specific multi-word phrases to catch before single words
BAD_PHRASES = [
    r"gay\s*sex",
]

ALLOWED_CHARS = re.compile(r"[^A-Za-z0-9\s\-\_\.!?,&'’]")

def _star(match: re.Match) -> str:
    return "*" * len(match.group(0))

def sanitize_name(raw: str, max_len: int = 40) -> str:
    """Return a cleaned/censored display name.
    - trims, length-limits
    - removes disallowed chars
    - censors banned phrases first, then banned words (case-insensitive)
    - collapses extra whitespace
    """
    if not raw:
        return "Anonymous"

    s = raw.strip()[:max_len]

    # Remove disallowed characters
    s = ALLOWED_CHARS.sub("", s)

    # Censor phrases first (e.g., 'gay sex')
    for pat in BAD_PHRASES:
        s = re.sub(pat, _star, s, flags=re.IGNORECASE)

    # Then censor individual words using word boundaries where appropriate
    for w in BAD_WORDS:
        # Use word boundaries for alphabetic words to avoid partial hits in other words
        if re.fullmatch(r"[A-Za-z]+", w):
            pattern = r"\b" + re.escape(w) + r"\b"
        else:
            pattern = re.escape(w)
        s = re.sub(pattern, _star, s, flags=re.IGNORECASE)

    # Collapse spaces
    s = re.sub(r"\s{2,}", " ", s).strip()

    # Fallback if everything got starred or too short
    visible = re.sub(r"[\s\*]", "", s)
    if len(visible) < 2:
        return "Anonymous"

    return s or "Anonymous"
