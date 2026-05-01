"""Rescore all stored analyses without calling the LLM again.

Reads every successful analysis from SQLite, reconstructs the `Analysis`
pydantic object, recomputes `compute_score(...)` with the CURRENT
`config/scoring.yaml` and the CURRENT triage policy, and updates the row
in place (`score_base`, `score_final`, `priority`).

Why this exists:

  * The expensive part is the LLM call. The triage logic (math, policy,
    overrides) is deterministic over the stored analysis. Iterating on
    weights / policy / thresholds should NEVER require re-paying tokens.
  * `process_batch.py --resume` skips messages that already have analysis,
    so it does NOT refresh the score. This script does.

Usage:

    python scripts/rescore.py
    python scripts/rescore.py --case CASE-052       # only one case
    python scripts/rescore.py --report reports/rescore_diff.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.schema import Analysis  # noqa: E402
from src.core.triage import compute_score, load_scoring_config  # noqa: E402
from src.database import db as db_mod  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _load_analysis_row(row: sqlite3.Row) -> Analysis | None:
    try:
        weak = json.loads(row["weak_points"]) if row["weak_points"] else []
        regs = json.loads(row["regulatory_flags"]) if row["regulatory_flags"] else []
        urg = json.loads(row["urgency_signals"]) if row["urgency_signals"] else {}
        esc = json.loads(row["escalation"]) if row["escalation"] else {}
    except json.JSONDecodeError:
        return None

    payload = {
        "language": (row["language"] or "es")[:2] if row["language"] else "es",
        "sentiment": {
            "score": float(row["sentiment_score"] or 0.0),
            "label": row["sentiment_label"] or "neutral",
        },
        "primary_emotion": row["primary_emotion"] or "neutral",
        "weak_points": weak,
        "regulatory_flags": regs,
        "urgency_signals": urg,
        "escalation": {
            "needed": bool(esc.get("needed")),
            "priority": (esc.get("priority") or "p4").lower(),
            "reason": esc.get("reason") or "",
            "suggested_human_team": esc.get("suggested_human_team") or "tier1_support",
        },
        "confidence": float(row["confidence"] or 0.0),
    }
    if payload["language"] not in ("es", "pt", "en", "fr"):
        payload["language"] = "other"
    try:
        return Analysis.model_validate(payload)
    except Exception as e:  # noqa: BLE001
        logger.warning("Cannot reconstruct Analysis for %s: %s", row["message_id"], e)
        return None


def rescore(case_filter: str | None = None) -> dict[str, Any]:
    db_mod.init_db()
    load_scoring_config.cache_clear()
    cfg_dump = load_scoring_config()

    db_path = db_mod._resolve_db_path()  # type: ignore[attr-defined]
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row

    sql = """
        SELECT a.*, m.case_id, m.language
          FROM analyses a
          JOIN messages m ON m.message_id = a.message_id
         WHERE a.analysis_status = 'ok'
    """
    params: tuple = ()
    if case_filter:
        sql += " AND m.case_id = ?"
        params = (case_filter,)

    rows = conn.execute(sql, params).fetchall()
    logger.info("Rescoring %d analyses (case_filter=%s).", len(rows), case_filter or "ALL")

    diffs: list[dict[str, Any]] = []
    before = Counter()
    after = Counter()
    policy_counter = Counter()

    for row in rows:
        analysis = _load_analysis_row(row)
        if analysis is None:
            continue
        first_at = db_mod.get_case_first_inbound_at(row["case_id"])
        score = compute_score(analysis, first_inbound_at=first_at, now=datetime.now(timezone.utc))

        old_priority = row["priority"]
        new_priority = score["priority_final"]
        before[old_priority] += 1
        after[new_priority] += 1
        policy_counter[score["priority_policy"]] += 1

        if old_priority != new_priority or abs((row["score_final"] or 0.0) - score["score_final"]) > 1e-4:
            diffs.append({
                "case_id": row["case_id"],
                "message_id": row["message_id"],
                "old": {"priority": old_priority, "score_final": row["score_final"]},
                "new": {
                    "priority": new_priority,
                    "score_final": score["score_final"],
                    "priority_math": score["priority_math"],
                    "priority_llm": score["priority_llm"],
                    "priority_policy": score["priority_policy"],
                    "severe_risk_signals": score["severe_risk_signals"],
                    "resolved_override": score["resolved_override"],
                },
            })

        conn.execute(
            "UPDATE analyses SET score_base=?, score_final=?, priority=? WHERE message_id=?",
            (score["score_base"], score["score_final"], new_priority, row["message_id"]),
        )

    conn.commit()
    conn.close()

    result = {
        "config_used": cfg_dump,
        "n_analyses": len(rows),
        "before_priority_distribution": dict(before),
        "after_priority_distribution": dict(after),
        "policy_distribution": dict(policy_counter),
        "n_changes": len(diffs),
        "diffs": diffs,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=str, default=None, help="Solo una case_id (debug).")
    parser.add_argument(
        "--report",
        type=str,
        default=None,
        help="Guardar el detalle de cambios en JSON.",
    )
    args = parser.parse_args()

    out = rescore(case_filter=args.case)
    print(f"\nAnalyses procesadas: {out['n_analyses']}")
    print(f"Cambios de prioridad o score: {out['n_changes']}")
    print(f"Antes: {out['before_priority_distribution']}")
    print(f"Despues: {out['after_priority_distribution']}")
    print(f"Politica aplicada: {out['policy_distribution']}")

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nDetalle de cambios guardado en {args.report}")


if __name__ == "__main__":
    main()
