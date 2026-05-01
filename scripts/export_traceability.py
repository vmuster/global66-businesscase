"""Export full traceability of the input dataset alongside its analysis.

Reads:
  - data/Business tech case 1 - BBDD.xlsx (input dataset)
  - data/voc.db (analyses produced by process_batch.py)

Produces:
  - <output>.csv   wide table with one row per case_id, including original text,
                   detected language, country, sentiment, primary topics,
                   priority breakdown, escalation status and per-call cost.
  - <output>.json  full JSON dump of the audit (already structured), copied
                   for auditors who prefer programmatic access.

Designed for the entrega: an auditor can open the CSV in Excel and see the
end-to-end pipeline output for every customer case.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.pricing import call_cost_usd  # noqa: E402

DEFAULT_INPUT_XLSX = PROJECT_ROOT / "data" / "Business tech case 1 - BBDD.xlsx"
DEFAULT_DB = PROJECT_ROOT / "data" / "voc.db"
DEFAULT_AUDIT_JSON = PROJECT_ROOT / "data" / "results_audit.json"


def _load_xlsx(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path)
    return df


def _load_db(path: Path) -> dict[str, pd.DataFrame]:
    with sqlite3.connect(path) as conn:
        cases = pd.read_sql_query("SELECT * FROM cases", conn)
        messages = pd.read_sql_query("SELECT * FROM messages", conn)
        analyses = pd.read_sql_query("SELECT * FROM analyses", conn)
        escalations = pd.read_sql_query("SELECT * FROM escalations", conn)
    return {
        "cases": cases,
        "messages": messages,
        "analyses": analyses,
        "escalations": escalations,
    }


def _safe_load(s: Any) -> Any:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    if isinstance(s, str):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return None
    return s


def _topics_of(weak_points: Any) -> str:
    wps = _safe_load(weak_points) or []
    return ", ".join(sorted({wp.get("topic") for wp in wps if wp.get("topic")}))


def _max_severity(weak_points: Any) -> float:
    wps = _safe_load(weak_points) or []
    sevs = [float(wp.get("severity", 0) or 0) for wp in wps]
    return max(sevs) if sevs else 0.0


def _flags_of(reg_flags: Any) -> str:
    flags = _safe_load(reg_flags) or []
    return ", ".join(sorted({f.get("type") for f in flags if f.get("type")}))


def _escalation_field(escalation: Any, key: str) -> Any:
    esc = _safe_load(escalation) or {}
    return esc.get(key)


def _priority_breakdown_for(message_id: str, audit_index: dict[str, dict]) -> dict[str, Any]:
    """Devuelve el desglose math/llm/política almacenado en `results_audit.json`.

    Si el JSON de auditoría aún no fue generado (primer run), regresa solo el campo
    persistido `priority` (el final). El CSV se mantiene utilizable en cualquier caso.
    """
    rec = audit_index.get(message_id) or {}
    score = rec.get("score") or {}
    return {
        "priority_math": score.get("priority_math"),
        "priority_llm_score": score.get("priority_llm"),
        "priority_policy": score.get("priority_policy"),
        "severe_risk_signals": ", ".join(score.get("severe_risk_signals") or []),
        "resolved_override": score.get("resolved_override"),
    }


def _index_audit(audit_path: Path) -> dict[str, dict]:
    """Devuelve un dict {message_id: registro} a partir de `results_audit.json`."""
    if not audit_path.exists():
        return {}
    try:
        data = json.loads(audit_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    out: dict[str, dict] = {}
    for r in data.get("results", []):
        mid = r.get("message_id")
        if mid:
            out[str(mid)] = r
    return out


def build_traceability_rows(
    input_df: pd.DataFrame,
    db_data: dict[str, pd.DataFrame],
    audit_index: dict[str, dict],
) -> list[dict[str, Any]]:
    """Devuelve una fila por (case_id, message_id) con el análisis cuando existe.

    Los mensajes OUTBOUND se mantienen en el thread con análisis vacío para no
    perder el contexto conversacional al exportar a Excel.
    """
    cases = db_data["cases"].set_index("case_id").to_dict("index")
    analyses = db_data["analyses"].set_index("message_id").to_dict("index")

    rows = []
    for r in input_df.itertuples(index=False):
        case_meta = cases.get(str(r.case_id), {})
        ana = analyses.get(str(r.message_id), {})

        cost_usd = call_cost_usd(
            int(ana.get("tokens_in") or 0),
            int(ana.get("tokens_out") or 0),
            ana.get("model_used"),
        ) if ana else 0.0

        breakdown = _priority_breakdown_for(str(r.message_id), audit_index)

        rows.append({
            "case_id": str(r.case_id),
            "message_id": str(r.message_id),
            "user_id_original": str(r.user_id) if pd.notna(r.user_id) else "",
            "pais_usuario": str(r.pais_usuario) if pd.notna(r.pais_usuario) else "",
            "country_iso": case_meta.get("country_iso") or "",
            "direction": str(r.direction),
            "text_original": str(r.text)[:1000] if pd.notna(r.text) else "",
            "language_detected": case_meta.get("language") or "",
            "sentiment_score": ana.get("sentiment_score"),
            "sentiment_label": ana.get("sentiment_label"),
            "primary_emotion": ana.get("primary_emotion"),
            "topics": _topics_of(ana.get("weak_points")),
            "max_severity": _max_severity(ana.get("weak_points")),
            "regulatory_flags": _flags_of(ana.get("regulatory_flags")),
            "escalation_needed_llm": _escalation_field(ana.get("escalation"), "needed"),
            "priority_llm": _escalation_field(ana.get("escalation"), "priority"),
            "priority_math": breakdown["priority_math"],
            "priority_final": ana.get("priority"),
            "priority_policy": breakdown["priority_policy"],
            "severe_risk_signals": breakdown["severe_risk_signals"],
            "resolved_override": breakdown["resolved_override"],
            "score_base": ana.get("score_base"),
            "score_final": ana.get("score_final"),
            "confidence": ana.get("confidence"),
            "model_used": ana.get("model_used"),
            "tokens_in": ana.get("tokens_in"),
            "tokens_out": ana.get("tokens_out"),
            "latency_ms": ana.get("latency_ms"),
            "cost_usd": round(cost_usd, 8) if cost_usd else 0,
            "analyzed_at": ana.get("analyzed_at"),
            "analysis_status": ana.get("analysis_status") or "not_analyzed",
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Export end-to-end traceability of the input dataset.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_XLSX)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT_JSON)
    parser.add_argument("--output-csv", type=Path, default=PROJECT_ROOT / "Entregables" / "data_outputs" / "traceability_dataset.csv")
    parser.add_argument("--output-json", type=Path, default=PROJECT_ROOT / "Entregables" / "data_outputs" / "results_audit.json")
    args = parser.parse_args()

    print(f"[traceability] reading input xlsx: {args.input}")
    input_df = _load_xlsx(args.input)
    print(f"[traceability] reading db: {args.db}")
    db_data = _load_db(args.db)
    audit_index = _index_audit(args.audit)

    rows = build_traceability_rows(input_df, db_data, audit_index)
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    print(f"[traceability] writing csv: {args.output_csv} ({len(rows)} rows)")
    if rows:
        with args.output_csv.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    if args.audit.exists():
        print(f"[traceability] copying audit json: {args.audit} -> {args.output_json}")
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(args.audit.read_text(encoding="utf-8"), encoding="utf-8")

    print("[traceability] done.")


if __name__ == "__main__":
    main()
