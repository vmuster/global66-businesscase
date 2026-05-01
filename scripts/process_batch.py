"""Process the historical BBDD (xlsx) end-to-end.

Reads `data/Business tech case 1 - BBDD.xlsx`, runs the full pipeline + LLM
analysis on every case, persists everything to SQLite and writes:

    data/results_audit.json   — full audit dump (PDF deliverable)
    data/escalations.jsonl    — append-only stream of escalations
    data/cost_report.json     — tokens, costs and projection to 100k/mo

Resilience features (critical for free-tier APIs):

  * `--strategy latest_per_case` (DEFAULT): 1 LLM call per case, on the LATEST
    inbound message. The orchestrator re-renders the FULL thread anyway, so
    analyzing every inbound was redundant work that doubled our quota burn.
    Use `--strategy every_inbound` for the legacy 1-call-per-message behaviour.

  * `--resume` (DEFAULT ON): skip messages that already have a successful
    analysis in SQLite. If the script crashes or the evaluator re-runs it,
    only the missing work is done — no wasted quota.

  * Pre-flight smoke call: before launching workers we test ONE LLM call. If
    quotas are dead, we abort immediately with a helpful message instead of
    spamming N failures.

  * Abort early: as soon as the FallbackLLMClient reports all providers
    cooled down, we stop scheduling new work, persist the partial audit and
    print clear next-steps. No more 400-error spam.

Usage:
    python scripts/process_batch.py
    python scripts/process_batch.py --provider openai --concurrency 4
    python scripts/process_batch.py --limit 30   # smoke test on first 30 cases
    python scripts/process_batch.py --no-resume  # force re-analysis from scratch
    python scripts/process_batch.py --strategy every_inbound
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv()

from src.utils.silence import silence_thirdparty_imports  # noqa: E402
silence_thirdparty_imports()

from src.core import pipeline  # noqa: E402
from src.core.engine import (  # noqa: E402
    AllProvidersExhausted,
    build_client_from_env,
)
from src.core.orchestrator import analyze_and_persist, ingest_message  # noqa: E402
from src.core.pricing import PRICING_USD_PER_1M, price_for  # noqa: E402
from src.core.schema import (  # noqa: E402
    ThreadInput,
    ThreadMessage,
    WebhookPayload,
)
from src.database import db  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DEFAULT_INPUT = PROJECT_ROOT / "data" / "Business tech case 1 - BBDD.xlsx"
DEFAULT_AUDIT = PROJECT_ROOT / "data" / "results_audit.json"
DEFAULT_COST = PROJECT_ROOT / "data" / "cost_report.json"

# Pricing lives in src.core.pricing (shared with dashboard).
_price_for = price_for


def load_dataframe(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path)
    str_cols = ["case_id", "message_id", "user_id", "direction", "text", "pais_usuario"]
    for c in str_cols:
        if c in df.columns:
            df[c] = df[c].apply(pipeline.fix_mojibake)
    df = df.dropna(subset=["case_id", "message_id", "text"])
    return df


def synthesize_timestamps(df: pd.DataFrame) -> dict:
    pairs = list(df[["case_id", "message_id"]].itertuples(index=False, name=None))
    return pipeline.generate_synthetic_timestamps(pairs)


def _select_targets(df: pd.DataFrame, strategy: str) -> pd.DataFrame:
    """Pick which messages to actually send to the LLM.

    `latest_per_case`  → 1 row per case (highest message_id of INBOUND).
                         The orchestrator re-renders the FULL thread on each
                         call, so analyzing the LATEST inbound captures the
                         entire conversational state of the case in 1 call.
                         This is what cuts free-tier RPD usage roughly in half.

    `every_inbound`    → 1 row per INBOUND message (legacy behaviour). Kept
                         because in real-time webhook mode each new inbound
                         IS analyzed as it arrives, and we want the audit to
                         reflect that for fidelity-with-production benchmarks.
    """
    inbound_df = df[df["direction"] == "INBOUND"].copy()
    inbound_df["__sort"] = inbound_df["message_id"].astype(str)
    inbound_df = inbound_df.sort_values(["case_id", "__sort"])

    if strategy == "every_inbound":
        return inbound_df

    # latest_per_case: take the row with highest message_id within each case.
    latest = inbound_df.groupby("case_id", as_index=False).tail(1)
    return latest.sort_values(["case_id", "__sort"]).reset_index(drop=True)


def _existing_analysis_for_audit(message_id: str, case_id: str) -> Optional[dict]:
    """Reconstruct an audit row from a previously-persisted analysis (resume mode)."""
    row = db.get_analysis(message_id)
    if not row or row.get("analysis_status") != "ok":
        return None
    return {
        "case_id": case_id,
        "message_id": message_id,
        "status": "ok",
        "score": {
            "score_base":   row.get("score_base"),
            "score_final":  row.get("score_final"),
            "priority":     row.get("priority"),
        },
        "escalated": row.get("escalation", {}).get("needed", False) if isinstance(row.get("escalation"), dict) else False,
        "tokens_in":  int(row.get("tokens_in") or 0),
        "tokens_out": int(row.get("tokens_out") or 0),
        "latency_ms": int(row.get("latency_ms") or 0),
        "model_used": row.get("model_used"),
        "analysis": {
            "language":         None,  # not stored at top level; downstream consumers use full row from DB
            "sentiment":        {"score": row.get("sentiment_score"), "label": row.get("sentiment_label")},
            "primary_emotion":  row.get("primary_emotion"),
            "weak_points":      row.get("weak_points") or [],
            "regulatory_flags": row.get("regulatory_flags") or [],
            "urgency_signals":  row.get("urgency_signals") or {},
            "escalation":       row.get("escalation") or {},
            "confidence":       row.get("confidence"),
        },
        "_resumed_from_db": True,
    }


async def _preflight_smoke_call(client) -> tuple[bool, Optional[str]]:
    """Make ONE tiny LLM call to verify quotas are alive before launching workers.

    Returns (ok, error_message). If ok is False, caller should abort and tell
    the user when quotas reset (Gemini = midnight Pacific Time).
    """
    probe = ThreadInput(
        case_id="__preflight__",
        country_hint="CL",
        detected_language="es",
        messages=[ThreadMessage(direction="INBOUND", text="Hola, esto es una prueba de salud del sistema.")],
    )
    try:
        await client.analyze(probe)
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


async def run_batch(args: argparse.Namespace) -> dict[str, Any]:
    db.init_db()
    df = load_dataframe(Path(args.input))
    if args.limit:
        keep_cases = df["case_id"].drop_duplicates().head(args.limit).tolist()
        df = df[df["case_id"].isin(keep_cases)].copy()
    logger.info("Loaded %d rows / %d cases.", len(df), df["case_id"].nunique())

    timestamps = synthesize_timestamps(df)
    logger.info("Generated %d synthetic timestamps.", len(timestamps))

    logger.info("Ingesting messages into SQLite...")
    for row in df.itertuples(index=False):
        payload = WebhookPayload(
            case_id=str(row.case_id),
            message_id=str(row.message_id),
            user_id=str(row.user_id),
            direction=str(row.direction),
            text=str(row.text),
            pais_usuario=str(row.pais_usuario) if pd.notna(row.pais_usuario) else None,
            platform="historical_batch",
            timestamp=None,
        )
        ingest_message(
            payload,
            synthetic_ts=True,
            forced_created_at=timestamps.get(payload.message_id),
        )

    targets_df = _select_targets(df, strategy=args.strategy)
    inbound_total = len(df[df["direction"] == "INBOUND"])
    logger.info(
        "Strategy=%s → %d LLM calls planned (out of %d INBOUND total, %d cases).",
        args.strategy, len(targets_df), inbound_total, df["case_id"].nunique(),
    )

    # ── RESUME: skip messages already analyzed successfully. We pre-load their
    # audit rows so the final report still reflects everything we know.
    resumed_results: list[dict[str, Any]] = []
    rows_to_process: list = []
    if args.resume:
        for row in targets_df.itertuples(index=False):
            existing = _existing_analysis_for_audit(str(row.message_id), str(row.case_id))
            if existing is not None:
                resumed_results.append(existing)
            else:
                rows_to_process.append(row)
        if resumed_results:
            logger.info(
                "Resume: %d messages already analyzed in DB — will skip. %d remain.",
                len(resumed_results), len(rows_to_process),
            )
    else:
        rows_to_process = list(targets_df.itertuples(index=False))

    if not rows_to_process:
        logger.info("Nothing to do. All targets already have a successful analysis.")
        results = list(resumed_results)
        return _finalize(
            results=results,
            provider="(resumed)",
            model="(resumed)",
            duration=0.0,
            counter={"done": len(results), "ok": len(results), "failed": 0, "skipped_resume": len(resumed_results)},
            aborted_reason=None,
        )

    client = build_client_from_env(
        provider=args.provider,
        rate_limit_rpm=args.rate_limit_rpm,
    )
    logger.info("Active LLM: provider=%s model=%s", client.provider_name, client.model)

    # ETA estimate (uses the active provider's RPM as a lower bound).
    eta_rpm = args.rate_limit_rpm or float(
        os.getenv(f"{client.provider_name.upper()}_RATE_LIMIT_RPM", "0")
        or os.getenv("LLM_RATE_LIMIT_RPM", "0")
        or 8
    )
    if eta_rpm > 0:
        eta_min = len(rows_to_process) / eta_rpm
        logger.info(
            "Rate limit %.0f RPM (active=%s) → ETA mínimo %.1f min for %d LLM calls.",
            eta_rpm, client.provider_name, eta_min, len(rows_to_process),
        )

    # ── PRE-FLIGHT: catch dead-quota state in 1 call instead of N.
    if args.preflight:
        logger.info("Preflight: testing quotas with a single probe call...")
        ok, err = await _preflight_smoke_call(client)
        if not ok:
            logger.error("Preflight FAILED. Aborting before burning workers. detail=%s", err[:500])
            logger.error(
                "Tip: en cuota gratuita de Gemini el reinicio suele ser 00:00 PT (zona del Pacífico, EE. UU.). "
                "Si OpenAI responde 'insufficient_quota', la clave no tiene saldo de facturación. "
                "Añada créditos o configure ANTHROPIC_API_KEY para extender la cadena de respaldo."
            )
            return _finalize(
                results=list(resumed_results),
                provider=client.provider_name,
                model=client.model,
                duration=0.0,
                counter={"done": 0, "ok": 0, "failed": 0, "skipped_resume": len(resumed_results)},
                aborted_reason=f"preflight_failed: {err[:200]}",
            )
        logger.info("Preflight OK. Launching workers.")

    semaphore = asyncio.Semaphore(args.concurrency)
    results: list[dict[str, Any]] = list(resumed_results)
    total = len(rows_to_process)
    counter = {"done": 0, "ok": 0, "failed": 0, "skipped_resume": len(resumed_results)}
    start = time.perf_counter()
    abort_event = asyncio.Event()
    aborted_reason: Optional[str] = None

    # Budget guard. Tracked per-call against the model that ACTUALLY served
    # each call (not against a single client.model — that breaks under
    # fallback because different rows can hit different models with different
    # prices). CIRCUIT BREAKER, not forecast: runs under budget never notice.
    cost_state = {"spent_usd": 0.0}

    def _add_cost(tokens_in: int, tokens_out: int, model_used: Optional[str]) -> float:
        price = _price_for(model_used or "unknown")
        usd = (tokens_in / 1_000_000) * price["in"] + (tokens_out / 1_000_000) * price["out"]
        cost_state["spent_usd"] += usd
        return cost_state["spent_usd"]

    async def worker(row) -> None:
        nonlocal aborted_reason
        if abort_event.is_set():
            # Fast-skip remaining workers without making any LLM call.
            return
        async with semaphore:
            if abort_event.is_set():
                return
            try:
                meta = {
                    "user_pseudonym": pipeline.pseudonymize_user_id(str(row.user_id)),
                    "country_iso": pipeline.country_to_iso(
                        str(row.pais_usuario) if pd.notna(row.pais_usuario) else None
                    ),
                    "language": pipeline.detect_language(pipeline.normalize_text(str(row.text))),
                }
                result = await analyze_and_persist(
                    client=client,
                    case_id=str(row.case_id),
                    message_id=str(row.message_id),
                    user_pseudonym=meta["user_pseudonym"],
                    country_iso=meta["country_iso"],
                    detected_language=meta["language"],
                )
                results.append(
                    {
                        "case_id": row.case_id,
                        "message_id": row.message_id,
                        "status": result["status"],
                        "score": result["score"],
                        "escalated": result["escalated"],
                        "tokens_in": result["tokens"]["in"],
                        "tokens_out": result["tokens"]["out"],
                        "latency_ms": result["latency_ms"],
                        "model_used": result["model_used"],
                        "analysis": result["analysis"].model_dump() if result["analysis"] else None,
                    }
                )
                counter["ok" if result["status"] == "ok" else "failed"] += 1

                if result["status"] == "ok":
                    spent = _add_cost(
                        result["tokens"]["in"],
                        result["tokens"]["out"],
                        result.get("model_used"),
                    )
                    if spent > args.max_cost_usd and not abort_event.is_set():
                        logger.error(
                            "ABORT: budget cap reached. Spent $%.4f > MAX_BATCH_COST_USD=$%.4f. "
                            "Stopping new work to protect billing.",
                            spent, args.max_cost_usd,
                        )
                        aborted_reason = "budget_cap_exceeded"
                        abort_event.set()
            except AllProvidersExhausted as e:
                if not abort_event.is_set():
                    logger.error(
                        "ABORT: every provider in the chain is in cooldown. "
                        "Stopping new work. Done so far: %d/%d ok. Chain: %s",
                        counter["ok"], total, e.chain_description,
                    )
                    aborted_reason = "all_providers_cooled_down"
                    abort_event.set()
                counter["failed"] += 1
                results.append({
                    "case_id": row.case_id,
                    "message_id": row.message_id,
                    "status": "failed_llm",
                    "error": str(e)[:500],
                })
            except Exception as e:
                logger.exception("Worker crashed on message %s: %s", row.message_id, e)
                counter["failed"] += 1
                results.append({
                    "case_id": row.case_id,
                    "message_id": row.message_id,
                    "status": "failed_llm",
                    "error": str(e)[:500],
                })
            finally:
                counter["done"] += 1
                if (
                    counter["done"] % 5 == 0
                    or counter["done"] == total
                    or counter["done"] == 1
                ):
                    elapsed = time.perf_counter() - start
                    rate = counter["done"] / elapsed if elapsed > 0 else 0
                    eta_seconds = (total - counter["done"]) / rate if rate > 0 else 0
                    logger.info(
                        "Progress: %d/%d (ok=%d, failed=%d, resumed=%d) — %.2f msg/s — ETA %.1fmin",
                        counter["done"], total, counter["ok"], counter["failed"],
                        counter["skipped_resume"], rate, eta_seconds / 60,
                    )

    try:
        await asyncio.gather(*(worker(row) for row in rows_to_process))
    except KeyboardInterrupt:
        aborted_reason = "keyboard_interrupt"
        logger.warning("Interrupted by user. Saving partial audit...")

    duration = time.perf_counter() - start
    return _finalize(
        results=results,
        provider=client.provider_name,
        model=client.model,
        duration=duration,
        counter=counter,
        aborted_reason=aborted_reason,
    )


def _finalize(
    *,
    results: list[dict[str, Any]],
    provider: str,
    model: str,
    duration: float,
    counter: dict,
    aborted_reason: Optional[str],
) -> dict[str, Any]:
    """Write audit + cost report and log the summary. Always called on exit."""
    cost_report = build_cost_report(
        results=results,
        provider=provider,
        model=model,
        duration_seconds=duration,
    )
    if aborted_reason:
        cost_report["aborted_reason"] = aborted_reason

    DEFAULT_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with DEFAULT_AUDIT.open("w", encoding="utf-8") as f:
        json.dump(
            {"results": results, "cost": cost_report, "aborted_reason": aborted_reason},
            f, ensure_ascii=False, indent=2, default=str,
        )
    with DEFAULT_COST.open("w", encoding="utf-8") as f:
        json.dump(cost_report, f, ensure_ascii=False, indent=2)

    total_done = counter.get("done", len(results))
    logger.info("\n──────── DONE ────────")
    if aborted_reason:
        logger.warning("Run aborted: %s", aborted_reason)
    logger.info(
        "Processed:        %d (ok=%d, failed=%d, resumed=%d)",
        total_done, counter.get("ok", 0), counter.get("failed", 0),
        counter.get("skipped_resume", 0),
    )
    logger.info("Duration:         %.1fs (%.2f msg/s)",
                duration, total_done / duration if duration else 0)
    logger.info("Tokens (in/out):  %d / %d",
                cost_report["tokens_in_total"], cost_report["tokens_out_total"])
    logger.info(
        "Cost (USD):       %.6f total — %.6f / message",
        cost_report["cost_total_usd"], cost_report["cost_per_message_usd"],
    )
    logger.info("Projection 100k/mo: $%.2f USD", cost_report["projection_100k_monthly_usd"])
    logger.info("Audit:            %s", DEFAULT_AUDIT)
    logger.info("Cost report:      %s", DEFAULT_COST)
    if aborted_reason == "all_providers_cooled_down":
        logger.warning(
            "Re-run after quotas reset (Gemini: 00:00 PT). The DB already "
            "has the partial work; --resume (default) will skip what's done."
        )

    return {"results": results, "cost": cost_report, "aborted_reason": aborted_reason}


def build_cost_report(*, results: list, provider: str, model: str, duration_seconds: float) -> dict:
    """Compute cost report aggregating by the model ACTUALLY used per row.

    Why per-row aggregation: in cascade-fallback mode, different rows can be
    served by different providers/models within the same run. Pricing also
    differs across models. Computing cost from a single `model` arg silently
    breaks when:
      (a) the run aborted with all providers in cooldown → `client.model` is
          "n/a" → pricing 0 → cost reports as $0 even with 400k+ tokens used.
      (b) Gemini handled most messages but a few failed over to Anthropic
          (different price tier).

    Todas las filas con status ok cuentan para coste y latencia, incluidas las
    rehidratadas desde SQLite (`_resumed_from_db`): esas filas ya traen
    tokens_in/out y model_used del análisis guardado.
    """
    counted = [r for r in results if r.get("status") == "ok"]
    tokens_in_total = sum(r.get("tokens_in", 0) or 0 for r in counted)
    tokens_out_total = sum(r.get("tokens_out", 0) or 0 for r in counted)
    n_calls = max(1, len(counted))

    cost_by_model: dict[str, dict] = {}
    cost_total = 0.0
    for r in counted:
        m = r.get("model_used") or model or "unknown"
        price = _price_for(m)
        in_t = r.get("tokens_in", 0) or 0
        out_t = r.get("tokens_out", 0) or 0
        row_cost = (in_t / 1_000_000) * price["in"] + (out_t / 1_000_000) * price["out"]
        cost_total += row_cost
        bucket = cost_by_model.setdefault(
            m,
            {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "price_per_1m_usd": price},
        )
        bucket["calls"] += 1
        bucket["tokens_in"] += in_t
        bucket["tokens_out"] += out_t
        bucket["cost_usd"] += row_cost

    for bucket in cost_by_model.values():
        bucket["cost_usd"] = round(bucket["cost_usd"], 6)

    cost_per_msg = cost_total / n_calls

    latencies = sorted(
        r.get("latency_ms", 0) or 0 for r in counted if r.get("latency_ms")
    )
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0

    # Back-compat top-level fields use the dominant model in the run.
    dominant_model = max(cost_by_model.items(), key=lambda kv: kv[1]["calls"])[0] if cost_by_model else (model or "unknown")
    dominant_price = _price_for(dominant_model)

    return {
        "provider": provider,
        "model": dominant_model,
        "messages_processed": len(results),
        "messages_failed": sum(1 for r in results if r.get("status") != "ok"),
        "messages_resumed_from_db": sum(1 for r in results if r.get("_resumed_from_db")),
        "tokens_in_total": tokens_in_total,
        "tokens_out_total": tokens_out_total,
        "tokens_in_avg": round(tokens_in_total / n_calls, 2),
        "tokens_out_avg": round(tokens_out_total / n_calls, 2),
        "price_per_1m_usd": dominant_price,
        "cost_total_usd": round(cost_total, 6),
        "cost_per_message_usd": round(cost_per_msg, 8),
        "projection_100k_monthly_usd": round(cost_per_msg * 100_000, 4),
        "cost_by_model": cost_by_model,
        "p50_latency_ms": p50,
        "p95_latency_ms": p95,
        "duration_seconds": round(duration_seconds, 2),
        "throughput_msgs_per_sec": round(n_calls / duration_seconds, 2) if duration_seconds > 0 else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Process the historical BBDD.")
    parser.add_argument("--input", type=str, default=str(DEFAULT_INPUT))
    parser.add_argument("--provider", type=str, default=None,
                        help="Override LLM_PROVIDER (gemini|openai|anthropic).")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("BATCH_CONCURRENCY", "1")),
        help="Workers concurrentes. En plan gratuito conviene 1 (el límite lo marca el rate limit del proveedor).",
    )
    parser.add_argument(
        "--rate-limit-rpm",
        type=float,
        default=None,
        help=(
            "Override RPM para TODOS los proveedores en la cadena. Si no se "
            "pasa, cada proveedor usa su <PROVIDER>_RATE_LIMIT_RPM del .env "
            "(defaults free-tier safe: gemini=8, openai=3, anthropic=4)."
        ),
    )
    parser.add_argument("--limit", type=int, default=0,
                        help="Smoke test: process only the first N cases.")
    parser.add_argument(
        "--strategy",
        choices=["latest_per_case", "every_inbound"],
        default="latest_per_case",
        help=(
            "latest_per_case (DEFAULT): 1 LLM call per case (final state). "
            "every_inbound: 1 call per INBOUND message (legacy, ~2x more calls)."
        ),
    )
    parser.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Force re-analysis of every target, even if already analyzed in DB.",
    )
    parser.set_defaults(resume=True)
    parser.add_argument(
        "--no-preflight",
        dest="preflight",
        action="store_false",
        help="Skip the 1-call quota smoke test before launching workers.",
    )
    parser.set_defaults(preflight=True)
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=float(os.getenv("MAX_BATCH_COST_USD", "0.50")),
        help=(
            "Circuit breaker: abort if cumulative cost (USD) exceeds this. "
            "Default $0.50 (≈ 6× the expected $0.085 cost of one full run). "
            "Read from MAX_BATCH_COST_USD env var if not passed."
        ),
    )
    args = parser.parse_args()
    asyncio.run(run_batch(args))


if __name__ == "__main__":
    main()
