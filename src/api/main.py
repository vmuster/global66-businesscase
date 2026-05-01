"""FastAPI webhook entry point.

Endpoints:
    GET  /health                     liveness probe
    POST /webhook                    sync analysis (default)
    POST /webhook?async=true         enqueue and return 202
    GET  /case/{case_id}             thread + analyses (audit view)
    GET  /escalations                list of human escalations (paginated)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

load_dotenv()

from src.utils.silence import silence_thirdparty_imports
silence_thirdparty_imports()

from src.core.engine import build_client_from_env
from src.core.orchestrator import analyze_and_persist, ingest_message
from src.core.schema import WebhookPayload
from src.database import db

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    try:
        app.state.llm_client = build_client_from_env()
        logger.info(
            "LLM client ready: provider=%s model=%s",
            app.state.llm_client.provider_name,
            app.state.llm_client.model,
        )
    except Exception as e:
        logger.error("LLM client could not be built: %s. Webhook will fail until fixed.", e)
        app.state.llm_client = None
    yield


app = FastAPI(
    title="Global66 VoC Intelligence",
    version="1.0.0",
    description="Webhook + async analysis for Voice-of-Customer triage.",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    client = getattr(app.state, "llm_client", None)
    return {
        "status": "ok",
        "llm_provider": client.provider_name if client else None,
        "llm_model": client.model if client else None,
    }


def _require_client():
    client = getattr(app.state, "llm_client", None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="LLM client not configured. Check LLM_PROVIDER and the matching API key in .env.",
        )
    return client


@app.post("/webhook")
async def webhook(
    payload: WebhookPayload,
    request: Request,
    async_mode: bool = Query(False, alias="async"),
):
    client = _require_client()

    ingest_meta = ingest_message(payload, synthetic_ts=False)

    if payload.direction == "OUTBOUND":
        return {
            "status": "ok",
            "skipped": True,
            "reason": "OUTBOUND messages are persisted but not analyzed.",
            "message_id": payload.message_id,
            "case_id": payload.case_id,
        }

    if ingest_meta["already_exists"]:
        existing = db.get_analysis(payload.message_id)
        return {
            "status": "ok",
            "duplicate": True,
            "message_id": payload.message_id,
            "case_id": payload.case_id,
            "analysis": existing,
        }

    if async_mode:
        asyncio.create_task(
            _analyze_background(
                client=client,
                payload=payload,
                ingest_meta=ingest_meta,
            )
        )
        return JSONResponse(
            status_code=202,
            content={
                "status": "queued",
                "message_id": payload.message_id,
                "case_id": payload.case_id,
            },
        )

    result = await analyze_and_persist(
        client=client,
        case_id=payload.case_id,
        message_id=payload.message_id,
        user_pseudonym=ingest_meta["user_pseudonym"],
        country_iso=ingest_meta["country_iso"],
        detected_language=ingest_meta["language"],
    )
    return {
        "status": result["status"],
        "case_id": payload.case_id,
        "message_id": payload.message_id,
        "analysis": result["analysis"].model_dump() if result["analysis"] else None,
        "score": result["score"],
        "priority_breakdown": result.get("priority_breakdown"),
        "escalated": result["escalated"],
        "escalation_payload": result["payload"] if result["escalated"] else None,
        "tokens": result["tokens"],
        "model_used": result["model_used"],
        "latency_ms": result["latency_ms"],
    }


async def _analyze_background(*, client, payload: WebhookPayload, ingest_meta: dict):
    try:
        await analyze_and_persist(
            client=client,
            case_id=payload.case_id,
            message_id=payload.message_id,
            user_pseudonym=ingest_meta["user_pseudonym"],
            country_iso=ingest_meta["country_iso"],
            detected_language=ingest_meta["language"],
        )
    except Exception:
        logger.exception("Background analysis failed for message_id=%s", payload.message_id)


@app.get("/case/{case_id}")
async def get_case(case_id: str):
    thread = db.get_thread(case_id)
    if not thread:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found.")

    analyses: list[Optional[dict[str, Any]]] = []
    for row in thread:
        analyses.append(db.get_analysis(row["message_id"]))

    final_score: Optional[float] = None
    final_priority: Optional[str] = None
    for a in reversed(analyses):
        if a and a.get("score_final") is not None:
            final_score = a["score_final"]
            final_priority = a["priority"]
            break

    return {
        "case_id": case_id,
        "messages": [
            {
                "message_id": row["message_id"],
                "direction": row["direction"],
                "text": row["text"],
                "language": row["language"],
                "created_at": row["created_at"],
                "analysis": analyses[i],
            }
            for i, row in enumerate(thread)
        ],
        "case_score_final": final_score,
        "case_priority": final_priority,
    }


_VALID_PRIORITIES = {"p1", "p2", "p3", "p4"}


@app.get("/escalations")
async def list_escalations(
    limit: int = Query(50, ge=1, le=500),
    priority: Optional[str] = Query(None, description="Filtrar por p1, p2, p3 o p4."),
):
    """Lista las últimas escalaciones registradas, opcionalmente filtradas por prioridad."""
    if priority is not None:
        priority = priority.lower()
        if priority not in _VALID_PRIORITIES:
            raise HTTPException(
                status_code=400,
                detail=f"priority must be one of {sorted(_VALID_PRIORITIES)}.",
            )

    from src.database.db import transaction

    with transaction() as conn:
        if priority:
            cur = conn.execute(
                "SELECT * FROM escalations WHERE priority = ? ORDER BY created_at DESC LIMIT ?",
                (priority, limit),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM escalations ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        if r.get("payload_json"):
            try:
                r["payload"] = json.loads(r.pop("payload_json"))
            except json.JSONDecodeError:
                pass
    return {"count": len(rows), "items": rows}
