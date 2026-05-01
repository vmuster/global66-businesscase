"""Re-run the LLM analysis on a specific list of case_ids using the CURRENT prompt.

Use this to test prompt changes WITHOUT spending tokens on the whole batch:

    python scripts/reanalyze_cases.py CASE-008 CASE-013 CASE-016
    python scripts/reanalyze_cases.py --from-misses reports/eval_v5_trust_llm.json

It picks the LATEST inbound message of each case, deletes its previous analysis
row, and runs `analyze_and_persist` so triage + scoring re-fire too.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from src.core.engine import build_client_from_env  # noqa: E402
from src.core.orchestrator import analyze_and_persist  # noqa: E402
from src.database import db as db_mod  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _last_inbound_message(case_id: str) -> sqlite3.Row | None:
    db_path = db_mod._resolve_db_path()  # type: ignore[attr-defined]
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            SELECT m.message_id, m.case_id, m.user_pseudonym, m.language, c.country_iso
              FROM messages m JOIN cases c ON c.case_id = m.case_id
             WHERE m.case_id = ? AND m.direction = 'INBOUND'
             ORDER BY m.created_at DESC, m.message_id DESC
             LIMIT 1
            """,
            (case_id,),
        ).fetchone()
    finally:
        conn.close()


def _delete_existing_analysis(message_id: str) -> None:
    db_path = db_mod._resolve_db_path()  # type: ignore[attr-defined]
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("DELETE FROM analyses WHERE message_id = ?", (message_id,))
        conn.execute("DELETE FROM escalations WHERE message_id = ?", (message_id,))
        conn.commit()
    finally:
        conn.close()


async def reanalyze(case_ids: list[str]) -> list[dict]:
    client = build_client_from_env()
    out = []
    for cid in case_ids:
        row = _last_inbound_message(cid)
        if not row:
            logger.warning("No INBOUND message found for %s, skipping.", cid)
            continue
        logger.info("Re-analyzing %s (msg=%s).", cid, row["message_id"])
        _delete_existing_analysis(row["message_id"])
        result = await analyze_and_persist(
            client=client,
            case_id=cid,
            message_id=row["message_id"],
            user_pseudonym=row["user_pseudonym"],
            country_iso=row["country_iso"],
            detected_language=row["language"],
        )
        score = result.get("score") or {}
        out.append(
            {
                "case_id": cid,
                "priority_final": score.get("priority_final"),
                "priority_llm": score.get("priority_llm"),
                "priority_math": score.get("priority_math"),
                "priority_policy": score.get("priority_policy"),
                "tokens_in": result.get("tokens", {}).get("in"),
                "tokens_out": result.get("tokens", {}).get("out"),
                "model": result.get("model_used"),
            }
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_ids", nargs="*", help="case_ids a reprocesar")
    parser.add_argument(
        "--from-misses",
        type=str,
        default=None,
        help="Ruta a un eval_*.json: incluir todos los case_id con priority.match=miss",
    )
    parser.add_argument(
        "--also",
        action="append",
        default=[],
        help="case_id de control que se quiera incluir explícitamente (repetible).",
    )
    args = parser.parse_args()

    cids: list[str] = list(args.case_ids)
    if args.from_misses:
        report = json.loads(Path(args.from_misses).read_text(encoding="utf-8"))
        cids.extend(
            pc["case_id"]
            for pc in report.get("per_case", [])
            if pc.get("priority", {}).get("match") == "miss"
        )
    cids.extend(args.also)
    cids = list(dict.fromkeys(cids))
    if not cids:
        parser.error("Debe pasar al menos un case_id (o --from-misses).")

    print(f"Reprocessing {len(cids)} cases: {', '.join(cids)}")
    summary = asyncio.run(reanalyze(cids))
    total_in = sum(r.get("tokens_in") or 0 for r in summary)
    total_out = sum(r.get("tokens_out") or 0 for r in summary)
    print(f"\nTokens (subset): in={total_in}  out={total_out}")
    for r in summary:
        print(
            f"  {r['case_id']:12s}  final={r['priority_final']:4s}  "
            f"llm={r['priority_llm']:4s}  math={r['priority_math']:4s}  "
            f"policy={r['priority_policy']}"
        )


if __name__ == "__main__":
    main()
