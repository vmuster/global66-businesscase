"""SQLite adapter. Single source of truth for persistence in the demo.

For production, swap this module for a PostgreSQL-backed equivalent without
touching the rest of the codebase.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_FILE = Path(__file__).parent / "schema.sql"

_lock = threading.Lock()


def _resolve_db_path() -> Path:
    url = os.getenv("DATABASE_URL", "sqlite:///data/voc.db")
    if url.startswith("sqlite:///"):
        return Path(url.replace("sqlite:///", "", 1)).resolve()
    if url.startswith("sqlite://"):
        return Path(url.replace("sqlite://", "", 1)).resolve()
    return Path(url).resolve()


def _connect() -> sqlite3.Connection:
    path = _resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # No detect_types: Python's stdlib timestamp converter does NOT support
    # timezone offsets ("+00:00") and our datetimes are tz-aware (UTC).
    # We persist datetimes as ISO 8601 strings via _to_iso() and parse them
    # back manually only where a real datetime is needed (see get_case_first_inbound_at).
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _to_iso(value):
    """Serialize a datetime to ISO 8601 string. Pass-through for None and already-strings."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def init_db() -> None:
    """Create tables if they don't exist. Safe to call repeatedly."""
    with _lock, _connect() as conn:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.commit()


@contextmanager
def transaction():
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ──────────────────────────────────────────────────────────────────────────────
#  Cases & messages (idempotent inserts)
# ──────────────────────────────────────────────────────────────────────────────


def upsert_case(
    *,
    case_id: str,
    pais_usuario: Optional[str],
    country_iso: Optional[str],
    language: Optional[str],
    case_started_at: Optional[datetime],
    synthetic_ts: bool,
) -> None:
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO cases (case_id, pais_usuario, country_iso, language, case_started_at, synthetic_ts)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(case_id) DO UPDATE SET
                pais_usuario   = COALESCE(excluded.pais_usuario, cases.pais_usuario),
                country_iso    = COALESCE(excluded.country_iso,  cases.country_iso),
                language       = COALESCE(excluded.language,     cases.language),
                case_started_at = COALESCE(cases.case_started_at, excluded.case_started_at),
                synthetic_ts   = excluded.synthetic_ts
            """,
            (case_id, pais_usuario, country_iso, language, _to_iso(case_started_at), int(synthetic_ts)),
        )


def insert_message(
    *,
    message_id: str,
    case_id: str,
    user_pseudonym: str,
    direction: str,
    text: str,
    language: Optional[str],
    platform: str,
    created_at: datetime,
    synthetic_ts: bool,
) -> bool:
    """Insert a message. Returns True if inserted, False if it already existed."""
    with transaction() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO messages
                (message_id, case_id, user_pseudonym, direction, text, language, platform, created_at, synthetic_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                case_id,
                user_pseudonym,
                direction,
                text,
                language,
                platform,
                _to_iso(created_at),
                int(synthetic_ts),
            ),
        )
        return cur.rowcount == 1


def message_exists(message_id: str) -> bool:
    with transaction() as conn:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None


def get_thread(case_id: str) -> list[dict[str, Any]]:
    with transaction() as conn:
        rows = conn.execute(
            """
            SELECT message_id, direction, text, language, created_at
              FROM messages
             WHERE case_id = ?
             ORDER BY created_at, message_id
            """,
            (case_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_case_first_inbound_at(case_id: str) -> Optional[datetime]:
    with transaction() as conn:
        row = conn.execute(
            """
            SELECT MIN(created_at) AS first_at
              FROM messages
             WHERE case_id = ? AND direction = 'INBOUND'
            """,
            (case_id,),
        ).fetchone()
        if row and row["first_at"]:
            return _parse_iso_safe(row["first_at"])
        return None


def _parse_iso_safe(value) -> Optional[datetime]:
    """Tolerant ISO 8601 parser: handles tz-aware, tz-naive, 'Z' suffix and bytes."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


# ──────────────────────────────────────────────────────────────────────────────
#  Analyses
# ──────────────────────────────────────────────────────────────────────────────


def upsert_analysis(
    *,
    message_id: str,
    sentiment_score: Optional[float],
    sentiment_label: Optional[str],
    primary_emotion: Optional[str],
    weak_points: Any,
    regulatory_flags: Any,
    urgency_signals: Any,
    escalation: Any,
    confidence: Optional[float],
    score_base: Optional[float],
    score_final: Optional[float],
    priority: Optional[str],
    analysis_status: str,
    model_used: Optional[str],
    provider: Optional[str],
    tokens_in: int,
    tokens_out: int,
    latency_ms: int,
) -> None:
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO analyses (
                message_id, sentiment_score, sentiment_label, primary_emotion,
                weak_points, regulatory_flags, urgency_signals, escalation,
                confidence, score_base, score_final, priority,
                analysis_status, model_used, provider,
                tokens_in, tokens_out, latency_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                sentiment_score = excluded.sentiment_score,
                sentiment_label = excluded.sentiment_label,
                primary_emotion = excluded.primary_emotion,
                weak_points     = excluded.weak_points,
                regulatory_flags= excluded.regulatory_flags,
                urgency_signals = excluded.urgency_signals,
                escalation      = excluded.escalation,
                confidence      = excluded.confidence,
                score_base      = excluded.score_base,
                score_final     = excluded.score_final,
                priority        = excluded.priority,
                analysis_status = excluded.analysis_status,
                model_used      = excluded.model_used,
                provider        = excluded.provider,
                tokens_in       = excluded.tokens_in,
                tokens_out      = excluded.tokens_out,
                latency_ms      = excluded.latency_ms,
                analyzed_at     = CURRENT_TIMESTAMP
            """,
            (
                message_id,
                sentiment_score,
                sentiment_label,
                primary_emotion,
                json.dumps(weak_points, ensure_ascii=False) if weak_points is not None else None,
                json.dumps(regulatory_flags, ensure_ascii=False) if regulatory_flags is not None else None,
                json.dumps(urgency_signals, ensure_ascii=False) if urgency_signals is not None else None,
                json.dumps(escalation, ensure_ascii=False) if escalation is not None else None,
                confidence,
                score_base,
                score_final,
                priority,
                analysis_status,
                model_used,
                provider,
                tokens_in,
                tokens_out,
                latency_ms,
            ),
        )


def get_analysis(message_id: str) -> Optional[dict[str, Any]]:
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM analyses WHERE message_id = ?", (message_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for key in ("weak_points", "regulatory_flags", "urgency_signals", "escalation"):
            if d.get(key):
                try:
                    d[key] = json.loads(d[key])
                except json.JSONDecodeError:
                    pass
        return d


def delete_escalations_for_message(message_id: str) -> None:
    """Elimina filas de cola para este mensaje.

    Una sola fila viva por `message_id` evita inflar KPIs si se reejecuta el batch
    o el webhook sobre el mismo mensaje. La inserción nueva reemplaza la anterior.
    """
    with transaction() as conn:
        conn.execute("DELETE FROM escalations WHERE message_id = ?", (message_id,))


def insert_escalation(
    *,
    message_id: str,
    case_id: str,
    priority: str,
    reason: str,
    suggested_team: str,
    payload: dict,
) -> int:
    with transaction() as conn:
        conn.execute("DELETE FROM escalations WHERE message_id = ?", (message_id,))
        cur = conn.execute(
            """
            INSERT INTO escalations (message_id, case_id, priority, reason, suggested_team, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message_id, case_id, priority, reason, suggested_team, json.dumps(payload, ensure_ascii=False)),
        )
        return int(cur.lastrowid)
