"""Defensive guards applied around the LLM call.

These guards are CHEAP heuristics that catch two real-world failure modes
observed during integration tests:

1. **Placeholder/test inputs**: Postman/Swagger UI defaults send `"string"` as
   the literal value for `text`. The LLM, asked to fit any input into the
   schema, dutifully invents a `weak_point` (topic="transfers" was a real
   case). That's a hallucination on a test payload — it pollutes audits and
   wastes tokens. The placeholder guard intercepts those calls BEFORE we hit
   the LLM and returns a neutral `Analysis` with `confidence=0`.

2. **Resolved threads (memory effect)**: when a thread starts with a problem
   and ends with the customer expressing gratitude or saying it was solved,
   the LLM sometimes still escalates "because there was an earlier problem".
   In production this means routing already-closed cases to a human queue,
   wasting agent time. The resolved-thread guard lets the triage layer
   override the LLM and force `escalated=false` / `priority=p4` when the
   LATEST inbound clearly indicates resolution.

Both guards are conservative: they trigger only on STRONG signals and they
NEVER override a hard regulatory / safety / legal-action escalation, even if
the customer is being polite about it.
"""

from __future__ import annotations

import re
from typing import Optional

from src.core.schema import (
    Analysis,
    Escalation,
    RegulatoryFlag,
    Sentiment,
    UrgencySignals,
    WeakPoint,  # noqa: F401  (re-export friendliness)
)


# ─────────────────────────────────────────────────────────────────────────────
#  Placeholder / test-input detection
# ─────────────────────────────────────────────────────────────────────────────

_PLACEHOLDER_TOKENS = {
    "string", "strings",
    "test", "testing", "tests",
    "lorem", "ipsum",
    "asdf", "asdfg", "qwerty", "abcd", "abcde",
    "hello world", "helloworld",
    "ping", "pong",
    "xxx", "xxxx",
    "todo", "tbd",
    "n/a", "na", "null", "none",
    "sample", "demo",
}


def _has_only_ascii_punct(s: str) -> bool:
    """True if `s` contains only ASCII punctuation/whitespace (no letters, digits, emojis)."""
    return bool(s) and all(ord(c) < 128 and not c.isalnum() for c in s)


def _has_meaningful_content(s: str) -> bool:
    """True if `s` contains at least one letter, digit, or non-ASCII char (emoji counts)."""
    return any(c.isalnum() or ord(c) >= 128 for c in s)


def is_placeholder_text(text: Optional[str]) -> bool:
    """Return True if `text` looks like a placeholder/test value, NOT a real customer message.

    Conservative on purpose: short legitimate complaints like "no funciona" or
    "ayuda urgente" must NOT be flagged. Pure emoji content ("😡😡😡") is also
    legitimate (emotional signal) and is NOT flagged.
    """
    if text is None:
        return True
    t = text.strip().lower()
    if not t:
        return True

    if len(t) < 3:
        return True

    if t in _PLACEHOLDER_TOKENS:
        return True

    words = t.split()
    if words and all(w in _PLACEHOLDER_TOKENS for w in words):
        return True

    no_space = re.sub(r"\s+", "", t)
    # Repeated ASCII single char ("aaaa", "----") is a placeholder.
    # Repeated emoji is NOT (emotional expression).
    if (
        no_space
        and len(set(no_space)) == 1
        and len(no_space) >= 3
        and all(ord(c) < 128 for c in no_space)
    ):
        return True

    if _has_only_ascii_punct(t):
        return True

    if not _has_meaningful_content(t):
        return True

    return False


def build_placeholder_analysis(language: Optional[str]) -> Analysis:
    """Neutral `Analysis` for placeholder inputs. Returned WITHOUT a LLM call."""
    safe_lang = language if language in ("es", "pt", "en", "fr") else "other"
    return Analysis(
        language=safe_lang,  # type: ignore[arg-type]
        sentiment=Sentiment(score=0.0, label="neutral"),
        primary_emotion="neutral",
        weak_points=[],
        regulatory_flags=[],
        urgency_signals=UrgencySignals(),
        escalation=Escalation(
            needed=False,
            priority="p4",
            reason="Mensaje sin contenido significativo (placeholder/test detectado por input guard).",
            suggested_human_team="tier1_support",
        ),
        confidence=0.0,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Resolved-thread detection (memory-effect override)
# ─────────────────────────────────────────────────────────────────────────────

_RESOLVED_EMOTIONS = {"gratitude", "satisfaction"}
_RESOLVED_SENTIMENT_FLOOR = 0.2


def is_resolved_thread(analysis: Analysis) -> bool:
    """Return True if the LATEST inbound clearly expresses resolution/gratitude.

    Hard regulatory or safety signals ALWAYS win over the override (a polite
    customer who just got their account hacked still needs to be escalated).
    """
    if analysis.primary_emotion not in _RESOLVED_EMOTIONS:
        return False
    if float(analysis.sentiment.score or 0.0) < _RESOLVED_SENTIMENT_FLOOR:
        return False

    if analysis.urgency_signals.human_safety_risk:
        return False
    if analysis.urgency_signals.threat_of_legal_action:
        return False
    if analysis.urgency_signals.money_blocked:
        return False
    if analysis.regulatory_flags:
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
#  Priority utilities (shared across triage + orchestrator)
# ─────────────────────────────────────────────────────────────────────────────

_PRIORITY_RANK = {"p1": 0, "p2": 1, "p3": 2, "p4": 3}


def priority_rank(p: Optional[str]) -> int:
    return _PRIORITY_RANK.get((p or "p4").lower(), 99)


def max_priority(*priorities: Optional[str]) -> str:
    """Return the highest-urgency priority from the inputs.

    Convention: p1 > p2 > p3 > p4. Unknown values are treated as the lowest.
    """
    valid = [p for p in priorities if p]
    if not valid:
        return "p4"
    return min(valid, key=priority_rank)
