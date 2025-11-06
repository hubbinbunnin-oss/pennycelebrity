import re

# Very simple word filter. Extend as needed.
BAD_WORDS = [
    # keep this list short and obvious; you can expand later or swap to a service
    "fuck", "shit", "bitch", "cunt", "nigger", "fag", "slut", "whore",
    "hitler", "nazi", "kkk"
]

def sanitize_name(raw: str, max_len: int = 40) -> str:
    if not raw:
        return "Anonymous"
    s = raw.strip()
    # Limit length
    s = s[:max_len]
    # Allow letters, numbers, spaces, and a few punctuation marks; strip others
    s = re.sub(r"[^A-Za-z0-9\s\-\_\.!?,&'’]", "", s)

    # Replace bad words (case-insensitive) with asterisks
    def repl(m):
        return "*" * len(m.group(0))
    for w in BAD_WORDS:
        s = re.sub(re.escape(w), repl, s, flags=re.IGNORECASE)
    # Collapse repeated spaces
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s if s else "Anonymous"
