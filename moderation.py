import re

# Very simple word filter. Extend as needed, or swap this whole module for a
# real moderation service later.
BAD_WORDS = [
    "fuck", "shit", "bitch", "cunt", "nigger", "nigga", "fag", "faggot",
    "slut", "whore", "hitler", "nazi", "kkk", "retard",
]

# Common leetspeak / lookalike substitutions, applied only for matching
# purposes so tricks like "n1gg3r" or "f.u.c.k" still get caught even though
# the displayed name keeps its original characters.
_LEET_MAP = str.maketrans({
    "0": "o", "1": "i", "!": "i", "3": "e", "4": "a", "@": "a",
    "5": "s", "$": "s", "7": "t", "+": "t",
})

def _normalize_for_matching(s: str) -> str:
    s = s.lower().translate(_LEET_MAP)
    # Strip everything but letters so spacing/punctuation dodges
    # ("f u c k", "f_u_c_k") don't slip past a plain substring check.
    return re.sub(r"[^a-z]", "", s)

def sanitize_name(raw: str, max_len: int = 40) -> str:
    if not raw:
        return "Anonymous"

    s = raw.strip()[:max_len]
    # Allow letters, numbers, spaces, and a few punctuation marks; strip others
    s = re.sub(r"[^A-Za-z0-9\s\-\_\.!?,&'’]", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    if not s:
        return "Anonymous"

    normalized = _normalize_for_matching(s)
    for w in BAD_WORDS:
        if w in normalized:
            # Once leetspeak/spacing normalization is in play we can no
            # longer map a match back to an exact slice of the original
            # string to asterisk out, so we reject the whole name rather
            # than risk a partially-censored slur getting through.
            return "Anonymous"

    return s
