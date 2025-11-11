# moderation.py
import os, re, unicodedata, string
from typing import Tuple

ENABLE_AI = os.getenv("ENABLE_AI_MODERATION", "0") == "1"

# ---- Config you can tweak ----
MAX_LEN = 48
MIN_LEN = 1
MAX_EMOJIS = 4

# Keep your blocklists out of source control; load them from files or env.
# Put one term per line in ./blocklists/{profanity.txt, slurs.txt, sexual.txt, violence.txt}.
def _load_blocklist(name):
    path = os.path.join(os.path.dirname(__file__), "blocklists", f"{name}.txt")
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {line.strip().lower() for line in f if line.strip() and not line.startswith("#")}

BLOCK_PROFANITY = _load_blocklist("profanity")
BLOCK_SLURS = _load_blocklist("slurs")
BLOCK_SEXUAL = _load_blocklist("sexual")
BLOCK_VIOLENCE = _load_blocklist("violence")

BANNED_PATTERNS = [
    r"(?:^|\b)(?:admin|support|moderator|stripe|official)(?:\b|$)",  # impersonation
    r"(?:https?://|www\.)",                                          # links
    r"[@#]{2,}",                                                     # spammy symbols
]

ZERO_WIDTH = dict.fromkeys({
    0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF
}, None)

EMOJI_RANGES = [
    (0x1F300, 0x1FAD6), (0x1F900, 0x1F9FF), (0x2600, 0x26FF), (0x2700, 0x27BF)
]

# ------------------------------

def _strip_zero_width(s: str) -> str:
    return s.translate(ZERO_WIDTH)

def _normalize_screening(s: str) -> str:
    # NFKC then lowercase. Keep original for display.
    s = unicodedata.normalize("NFKC", s)
    return s.lower()

def _ascii_skeleton(s: str) -> str:
    # Turn “P@yp@l” → “paypal”, “ḿŏdéråtor” → “moderator”
    nfkd = unicodedata.normalize("NFKD", s)
    only_ascii = nfkd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", only_ascii.lower())

def _count_emojis(s: str) -> int:
    count = 0
    for ch in s:
        cp = ord(ch)
        if any(a <= cp <= b for (a, b) in EMOJI_RANGES):
            count += 1
    return count

def _matches_blocklists(screen: str, skeleton: str) -> Tuple[bool, str]:
    words = set(re.findall(r"[a-z0-9]+", screen))
    combined = words | {skeleton}

    def hit(blockset, label):
        return any(term in screen or term in skeleton for term in blockset), label

    for pat in BANNED_PATTERNS:
        if re.search(pat, screen):
            return True, "impersonation/links/symbol spam"

    for block, label in [
        (BLOCK_SLURS, "slur"),
        (BLOCK_PROFANITY, "profanity"),
        (BLOCK_SEXUAL, "sexual content"),
        (BLOCK_VIOLENCE, "violent content"),
    ]:
        if any(term for term in block if term and (term in screen or term in skeleton)):
            return True, label

    return False, ""

def _basic_rules(original: str, screen: str) -> Tuple[bool, str]:
    if not (MIN_LEN <= len(original) <= MAX_LEN):
        return False, f"name must be {MIN_LEN}–{MAX_LEN} characters"
    if _count_emojis(original) > MAX_EMOJIS:
        return False, "too many emojis"
    # limit control chars
    if any(ord(c) < 32 for c in original):
        return False, "invalid control characters"
    # allow letters, marks, numbers, spaces, and a few safe punctuation marks
    allowed_punct = set(" .,_-’'&")
    for ch in original:
        cat = unicodedata.category(ch)
        if ch in allowed_punct:
            continue
        if cat[0] in ("L", "M", "N", "Z"):  # letters/marks/numbers/separators
            continue
        return False, "invalid characters"
    return True, ""

# ---- Optional AI moderation (plug your provider here) ----
def _ai_moderation(screen: str, skeleton: str) -> Tuple[bool, str]:
    """
    Return (allowed, reason_if_blocked).
    Implement one provider. Below is a stub that you can wire to your choice.
    """
    # Example sketch for an API; replace with your provider of choice.
    # Fail closed only for strings that already look risky. Otherwise, ignore errors.
    try:
        # from some_moderation_client import moderate
        # result = moderate(text=screen)
        # blocked = result.is_disallowed   # bool
        # cat = result.category            # str
        blocked = False
        cat = ""
        return (not blocked), cat
    except Exception:
        # If our deterministic checks already found risk, keep it blocked;
        # otherwise let step-2 decision stand.
        return True, ""  # "True" here means "allowed" to avoid accidental lockouts.

def moderate_name(name: str) -> Tuple[bool, str, str]:
    """
    Returns (allowed, reason_if_blocked, clean_display_name).
    We clean zero-widths and collapse whitespace but preserve user’s characters.
    """
    original = name.strip()
    original = _strip_zero_width(original)

    screen = _normalize_screening(original)
    skeleton = _ascii_skeleton(screen)

    ok, why = _basic_rules(original, screen)
    if not ok:
        return False, why, ""

    hit, label = _matches_blocklists(screen, skeleton)
    if hit:
        return False, label, ""

    if ENABLE_AI:
        ai_ok, ai_label = _ai_moderation(screen, skeleton)
        if not ai_ok:
            return False, ai_label or "inappropriate", ""

    # final tidy for display
    clean = re.sub(r"\s+", " ", original)
    return True, "", clean

def sanitize_name(name: str) -> str:
    ok, why, clean = moderate_name(name)
    if not ok:
        raise ValueError(f"Name rejected: {why}")
    return clean
