"""Lógica de scoring y escalación.

Funciones puras que toman un `Analysis` (output del LLM) y devuelven un
score numérico, una prioridad y un payload de escalación listo para ser
consumido por el sistema downstream (Slack, Zendesk, cola).

Pesos y umbrales viven en `config/scoring.yaml` para calibrar sin tocar
código (ver `docs/04_scoring_and_escalation.md`).

Política de prioridad (decisión 2026-05-01, reemplaza al `max(math, llm)`
del 2026-04-30):

    El LLM emite su prioridad cualitativa (`analysis.escalation.priority`)
    razonando sobre el hilo completo. La fórmula calcula `priority_math` a
    partir de una suma ponderada de señales que el modelo ya vio (severity,
    sentiment, regulatory, urgency). Ambas se exponen para auditoría, pero
    NO tienen la misma autoridad.

    Política por defecto: TRUST THE LLM.
        priority_final = priority_llm

    La fórmula es **red de seguridad**, no override. Solo entra cuando el
    LLM bajó (p3/p4) y existe un riesgo severo objetivo que pudo
    subponderar. En ese caso se toma `max(math, llm)`.

    Riesgos severos (`_has_severe_risk`):
      - urgency_signals.human_safety_risk
      - urgency_signals.explicit_emergency
      - regulatory_flag de tipo en HARD_REGULATORY_TYPES
        (fraud_suspected, aml_suspected, unauthorized_charge)
        con confianza >= regulatory.high_confidence_threshold

    `money_blocked`, `threat_of_legal_action`, `consumer_protection` y
    `data_privacy` NO son riesgos severos a estos efectos: el LLM ya los
    consideró al producir su `priority`. Dejar que la fórmula los contara
    de nuevo era la causa raíz de los falsos p1.

    El override de hilo resuelto sigue ganando sobre todo lo anterior: si
    el último INBOUND expresa resolución/gratitud sin riesgo severo, se
    fuerza `priority_final="p4"` y no se escala.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

from src.core.guards import is_resolved_thread, max_priority, priority_rank
from src.core.schema import Analysis, RegulatoryFlag, UrgencySignals


@lru_cache(maxsize=1)
def load_scoring_config() -> dict:
    path = Path(os.getenv("SCORING_CONFIG", "config/scoring.yaml"))
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# Tipos regulatorios que SÍ son señal dura: indican un riesgo concreto y
# accionable. Los excluidos (`consumer_protection`, `data_privacy`, `other`)
# son categorías amplias que la IA dispara fácil con cualquier queja seria —
# no deben, por sí solos, subir un caso a p1 contra el criterio del LLM.
HARD_REGULATORY_TYPES = frozenset({
    "fraud_suspected",
    "aml_suspected",
    "unauthorized_charge",
})


def _has_severe_risk(analysis: Analysis, cfg: dict) -> tuple[bool, list[str]]:
    """Riesgos objetivos severos que el LLM podría haber subponderado.

    Solo estos justifican que la fórmula suba la prioridad por encima del LLM.
    Devuelve (flag, lista de razones para auditoría).
    """
    reasons: list[str] = []
    u = analysis.urgency_signals
    if u.human_safety_risk:
        reasons.append("urgency.human_safety_risk")
    if u.explicit_emergency:
        reasons.append("urgency.explicit_emergency")

    threshold = float(cfg["regulatory"]["high_confidence_threshold"])
    conf = float(analysis.confidence or 0.0)
    if conf >= threshold:
        for f in analysis.regulatory_flags or []:
            if f.type in HARD_REGULATORY_TYPES:
                reasons.append(f"regulatory.{f.type}")
                break

    return (len(reasons) > 0, reasons)


def _max_severity(weak_points) -> float:
    if not weak_points:
        return 0.0
    return max((wp.severity for wp in weak_points), default=0.0)


def _regulatory_risk(flags: list[RegulatoryFlag], confidence: float, cfg: dict) -> float:
    if not flags:
        return 0.0
    threshold = cfg["regulatory"]["high_confidence_threshold"]
    high = cfg["regulatory"]["high_score"]
    low = cfg["regulatory"]["low_score"]
    return high if confidence >= threshold else low


def _urgency_expressed(signals: UrgencySignals, cfg: dict) -> float:
    u = cfg["urgency"]
    score = 0.0
    if signals.explicit_emergency:
        score += u["explicit_emergency"]
    if signals.human_safety_risk:
        score += u["human_safety_risk"]
    if signals.money_blocked:
        score += u["money_blocked"]
    if signals.threat_of_legal_action:
        score += u["threat_of_legal_action"]
    if signals.repeated_unanswered_contact:
        score += u["repeated_unanswered_contact"]
    score += u["intensity_factor"] * float(signals.intensity or 0.0)
    return min(1.0, score)


def _sentiment_negativity(score: float) -> float:
    return max(0.0, -float(score or 0.0))


def _sla_factor(first_inbound_at: Optional[datetime], now: Optional[datetime], cfg: dict) -> float:
    sla = cfg.get("sla", {})
    if not sla.get("enabled") or not first_inbound_at:
        return 1.0
    now = now or datetime.now(tz=timezone.utc)
    if first_inbound_at.tzinfo is None:
        first_inbound_at = first_inbound_at.replace(tzinfo=timezone.utc)
    hours = (now - first_inbound_at).total_seconds() / 3600.0
    full = float(sla.get("full_penalty_hours", 24))
    cap = float(sla.get("max_multiplier", 1.5))
    factor = 1.0 + (cap - 1.0) * min(1.0, hours / max(1.0, full))
    return factor


def _priority_from_score(score: float, cfg: dict) -> str:
    t = cfg["thresholds"]
    if score >= t["p1_critical"]:
        return "p1"
    if score >= t["p2_high"]:
        return "p2"
    if score >= t["p3_medium"]:
        return "p3"
    return "p4"


def compute_score(
    analysis: Analysis,
    *,
    first_inbound_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Devuelve el desglose de scoring y la prioridad final para un Analysis.

    Claves devueltas:
      severity, regulatory_risk, urgency_expressed, sentiment_negativity,
      score_base, sla_factor, score_final,
      priority_math, priority_llm, priority_final, priority,
      priority_policy, severe_risk_signals, resolved_override.

    Semántica:
      * `priority_math`   — proviene solo del score ponderado y los umbrales.
      * `priority_llm`    — la prioridad cualitativa que devolvió el modelo.
      * `priority_final`  — política `trust_llm` por defecto: igual a
                            `priority_llm`. La fórmula solo "sube" el final
                            como red de seguridad cuando el LLM bajó a p3/p4
                            y `_has_severe_risk()` detecta un riesgo objetivo
                            severo (riesgo humano, emergencia explícita,
                            regulatorio duro con confianza).
      * `priority_policy` — trazabilidad: `trust_llm`,
                            `math_overrides_due_to_severe_risk`, o
                            `resolved_override`.

    Override de hilo resuelto:
      Si `is_resolved_thread(analysis)` es True, fuerza `priority_final="p4"`
      y baja `score_final` justo por debajo del umbral de escalación. Los
      campos `priority_math` y `priority_llm` se conservan tal cual para que
      el revisor humano vea por qué se aplicó el override.
    """
    cfg = load_scoring_config()
    w = cfg["weights"]

    severity = _max_severity(analysis.weak_points)
    reg = _regulatory_risk(analysis.regulatory_flags, float(analysis.confidence or 0.0), cfg)
    urgency = _urgency_expressed(analysis.urgency_signals, cfg)
    sneg = _sentiment_negativity(float(analysis.sentiment.score))

    base = (
        w["severity"] * severity
        + w["regulatory_risk"] * reg
        + w["urgency_expressed"] * urgency
        + w["sentiment_negativity"] * sneg
    )
    sla = _sla_factor(first_inbound_at, now, cfg)
    final = min(1.0, base * sla)

    priority_math = _priority_from_score(final, cfg)
    priority_llm = (analysis.escalation.priority or "p4").lower()

    has_severe, severe_reasons = _has_severe_risk(analysis, cfg)
    llm_lowered = priority_llm in {"p3", "p4"}
    math_higher = priority_rank(priority_math) < priority_rank(priority_llm)

    # Default: confiamos en el LLM. La fórmula solo entra como red de
    # seguridad cuando el LLM bajó (p3/p4), el math está más alto, y hay
    # un riesgo severo objetivo concreto.
    if llm_lowered and math_higher and has_severe:
        priority_final = max_priority(priority_math, priority_llm)
        priority_policy = "math_overrides_due_to_severe_risk"
    else:
        priority_final = priority_llm
        priority_policy = "trust_llm"

    resolved_override = is_resolved_thread(analysis)
    if resolved_override:
        priority_final = "p4"
        priority_policy = "resolved_override"
        escalate_above = float(cfg["thresholds"]["escalate_to_human_above"])
        final = min(final, max(0.0, escalate_above - 0.01))

    return {
        "severity": round(severity, 4),
        "regulatory_risk": round(reg, 4),
        "urgency_expressed": round(urgency, 4),
        "sentiment_negativity": round(sneg, 4),
        "score_base": round(base, 4),
        "sla_factor": round(sla, 4),
        "score_final": round(final, 4),
        "priority_math": priority_math,
        "priority_llm": priority_llm,
        "priority_final": priority_final,
        "priority": priority_final,
        "priority_policy": priority_policy,
        "severe_risk_signals": severe_reasons,
        "resolved_override": resolved_override,
    }


def must_escalate(
    analysis: Analysis,
    *,
    score_final: float,
    cfg: Optional[dict] = None,
) -> bool:
    """Decide si el caso debe enviarse a un humano.

    Reglas (en orden):
      0. Override de hilo resuelto: si el último mensaje INBOUND expresa
         resolución y no hay riesgo severo, no escalar. Corrige el bug de
         "efecto memoria" donde un hilo cerrado seguía escalando por un
         weak_point antiguo.
      1. Riesgo severo objetivo (`_has_severe_risk`): siempre escala.
      2. Caso normal: se respeta el `escalation.needed` que devolvió el LLM.
         No se evalúa el score numérico aquí porque se construye con las
         mismas señales que ya consideró el modelo al decidir.

    Los estados `failed_analysis` y `low_confidence` se manejan fuera de
    esta función, en el orchestrator.
    """
    cfg = cfg or load_scoring_config()

    if is_resolved_thread(analysis):
        return False

    has_severe, _ = _has_severe_risk(analysis, cfg)
    if has_severe:
        return True

    return bool(analysis.escalation.needed)


def build_escalation_payload(
    *,
    case_id: str,
    message_id: str,
    user_pseudonym: Optional[str],
    country_iso: Optional[str],
    language: str,
    analysis: Analysis,
    score_final: float,
    priority: str,
    thread_excerpt: list[dict[str, str]],
    priority_breakdown: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Format ready for the next downstream system (Slack, Zendesk, queue, etc.).

    `priority_breakdown` exposes the math vs LLM priorities so a human reviewer
    in the downstream queue can see whether the urgency was inferred from the
    formula, from the model's qualitative call, or both agreed.
    """
    return {
        "created_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "case_id": case_id,
        "message_id": message_id,
        "user_pseudonym": user_pseudonym,
        "country": country_iso,
        "language": language,
        "score_final": score_final,
        "priority": priority,
        "priority_breakdown": priority_breakdown or {},
        "suggested_team": analysis.escalation.suggested_human_team,
        "reason": analysis.escalation.reason,
        "weak_points": [wp.model_dump() for wp in analysis.weak_points],
        "regulatory_flags": [rf.model_dump() for rf in analysis.regulatory_flags],
        "urgency_signals": analysis.urgency_signals.model_dump(),
        "thread_excerpt": thread_excerpt,
        "suggested_first_response_language": language,
    }
