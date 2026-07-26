"""Release language detection.

Torrent release names rarely carry a clean language field, so we infer it from
the title using a layered approach:

  1. explicit indexer language field, if the search result provides one
  2. script detection — Cyrillic / CJK / Arabic / etc. in the title strongly
     implies that language (e.g. 'Американский Папаша' -> ru)
  3. language tags scene groups use — 'MULTI', 'VOSTFR', 'GERMAN', 'ITA',
     'SUBBED', 'DUBBED', dotted/spaced 'FR'/'ES'/'DE' tokens

Returns ISO-639-1 codes ('en', 'ru', 'fr', ...). Defaults to 'en' only when the
title looks like a normal Latin-script English release with no foreign markers,
otherwise 'und' (undetermined) so callers can decide how strict to be.
"""
from __future__ import annotations

import re
import unicodedata

# tag/word -> ISO code. Word-boundary matched, case-insensitive.
_TAG_LANG = {
    "french": "fr", "vostfr": "fr", "truefrench": "fr", "vff": "fr", "vf": "fr",
    "german": "de", "deutsch": "de", "ger": "de",
    "italian": "it", "ita": "it",
    "spanish": "es", "espanol": "es", "castellano": "es", "latino": "es",
    "russian": "ru", "rus": "ru",
    "japanese": "ja", "jpn": "ja",
    "korean": "ko", "kor": "ko",
    "chinese": "zh", "mandarin": "zh", "cantonese": "zh",
    "portuguese": "pt", "dublado": "pt",
    "hindi": "hi", "tamil": "ta", "telugu": "te",
    "polish": "pl", "lektor": "pl",
    "dutch": "nl", "nordic": "sv", "swedish": "sv",
    "english": "en", "eng": "en",
}

# Short dotted/spaced tokens that imply a language (lower confidence, so only
# when standing alone as a token, e.g. 'Show.S01E01.FR.1080p').
_SHORT_TAG = {"fr": "fr", "de": "de", "it": "it", "es": "es", "ru": "ru",
              "ja": "ja", "ko": "ko", "zh": "zh", "pt": "pt", "nl": "nl"}

# Unicode script ranges -> language guess.
_SCRIPT_RANGES = [
    ("ru", (0x0400, 0x04FF)),   # Cyrillic
    ("ja", (0x3040, 0x30FF)),   # Hiragana/Katakana
    ("zh", (0x4E00, 0x9FFF)),   # CJK unified (also ja kanji; ja caught above first)
    ("ko", (0xAC00, 0xD7A3)),   # Hangul
    ("ar", (0x0600, 0x06FF)),   # Arabic
    ("he", (0x0590, 0x05FF)),   # Hebrew
    ("hi", (0x0900, 0x097F)),   # Devanagari
    ("el", (0x0370, 0x03FF)),   # Greek
]


def _script_lang(title: str) -> str | None:
    for ch in title:
        cp = ord(ch)
        for lang, (lo, hi) in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                return lang
    return None


def detect(result: dict) -> str:
    """Best-guess ISO-639-1 language for a search result dict. Uses an explicit
    'language' field if the indexer set one, else infers from the title."""
    explicit = (result.get("language") or "").strip().lower()
    if explicit:
        # normalize a few common full names
        return _TAG_LANG.get(explicit, explicit[:2] if explicit else "und")

    title = result.get("title") or ""
    # 1. non-Latin script is the strongest signal
    sl = _script_lang(title)
    if sl:
        return sl
    # 2. explicit language words/tags. Collect ALL hits first: a dual-audio
    # release tagged 'ITA ENG' contains an English track, so returning the
    # first foreign tag found (old behavior) wrongly filtered it from English
    # profiles. 'ENG SUBS' next to a foreign tag still means foreign audio.
    tokens = re.split(r"[^A-Za-z]+", title.lower())
    tokenset = set(tokens)
    hits = {lang for word, lang in _TAG_LANG.items() if word in tokenset}
    if hits:
        has_subs = tokenset & {"subs", "sub", "subbed", "subtitles", "vostfr"}
        if "en" in hits and (len(hits) == 1 or not has_subs):
            return "en"
        foreign = sorted(hits - {"en"})
        if foreign:
            return foreign[0]
    # 'MULTI' means multiple audio tracks incl. (usually) English — treat as en
    if "multi" in tokenset:
        return "en"
    # 3. short standalone tokens — LOW confidence, and a huge false-positive
    # trap: 'It', 'Es', 'De' appear as ordinary words/titles ("It (2017)" was
    # being detected as Italian and filtered out of English profiles). Only
    # count a short tag when it appears UPPERCASE in the original title and
    # isn't at the very start (scene tags sit after the episode/quality part,
    # titles sit at the front).
    raw_tokens = re.split(r"[^A-Za-z]+", title)
    for i, rt in enumerate(raw_tokens):
        low = rt.lower()
        if low in _SHORT_TAG and rt.isupper() and i >= 2:
            return _SHORT_TAG[low]
    # 4. plain Latin-script release with no foreign markers -> assume English
    if title and all(ord(c) < 0x250 for c in title):
        return "en"
    return "und"


def matches(result: dict, preferred: str) -> bool:
    """True if the release is in (or compatible with) the preferred language.
    'und' (undetermined) is treated as acceptable so we don't drop releases we
    simply couldn't classify."""
    if not preferred or preferred == "any":
        return True
    lang = detect(result)
    return lang == preferred or lang == "und"
