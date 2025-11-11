# moderation.py
# A robust profanity/NSFW filter with phrase/root matching & obfuscation handling.
# No external deps.

import re
import unicodedata
from typing import Iterable

# --- Utility: basic leetspeak mapping (expand as needed)
LEET_MAP = str.maketrans({
    "0": "o",
    "1": "i",
    "2": "z",
    "3": "e",
    "4": "a",
    "5": "s",
    "6": "g",
    "7": "t",
    "8": "b",
    "9": "g",
    "@": "a",
    "$": "s",
    "!": "i",
    "+": "t",
})

# Zero-width and common separator chars to strip for detection (not for display)
ZERO_WIDTH_RE = re.compile(r"[\u200B-\u200F\u202A-\u202E\u2060-\u206F]")
SEPARATORS_RE = re.compile(r"[\W_]+", re.UNICODE)  # any non-word and underscore

def _basic_normalize(s: str) -> str:
    # NFKC normalize, strip zero-width, lower, transliterate some leetspeak
    s = unicodedata.normalize("NFKC", s)
    s = ZERO_WIDTH_RE.sub("", s)
    return s

def _for_detection(s: str) -> str:
    # Lower + leet-fold + remove separators for detection
    s = _basic_normalize(s).lower()
    s = s.translate(LEET_MAP)
    s = SEPARATORS_RE.sub("", s)
    # collapse triple+ repeats: "seeeexxx" -> "seexx"
    s = re.sub(r"(.)\1{2,}", r"\1\1", s)
    return s

# --- Blocklists
# IMPORTANT: This is a starting set. You can (and should) expand using blocklist_extra.txt.
# Categories (keep short examples here; the detection engine handles obfuscations):
BLOCK_WORD_ROOTS: list[str] = [
    # hateful/offensive slurs & root forms (samples; expand as needed)
    "homo", "fag", "dyke", "tranny", "retard", "nazi", "kkk",
    # profanity
    "fuck", "shit", "bitch", "cunt", "asshole", "bastard",
    # sexual
    "sex", "porn", "anal", "cum", "semen", "orgasm", "blowjob", "handjob",
    "cock", "dick", "pussy", "boob", "tit", "clit", "vagina", "penis",
    # violence
    "kill", "rape",
]

# Phrases (checked before roots)
BLOCK_PHRASES: list[str] = [
    "gay sex",
    "anal sex",
    "child porn",
    "incest",
    "kill yourself",
    "gas the",        # fragment to catch common hateful phrase
]

# Optional raw regex patterns (already in detection form). Add rare edge cases here.
BLOCK_REGEX: list[str] = [
    # Example: catch "s u i c i d e" with arbitrary separators already stripped
    r"sui?ici?de",
]

def _load_extra(path: str = "blocklist_extra.txt") -> tuple[list[str], list[str], list[str]]:
    roots, phrases, regexes = [], [], []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                t = line.strip()
                if not t or t.startswith("#"):
                    continue
                # prefixes: root:, phrase:, regex:
                if t.lower().startswith("root:"):
                    roots.append(t[5:].strip())
                elif t.lower().startswith("phrase:"):
                    phrases.append(t[7:].strip())
                elif t.lower().startswith("regex:"):
                    regexes.append(t[6:].strip())
                else:
                    # default to root
                    roots.append(t)
    except FileNotFoundError:
        pass
    return roots, phrases, regexes

# Merge extras if present
_extra_roots, _extra_phrases, _extra_regex = _load_extra()
BLOCK_WORD_ROOTS += _extra_roots
BLOCK_PHRASES += _extra_phrases
BLOCK_REGEX += _extra_regex

# --- Build detection sets for speed
# We keep a normalized "detection" version of each entry without separators & leet folded.
DETECT_ROOTS = { _for_detection(w) for w in BLOCK_WORD_ROOTS }
DETECT_PHRASES = { _for_detection(p) for p in BLOCK_PHRASES }

DETECT_REGEX = [re.compile(p, re.IGNORECASE) for p in BLOCK_REGEX]

def _has_blocked_content(raw: str) -> bool:
    if not raw:
        return False
    det = _for_detection(raw)

    # Regex first (rare patterns)
    for rx in DETECT_REGEX:
        if rx.search(det):
            return True

    # Phrase contains?
    for phrase in DETECT_PHRASES:
        if phrase and phrase in det:
            return True

    # Root contains?
    for root in DETECT_ROOTS:
        if root and root in det:
            return True

    return False

def _mask_offensive_spans(s: str) -> str:
    """
    Mask offensive spans by finding all substrings in the *display* string
    that correspond to blocked content after normalization.
    Strategy:
      - Slide over tokens; if token (normalized) contains a blocked root/phrase, mask that token fully.
    """
    if not s:
        return "Anonymous"

    # Tokenize on whitespace but keep punctuation around tokens
    tokens = re.split(r"(\s+)", s)
    out = []
    for tok in tokens:
        if tok.isspace():
            out.append(tok)
            continue
        norm = _for_detection(tok)
        flagged = False
        # Regex
        if any(rx.search(norm) for rx in DETECT_REGEX):
            flagged = True
        # Phrases/roots (since token-level, we check roots only; phrases are often multi-token)
        elif any(root in norm for root in DETECT_ROOTS):
            flagged = True
        # If token contains separators (e.g. “g*a*y”), checking phrases on full string is better,
        # but we’ll still catch the token-level roots.

        if flagged:
            out.append("*" * len(tok))
        else:
            out.append(tok)
    masked = "".join(out)

    # If multi-word phrase exists across tokens, mask whole string
    if any(ph in _for_detection(masked) for ph in DETECT_PHRASES):
        # Mask only alphabetic chars to keep spacing/punct readable
        masked = re.sub(r"[A-Za-z0-9]", "*", masked)
    return masked

def sanitize_name(raw: str, max_len: int = 40) -> str:
    """
    - Trim + cut length
    - Quick validation: if blocked content present -> mask
    - Remove outrageous control/ZW chars for display
    - Collapse spaces
    - Return 'Anonymous' if result is basically all stars/empty
    """
    if not raw:
        return "Anonymous"

    # Hard trim & normalize for display (keep user-visible characters)
    disp = _basic_normalize(raw).strip()[:max_len]

    # If contains blocked content, mask offensive parts
    if _has_blocked_content(disp):
        disp = _mask_offensive_spans(disp)

    # Collapse repeat whitespace
    disp = re.sub(r"\s{2,}", " ", disp).strip()

    # If the visible characters are basically gone, fallback
    visible = re.sub(r"[\s\*]", "", disp)
    if len(visible) < 2:
        return "Anonymous"

    return disp or "Anonymous"
