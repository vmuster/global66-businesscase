"""End-to-end orchestration of a single message: pipeline → DB → engine → triage → DB.

Used by both the FastAPI webhook (real-time) and the batch script. By centralizing
this flow, both entry points share identical semantics.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.core import pipeline
from src.core.engine import LLMClient, analyze_thread_safely
from src.core.guards import build_placeholder_analysis, is_placeholder_text
from src.core.schema import Analysis, ThreadInput, ThreadMessage, TokenUsage, WebhookPayload
from src.core.triage import (
    build_escalation_payload,
    compute_score,
    must_escalate,
)
from src.database import db

logger = logging.getLogger(__name__)

ESCALATIONS_FILE = Path("data/escalations.jsonl")


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc).replace(microsecond=0)


def ingest_message(
    payload: WebhookPayload,
    *,
    synthetic_ts: bool = False,
    forced_created_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """Run the deterministic pipeline and persist case+message.

    Returns metadata used by the analysis stage (already_exists flag, normalized
    text, language, country_iso, created_at).
    """
    text = pipeline.normalize_text(payload.text)
    language = pipeline.detect_language(text)
    country_iso = pipeline.country_to_iso(payload.pais_usuario)
    pseudonym = pipeline.pseudonymize_user_id(payload.user_id)

    created_at = forced_created_at or _parse_iso(payload.timestamp) or _now_utc()
    if forced_created_at is None and payload.timestamp is None:
        logger.warning(
            "Webhook payload without timestamp for message_id=%s; using server time.",
            payload.message_id,
        )

    db.upsert_case(
        case_id=payload.case_id,
        pais_usuario=payload.pais_usuario,
        country_iso=country_iso,
        language=language,
        case_started_at=created_at,
        synthetic_ts=synthetic_ts,
    )
    inserted = db.insert_message(
        message_id=payload.message_id,
        case_id=payload.case_id,
        user_pseudonym=pseudonym,
        direction=payload.direction,
        text=text,
        language=language,
        platform=payload.platform or "unknown",
        created_at=created_at,
        synthetic_ts=synthetic_ts,
    )

    return {
        "inserted": inserted,
        "already_exists": not inserted,
        "text": text,
        "language": language,
        "country_iso": country_iso,
        "user_pseudonym": pseudonym,
        "created_at": created_at,
    }


async def analyze_and_persist(
    *,
    client: LLMClient,
    case_id: str,
    message_id: str,
    user_pseudonym: Optional[str],
    country_iso: Optional[str],
    detected_language: Optional[str],
) -> dict[str, Any]:
    """Run the LLM analysis on the case thread, score it, persist and (optionally) escalate.

    Returns a dict with `analysis`, `score`, `escalated` (bool) and `payload` for
    the webhook response.
    """
    thread_rows = db.get_thread(case_id)
    if not thread_rows:
        raise RuntimeError(f"Thread for case_id={case_id} is empty; cannot analyze.")

    thread = ThreadInput(
        case_id=case_id,
        country_hint=country_iso,
        detected_language=detected_language,
        messages=[
            ThreadMessage(direction=row["direction"], text=row["text"]) for row in thread_rows
        ],
    )

    # Input guard: short-circuit placeholder/test inputs WITHOUT a LLM call.
    # The latest INBOUND text is what the model would weight most heavily, so
    # we look at it to decide whether the request is a real customer message
    # or a synthetic test (Postman/Swagger default `"string"`, `"test"`, etc).
    last_inbound_text = next(
        (row["text"] for row in reversed(thread_rows) if row["direction"] == "INBOUND"),
        None,
    )
    if is_placeholder_text(last_inbound_text):
        logger.info(
            "Input guard: placeholder text detected for message_id=%s, returning neutral "
            "analysis without a LLM call.",
            message_id,
        )
        analysis = build_placeholder_analysis(language=detected_language)
        usage = TokenUsage(
            tokens_in=0,
            tokens_out=0,
            model="input_guard",
            provider="input_guard",
            latency_ms=0,
        )
        status = "ok"
    else:
        analysis, usage, status = await analyze_thread_safely(client, thread)

    if status != "ok" or analysis is None:
        db.upsert_analysis(
            message_id=message_id,
            sentiment_score=None,
            sentiment_label=None,
            primary_emotion=None,
            weak_points=None,
            regulatory_flags=None,
            urgency_signals=None,
            escalation=None,
            confidence=None,
            score_base=None,
            score_final=None,
            priority="p3",
            analysis_status=status,
            model_used=usage.model,
            provider=usage.provider,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            latency_ms=usage.latency_ms,
        )
        fallback_payload = {
            "case_id": case_id,
            "message_id": message_id,
            "priority": "p3",
            "suggested_team": "tier1_support",
            "reason": f"Automated analysis failed ({status}); manual review required.",
            "score_final": None,
        }
        db.insert_escalation(
            message_id=message_id,
            case_id=case_id,
            priority="p3",
            reason=fallback_payload["reason"],
            suggested_team="tier1_support",
            payload=fallback_payload,
        )
        _append_escalation_jsonl(fallback_payload)
        return {
            "status": status,
            "analysis": None,
            "score": None,
            "escalated": True,
            "payload": fallback_payload,
            "tokens": {"in": usage.tokens_in, "out": usage.tokens_out},
            "model_used": usage.model,
            "latency_ms": usage.latency_ms,
        }

    first_inbound = db.get_case_first_inbound_at(case_id)
    score = compute_score(analysis, first_inbound_at=first_inbound, now=_now_utc())

    db.upsert_analysis(
        message_id=message_id,
        sentiment_score=analysis.sentiment.score,
        sentiment_label=analysis.sentiment.label,
        primary_emotion=analysis.primary_emotion,
        weak_points=[wp.model_dump() for wp in analysis.weak_points],
        regulatory_flags=[rf.model_dump() for rf in analysis.regulatory_flags],
        urgency_signals=analysis.urgency_signals.model_dump(),
        escalation=analysis.escalation.model_dump(),
        confidence=analysis.confidence,
        score_base=score["score_base"],
        score_final=score["score_final"],
        priority=score["priority_final"],
        analysis_status="ok",
        model_used=usage.model,
        provider=usage.provider,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        latency_ms=usage.latency_ms,
    )

    priority_breakdown = {
        "math": score["priority_math"],
        "llm": score["priority_llm"],
        "final": score["priority_final"],
        "policy": score.get("priority_policy"),
        "severe_risk_signals": score.get("severe_risk_signals", []),
        "resolved_override": score.get("resolved_override", False),
        "rule": (
            "default trust_llm; math overrides only when LLM lowered (p3/p4) "
            "AND severe risk present; resolved-thread override -> p4"
        ),
    }

    escalated_payload: Optional[dict[str, Any]] = None
    if must_escalate(analysis, score_final=score["score_final"]):
        thread_excerpt = [
            {"direction": row["direction"], "text": row["text"]} for row in thread_rows
        ]
        escalated_payload = build_escalation_payload(
            case_id=case_id,
            message_id=message_id,
            user_pseudonym=user_pseudonym,
            country_iso=country_iso,
            language=analysis.language,
            analysis=analysis,
            score_final=score["score_final"],
            priority=score["priority_final"],
            thread_excerpt=thread_excerpt,
            priority_breakdown=priority_breakdown,
        )
        db.insert_escalation(
            message_id=message_id,
            case_id=case_id,
            priority=score["priority_final"],
            reason=analysis.escalation.reason,
            suggested_team=analysis.escalation.suggested_human_team,
            payload=escalated_payload,
        )
        _append_escalation_jsonl(escalated_payload)
    else:
        # Sin escalación: quitar filas viejas del mismo mensaje (p. ej. tras política
        # trust_llm o re-batch) para que el dashboard no cuente la cola dos veces.
        db.delete_escalations_for_message(message_id)

    return {
        "status": "ok",
        "analysis": analysis,
        "score": score,
        "priority_breakdown": priority_breakdown,
        "escalated": escalated_payload is not None,
        "payload": escalated_payload,
        "tokens": {"in": usage.tokens_in, "out": usage.tokens_out},
        "model_used": usage.model,
        "latency_ms": usage.latency_ms,
    }


def _append_escalation_jsonl(payload: dict[str, Any]) -> None:
    """Append-only journal for downstream consumers (Slack/Zendesk/etc. simulation)."""
    ESCALATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with ESCALATIONS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
