"""Deterministic pre-processing applied before any LLM call.

Steps (in order): encoding fix → trim → language detection → user_id pseudonymization
→ deduplication (delegated to DB) → timestamping → threading.

Documented in docs/02_data_pipeline.md.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import random
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Iterable, List, Optional

# ──────────────────────────────────────────────────────────────────────────────
#  Encoding fix
# ──────────────────────────────────────────────────────────────────────────────

_MOJIBAKE_MARKERS = ("Ã", "Â", "â\x80", "�")


def fix_mojibake(s: Optional[str]) -> Optional[str]:
    """Repair the typical cp1252→utf8 round-trip damage seen in the source xlsx.

    Applied conservatively: only if the result still contains valid characters
    AND doesn't introduce more replacement chars than it removes.
    """
    if not isinstance(s, str) or not s:
        return s
    try:
        repaired = s.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    if "�" in repaired and "�" not in s:
        return s
    return repaired


_QUOTE_PAIRS = [('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")]


def normalize_text(s: Optional[str]) -> str:
    """Trim and strip wrapping quotes; preserve case and punctuation."""
    if not isinstance(s, str):
        return ""
    s = fix_mojibake(s) or ""
    s = s.strip()
    for opener, closer in _QUOTE_PAIRS:
        if len(s) >= 2 and s.startswith(opener) and s.endswith(closer):
            s = s[1:-1].strip()
            break
    s = re.sub(r"[ \t]+", " ", s)
    return s


# ──────────────────────────────────────────────────────────────────────────────
#  Language detection (offline, no LLM)
# ──────────────────────────────────────────────────────────────────────────────

_SUPPORTED = {"es": "es", "pt": "pt", "en": "en", "fr": "fr"}


@lru_cache(maxsize=1)
def _detector():
    try:
        from lingua import Language, LanguageDetectorBuilder

        return (
            LanguageDetectorBuilder.from_languages(
                Language.SPANISH,
                Language.PORTUGUESE,
                Language.ENGLISH,
                Language.FRENCH,
            )
            .with_minimum_relative_distance(0.0)
            .build()
        )
    except Exception:
        return None


def detect_language(text: str) -> str:
    """Return ISO 639-1 code or 'other' if undetermined / unsupported."""
    if not text or len(text.strip()) < 3:
        return "other"
    det = _detector()
    if det is None:
        return "other"
    try:
        from lingua import Language

        lang = det.detect_language_of(text)
        if lang is None:
            return "other"
        mapping = {
            Language.SPANISH: "es",
            Language.PORTUGUESE: "pt",
            Language.ENGLISH: "en",
            Language.FRENCH: "fr",
        }
        return mapping.get(lang, "other")
    except Exception:
        return "other"


# ──────────────────────────────────────────────────────────────────────────────
#  Pseudonymization
# ──────────────────────────────────────────────────────────────────────────────


def pseudonymize_user_id(user_id: str) -> str:
    """HMAC-SHA256 hex digest, truncated to 16 chars. Salt comes from .env.

    The LLM never receives this value. It exists only for internal correlation.
    """
    salt = os.getenv("PSEUDONYM_SALT", "default-unsafe-salt-change-me").encode("utf-8")
    if not isinstance(user_id, str):
        user_id = str(user_id)
    return hmac.new(salt, user_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


# ──────────────────────────────────────────────────────────────────────────────
#  Country normalization (pais_usuario → ISO alpha-2)
# ──────────────────────────────────────────────────────────────────────────────

_COUNTRY_TO_ISO = {
    "chile": "CL",
    "méxico": "MX", "mexico": "MX",
    "perú": "PE", "peru": "PE",
    "colombia": "CO",
    "brasil": "BR", "brazil": "BR",
    "argentina": "AR",
    "venezuela": "VE",
    "ecuador": "EC",
    "españa": "ES", "espana": "ES", "spain": "ES",
    "francia": "FR", "france": "FR",
    "tailandia": "TH", "thailand": "TH",
    "reino unido": "GB", "united kingdom": "GB", "uk": "GB",
    "usa": "US", "estados unidos": "US", "united states": "US",
}


def country_to_iso(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    normalized = (fix_mojibake(name) or name).strip().lower()
    return _COUNTRY_TO_ISO.get(normalized)


# ──────────────────────────────────────────────────────────────────────────────
#  Synthetic timestamps for the historical batch
# ──────────────────────────────────────────────────────────────────────────────


def generate_synthetic_timestamps(
    case_message_pairs: Iterable[tuple],
    window_days: Optional[int] = None,
    seed: Optional[int] = None,
) -> dict:
    """Assign synthetic timestamps to a list of (case_id, message_id) pairs.

    Rules (see docs/02_data_pipeline.md):
      - Each case gets a `case_started_at` uniformly distributed in the window
        ending at `now`.
      - Within a case, messages are ordered by message_id (lexicographically).
        Each message after the first adds a delta uniformly in [5min, 12h].
      - Returns a dict {message_id: datetime}.
    """
    if window_days is None:
        window_days = int(os.getenv("SYNTHETIC_TIMESTAMP_WINDOW_DAYS", "14"))
    if seed is None:
        seed = int(os.getenv("SYNTHETIC_TIMESTAMP_SEED", "42"))
    rng = random.Random(seed)

    by_case: dict[str, list[str]] = {}
    for case_id, message_id in case_message_pairs:
        by_case.setdefault(case_id, []).append(message_id)

    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    window_start = now - timedelta(days=window_days)

    timestamps: dict[str, datetime] = {}
    for case_id, message_ids in by_case.items():
        message_ids.sort()
        offset_seconds = rng.randint(0, max(1, int((now - window_start).total_seconds())))
        case_start = window_start + timedelta(seconds=offset_seconds)
        cursor = case_start
        for mid in message_ids:
            timestamps[mid] = cursor
            cursor = cursor + timedelta(seconds=rng.randint(5 * 60, 12 * 3600))
    return timestamps
