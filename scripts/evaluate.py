"""Evaluate the LLM analysis quality against a hand-labeled subset.

Reads `tests/fixtures/labeled_subset.json`, looks up the corresponding case
in SQLite (the batch must have been processed first), and computes:

  - Sentiment accuracy (exact label match) and a tolerant version (off-by-one).
  - Weak-points recall (did we hit at least one expected topic?).
  - Escalation precision/recall against expected_should_escalate.
  - Priority match (within expected_priority_in); per_case includes priority_llm.
  - Regulatory flags recall (≥1 expected type detected).
  - When expected_regulatory_flag_types is []: tracks false positives (system flags but human said none).
  - Optional --review-status APPROVED (repeatable) to restrict which JSON rows count.

Usage:
    python scripts/evaluate.py
    python scripts/evaluate.py --output reports/eval_v1.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.database import db  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

LABELED = PROJECT_ROOT / "tests" / "fixtures" / "labeled_subset.json"

SENTIMENT_ORDER = ["very_negative", "negative", "neutral", "positive", "very_positive"]


def _sentiment_distance(a: str, b: str) -> int:
    try:
        return abs(SENTIMENT_ORDER.index(a) - SENTIMENT_ORDER.index(b))
    except ValueError:
        return 99


def _last_inbound_analysis(case_id: str) -> dict[str, Any] | None:
    thread = db.get_thread(case_id)
    if not thread:
        return None
    inbound = [m for m in thread if m["direction"] == "INBOUND"]
    if not inbound:
        return None
    last = inbound[-1]
    return db.get_analysis(last["message_id"])


def evaluate(labeled_path: Path, review_status_filter: list[str] | None = None) -> dict[str, Any]:
    db.init_db()
    with labeled_path.open("r", encoding="utf-8") as f:
        labeled = json.load(f)

    cases = labeled["cases"]
    if review_status_filter:
        filt = {s.upper() for s in review_status_filter}
        cases = [c for c in cases if str(c.get("review_status", "PENDING")).upper() in filt]
        logger.info(
            "Evaluating %d labeled cases (filter review_status=%s).",
            len(cases),
            sorted(filt),
        )
    else:
        logger.info("Evaluating %d labeled cases (no status filter).", len(cases))

    per_case: list[dict[str, Any]] = []
    sentiment_exact = 0
    sentiment_tolerant = 0
    sentiment_evaluated = 0

    weak_recall_hits = 0
    weak_recall_evaluated = 0

    reg_recall_hits = 0
    reg_recall_evaluated = 0
    reg_none_expected_evaluated = 0
    reg_none_expected_correct = 0
    reg_false_positive_when_none_expected = 0

    priority_hits = 0
    priority_evaluated = 0

    esc_tp = esc_fp = esc_fn = esc_tn = 0

    not_found = 0
    failed_status = 0

    tokens_in = tokens_out = 0

    for c in cases:
        cid = c["case_id"]
        analysis = _last_inbound_analysis(cid)
        if analysis is None:
            not_found += 1
            per_case.append({"case_id": cid, "error": "not_found_in_db"})
            continue
        if analysis.get("analysis_status") != "ok":
            failed_status += 1
            per_case.append({"case_id": cid, "error": f"analysis_status={analysis.get('analysis_status')}"})
            continue

        tokens_in += analysis.get("tokens_in", 0) or 0
        tokens_out += analysis.get("tokens_out", 0) or 0

        # Sentiment
        pred_label = analysis.get("sentiment_label")
        exp_label = c.get("expected_sentiment_label")
        sent_eval = {"expected": exp_label, "predicted": pred_label}
        if exp_label and pred_label:
            sentiment_evaluated += 1
            if pred_label == exp_label:
                sentiment_exact += 1
                sentiment_tolerant += 1
                sent_eval["match"] = "exact"
            elif _sentiment_distance(pred_label, exp_label) <= 1:
                sentiment_tolerant += 1
                sent_eval["match"] = "tolerant"
            else:
                sent_eval["match"] = "miss"

        # Weak points recall
        pred_topics = {wp["topic"] for wp in (analysis.get("weak_points") or [])}
        exp_topics = set(c.get("expected_topics", []))
        wp_eval = {"expected": list(exp_topics), "predicted": list(pred_topics)}
        if exp_topics:
            weak_recall_evaluated += 1
            if pred_topics & exp_topics:
                weak_recall_hits += 1
                wp_eval["match"] = "hit"
            else:
                wp_eval["match"] = "miss"

        # Regulatory: recall cuando el humano espera ≥1 tipo; precisión “sin ruido”
        # cuando el humano deja la lista vacía (no debía haber flags).
        pred_reg = {
            f["type"] for f in (analysis.get("regulatory_flags") or []) if isinstance(f, dict) and f.get("type")
        }
        if "expected_regulatory_flag_types" in c:
            exp_reg = set(c.get("expected_regulatory_flag_types") or [])
        else:
            exp_reg = set()
        reg_eval: dict[str, Any] = {"expected": list(exp_reg), "predicted": list(pred_reg)}
        if "expected_regulatory_flag_types" not in c:
            reg_eval["match"] = "skipped_no_field"
        elif not exp_reg:
            reg_none_expected_evaluated += 1
            if not pred_reg:
                reg_none_expected_correct += 1
                reg_eval["match"] = "hit_no_flags"
            else:
                reg_false_positive_when_none_expected += 1
                reg_eval["match"] = "false_positive_flags"
        else:
            reg_recall_evaluated += 1
            if pred_reg & exp_reg:
                reg_recall_hits += 1
                reg_eval["match"] = "hit"
            else:
                reg_eval["match"] = "miss"

        # Priority
        pred_priority = analysis.get("priority")
        exp_priority = c.get("expected_priority_in", [])
        prio_eval = {"expected": exp_priority, "predicted": pred_priority}
        if exp_priority:
            priority_evaluated += 1
            if pred_priority in exp_priority:
                priority_hits += 1
                prio_eval["match"] = "hit"
            else:
                prio_eval["match"] = "miss"

        # Escalation (the analysis is on the LAST inbound; we check if ANY escalation was triggered for the case
        # via the score+rules). We approximate: if the LAST analysis says needed OR priority>=p2 we count as escalated.
        esc_obj = analysis.get("escalation") or {}
        pred_escalated = bool(esc_obj.get("needed")) or pred_priority in {"p1", "p2"}
        exp_escalated = bool(c.get("expected_should_escalate", False))

        priority_llm = esc_obj.get("priority")

        if pred_escalated and exp_escalated:
            esc_tp += 1
            esc_outcome = "TP"
        elif pred_escalated and not exp_escalated:
            esc_fp += 1
            esc_outcome = "FP"
        elif not pred_escalated and exp_escalated:
            esc_fn += 1
            esc_outcome = "FN"
        else:
            esc_tn += 1
            esc_outcome = "TN"

        per_case.append(
            {
                "case_id": cid,
                "category": c.get("category"),
                "sentiment": sent_eval,
                "weak_points": wp_eval,
                "regulatory": reg_eval,
                "priority": {
                    **prio_eval,
                    "priority_llm": priority_llm,
                    "note": "predicted es priority_final persistido (max math, llm; ver triage).",
                },
                "escalation": {"expected": exp_escalated, "predicted": pred_escalated, "outcome": esc_outcome},
                "score_final": analysis.get("score_final"),
                "confidence": analysis.get("confidence"),
                "tokens": {"in": analysis.get("tokens_in", 0), "out": analysis.get("tokens_out", 0)},
                "model_used": analysis.get("model_used"),
            }
        )

    summary: dict[str, Any] = {
        "labeled_cases": len(cases),
        "evaluated": len(cases) - not_found - failed_status,
        "not_found_in_db": not_found,
        "failed_status": failed_status,
        "sentiment": {
            "evaluated": sentiment_evaluated,
            "accuracy_exact": _safe_div(sentiment_exact, sentiment_evaluated),
            "accuracy_tolerant_off_by_one": _safe_div(sentiment_tolerant, sentiment_evaluated),
        },
        "weak_points_recall": {
            "evaluated": weak_recall_evaluated,
            "recall": _safe_div(weak_recall_hits, weak_recall_evaluated),
        },
        "regulatory_flags_recall": {
            "evaluated": reg_recall_evaluated,
            "recall": _safe_div(reg_recall_hits, reg_recall_evaluated),
        },
        "regulatory_flags_when_none_expected": {
            "evaluated": reg_none_expected_evaluated,
            "predicted_none_rate": _safe_div(reg_none_expected_correct, reg_none_expected_evaluated),
            "false_positives": reg_false_positive_when_none_expected,
        },
        "priority": {
            "evaluated": priority_evaluated,
            "match_rate": _safe_div(priority_hits, priority_evaluated),
        },
        "escalation": {
            "tp": esc_tp,
            "fp": esc_fp,
            "fn": esc_fn,
            "tn": esc_tn,
            "precision": _safe_div(esc_tp, esc_tp + esc_fp),
            "recall": _safe_div(esc_tp, esc_tp + esc_fn),
            "f1": _safe_div(2 * esc_tp, 2 * esc_tp + esc_fp + esc_fn),
        },
        "tokens": {
            "tokens_in_total": tokens_in,
            "tokens_out_total": tokens_out,
        },
    }
    return {"summary": summary, "per_case": per_case}


def _safe_div(num: float, den: float) -> float | None:
    if not den:
        return None
    return round(num / den, 4)


def _print_report(report: dict[str, Any]) -> None:
    s = report["summary"]
    print("\n---------- EVALUATION REPORT ----------")
    print(f"Labeled cases:     {s['labeled_cases']}")
    print(f"Evaluated:         {s['evaluated']} (not_found={s['not_found_in_db']}, failed={s['failed_status']})")
    print()
    print("Sentiment")
    print(f"  Exact accuracy:           {s['sentiment']['accuracy_exact']}")
    print(f"  Tolerant (off-by-one):    {s['sentiment']['accuracy_tolerant_off_by_one']}")
    print()
    print("Weak points")
    print(f"  Recall (hit >=1 topic):   {s['weak_points_recall']['recall']}  (n={s['weak_points_recall']['evaluated']})")
    print()
    print("Regulatory flags")
    print(f"  Recall (hit >=1 type):    {s['regulatory_flags_recall']['recall']}  (n={s['regulatory_flags_recall']['evaluated']})")
    rne = s.get("regulatory_flags_when_none_expected") or {}
    print(
        f"  Sin flags esperados:      predijo ninguno en {rne.get('predicted_none_rate')}  "
        f"(n={rne.get('evaluated')}, FP={rne.get('false_positives')})"
    )
    print()
    print("Priority")
    print(f"  Match rate (in expected): {s['priority']['match_rate']}  (n={s['priority']['evaluated']})")
    print()
    print("Escalation (binary)")
    print(f"  TP={s['escalation']['tp']}  FP={s['escalation']['fp']}  FN={s['escalation']['fn']}  TN={s['escalation']['tn']}")
    print(f"  Precision={s['escalation']['precision']}  Recall={s['escalation']['recall']}  F1={s['escalation']['f1']}")
    print()
    print(f"Tokens (subset): in={s['tokens']['tokens_in_total']}  out={s['tokens']['tokens_out_total']}")
    print("--------------------------------------")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labeled", type=str, default=str(LABELED))
    parser.add_argument(
        "--review-status",
        action="append",
        default=None,
        help=(
            "Solo casos con este review_status (p.ej. APPROVED). "
            "Repetir flag para varios valores. Sin flag = todos los casos del JSON."
        ),
    )
    parser.add_argument("--output", type=str, default=None,
                        help="Write detailed JSON report here (default: print only).")
    args = parser.parse_args()

    report = evaluate(Path(args.labeled), review_status_filter=args.review_status)
    _print_report(report)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info("Detailed report saved to %s", out)


if __name__ == "__main__":
    main()
