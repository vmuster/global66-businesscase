"""Mini app Streamlit para revisar y aprobar las etiquetas del labeled_subset.

Pensado para acelerar el paso "owner valida y corrige las etiquetas" del
roadmap de cierre. En vez de editar 283 líneas de JSON a mano, esta app
muestra cada caso con:
  - texto original del thread.
  - etiqueta propuesta por el agente.
  - análisis ACTUAL del LLM (lo que produjo el batch real).
  - botones: Approve / Edit / Reject / Skip.

Cómo usar:
    streamlit run src/dashboard/label_review.py

El archivo `tests/fixtures/labeled_subset.json` se sobrescribe in-place con
los cambios. Backup automático antes de cada edición.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.core.schema import Analysis
from src.core.triage import compute_score
from src.database import db as db_mod

load_dotenv()

LABELED_PATH = PROJECT_ROOT / "tests" / "fixtures" / "labeled_subset.json"
DB_PATH = PROJECT_ROOT / "data" / "voc.db"

PRIORITIES = ["p1", "p2", "p3", "p4"]
TOPICS = [
    "transfers", "kyc_onboarding", "app_stability", "cards", "cash_in_out",
    "fx_currency", "account_management", "fees_pricing", "support_quality",
    "fraud_security", "other",
]
REGULATORY_FLAGS = [
    "fraud_suspected", "aml_suspected", "consumer_protection",
    "data_privacy", "unauthorized_charge", "other",
]
SENTIMENT_LABELS = [
    "very_negative", "negative", "neutral", "positive", "very_positive",
]
URGENCY_SIGNALS = [
    "explicit_emergency", "human_safety_risk", "money_blocked",
    "threat_of_legal_action", "repeated_unanswered_contact",
]


def _load_labels() -> dict:
    return json.loads(LABELED_PATH.read_text(encoding="utf-8"))


def _save_labels(data: dict, note: str) -> None:
    backup_dir = LABELED_PATH.parent / "_backups"
    backup_dir.mkdir(exist_ok=True)
    backup_path = backup_dir / f"labeled_subset.{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
    shutil.copy(LABELED_PATH, backup_path)

    data.setdefault("_meta", {}).setdefault("changelog", []).append({
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": note,
    })

    LABELED_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


@st.cache_data(ttl=10)
def _load_thread_and_analysis(case_id: str) -> dict[str, Any]:
    """Read the thread + the latest analysis for the given case_id from voc.db."""
    if not DB_PATH.exists():
        return {"thread": [], "analysis": None, "score": None}
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        thread_rows = conn.execute(
            """
            SELECT message_id, direction, text, language, created_at
              FROM messages
             WHERE case_id = ?
             ORDER BY created_at, message_id
            """,
            (case_id,),
        ).fetchall()
        thread = [dict(r) for r in thread_rows]

        analysis_row = conn.execute(
            """
            SELECT a.*
              FROM analyses a
              JOIN messages m ON m.message_id = a.message_id
             WHERE m.case_id = ? AND a.analysis_status = 'ok'
             ORDER BY m.created_at DESC
             LIMIT 1
            """,
            (case_id,),
        ).fetchone()
        analysis = dict(analysis_row) if analysis_row else None
        if analysis:
            for k in ("weak_points", "regulatory_flags", "urgency_signals", "escalation"):
                if analysis.get(k):
                    try:
                        analysis[k] = json.loads(analysis[k])
                    except (TypeError, json.JSONDecodeError):
                        pass

    return {"thread": thread, "analysis": analysis}


def _topics_from(weak_points: list[dict]) -> list[str]:
    return sorted({wp.get("topic") for wp in (weak_points or []) if wp.get("topic")})


def _regulatory_types(reg_flags: list[dict]) -> list[str]:
    return sorted({f.get("type") for f in (reg_flags or []) if f.get("type")})


def _urgency_active(signals: dict) -> list[str]:
    if not signals:
        return []
    return [k for k in URGENCY_SIGNALS if signals.get(k) is True]


def _case_language(case_id: str) -> str:
    if not DB_PATH.exists():
        return "es"
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT language FROM cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
    raw = (row["language"] if row and row["language"] else "es") or "es"
    low = str(raw).strip().lower()[:2]
    if low in ("es", "pt", "en", "fr"):
        return low
    return "other"


def _triage_breakdown(case_id: str, analysis: dict | None) -> dict[str, Any] | None:
    """Reconstruye el Analysis y recalcula math vs LLM con el YAML actual (sin re-LLM)."""
    if not analysis:
        return None
    try:
        lang = _case_language(case_id)
        esc = analysis.get("escalation") or {}
        payload = {
            "language": lang,
            "sentiment": {
                "score": float(analysis.get("sentiment_score") or 0.0),
                "label": analysis.get("sentiment_label") or "neutral",
            },
            "primary_emotion": analysis.get("primary_emotion") or "neutral",
            "weak_points": analysis.get("weak_points") or [],
            "regulatory_flags": analysis.get("regulatory_flags") or [],
            "urgency_signals": analysis.get("urgency_signals") or {},
            "escalation": {
                "needed": bool(esc.get("needed")),
                "priority": str(esc.get("priority") or "p4").lower(),
                "reason": esc.get("reason") or "",
                "suggested_human_team": esc.get("suggested_human_team") or "tier1_support",
            },
            "confidence": float(analysis.get("confidence") or 0.0),
        }
        model = Analysis.model_validate(payload)
        first_at = db_mod.get_case_first_inbound_at(case_id)
        score = compute_score(model, first_inbound_at=first_at, now=datetime.now(timezone.utc))
        return {
            "score_final": score["score_final"],
            "priority_math": score["priority_math"],
            "priority_llm": score["priority_llm"],
            "priority_final": score["priority_final"],
            "priority_policy": score.get("priority_policy"),
            "severe_risk_signals": score.get("severe_risk_signals", []),
            "resolved_override": score.get("resolved_override", False),
        }
    except Exception:
        return None


def _classify_priority(expected: list[str], predicted: str | None, priority_llm: str | None) -> str:
    """Devuelve hit / miss_math / miss_llm para colorear la tabla."""
    if not expected:
        return "—"
    if predicted in expected:
        return "hit"
    if priority_llm and priority_llm in expected:
        return "miss_math (LLM acertó ante humano; sistema distinto)"
    return "miss_llm"


def _classify_flags(expected: list[str], predicted: list[str]) -> str:
    exp = set(expected or [])
    pred = set(predicted or [])
    if not exp and not pred:
        return "ok_sin_flags"
    if not exp and pred:
        return f"FP ({', '.join(sorted(pred))})"
    if exp and not pred:
        return "miss (esperaba flags)"
    if exp & pred:
        return "hit"
    return "mismatch"


def _build_comparison_rows(visible_cases: list[tuple[int, dict]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for _, case in visible_cases:
        cid = case["case_id"]
        db_data = _load_thread_and_analysis(cid)
        analysis = db_data["analysis"]
        br = _triage_breakdown(cid, analysis) if analysis else None
        esc = (analysis or {}).get("escalation") or {}

        predicted_final = (br or {}).get("priority_final") or (analysis or {}).get("priority")
        priority_llm = (br or {}).get("priority_llm") or esc.get("priority")
        priority_math = (br or {}).get("priority_math")
        score_final = (br or {}).get("score_final") or (analysis or {}).get("score_final")

        pred_flags = sorted(
            {
                f.get("type")
                for f in ((analysis or {}).get("regulatory_flags") or [])
                if isinstance(f, dict) and f.get("type")
            }
        )
        exp_flags = case.get("expected_regulatory_flag_types", []) or []
        exp_priority = case.get("expected_priority_in", []) or []
        exp_escalate = bool(case.get("expected_should_escalate", False))
        pred_escalate = bool(esc.get("needed")) or predicted_final in {"p1", "p2"}

        rows.append(
            {
                "case_id": cid,
                "review": case.get("review_status", "PENDING"),
                "expected_priority_in": ", ".join(exp_priority) if exp_priority else "—",
                "priority_math": priority_math or "—",
                "priority_llm": priority_llm or "—",
                "priority_final": predicted_final or "—",
                "priority_status": _classify_priority(exp_priority, predicted_final, priority_llm),
                "score_final": round(score_final, 3) if isinstance(score_final, (int, float)) else "—",
                "expected_flags": ", ".join(exp_flags) if exp_flags else "(ninguno)",
                "predicted_flags": ", ".join(pred_flags) if pred_flags else "(ninguno)",
                "flags_status": _classify_flags(exp_flags, pred_flags),
                "esc_human": "sí" if exp_escalate else "no",
                "esc_system": "sí" if pred_escalate else "no",
                "esc_match": "ok" if pred_escalate == exp_escalate else "mismatch",
            }
        )
    return rows


def _render_comparison_table(visible_cases: list[tuple[int, dict]]) -> None:
    st.subheader("Tabla comparativa — humano (`expected_*`) vs sistema (BD post-batch)")
    st.caption(
        "Lo que ves acá usa el `config/scoring.yaml` actual: `priority_math` y `priority_final` se recalculan "
        "desde el análisis ya guardado, sin volver a llamar al LLM. `priority_llm` es lo que dijo el modelo. "
        "Es la vista correcta para **comparar antes y después de recalibrar** sobre el mismo subset."
    )

    rows = _build_comparison_rows(visible_cases)
    if not rows:
        st.info("Sin casos para mostrar.")
        return

    df = pd.DataFrame(rows)

    total = len(df)
    prio_eval = df[df["expected_priority_in"] != "—"]
    hits = (prio_eval["priority_status"] == "hit").sum() if not prio_eval.empty else 0
    miss_math = (prio_eval["priority_status"].str.startswith("miss_math")).sum() if not prio_eval.empty else 0
    miss_llm = (prio_eval["priority_status"] == "miss_llm").sum() if not prio_eval.empty else 0
    fp_flags = df["flags_status"].str.startswith("FP").sum()
    esc_mismatch = (df["esc_match"] == "mismatch").sum()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Casos en vista", total)
    c2.metric(
        "Priority hit",
        f"{hits}/{len(prio_eval)}" if len(prio_eval) else "—",
    )
    c3.metric("Discrepancia math vs humano", int(miss_math), help="Casos donde el LLM coincide con lo esperado pero el sistema no (p. ej. override o política de red de seguridad).")
    c4.metric("Miss por LLM", int(miss_llm), help="Ni `priority_math` ni `priority_llm` aciertan — ir al system_prompt.")
    c5.metric("FP regulatorios", int(fp_flags), help="Casos donde esperabas SIN flags y el sistema metió alguno.")

    only_misses = st.toggle("Mostrar solo discrepancias (priority miss, flags FP, escalation mismatch)", value=False)
    if only_misses:
        mask = (
            (df["priority_status"].astype(str).str.startswith("miss"))
            | (df["flags_status"].astype(str).str.startswith("FP"))
            | (df["flags_status"].astype(str).str.startswith("miss"))
            | (df["esc_match"] == "mismatch")
        )
        df_view = df[mask]
    else:
        df_view = df

    def _row_style(row: pd.Series) -> list[str]:
        color = ""
        if str(row.get("priority_status", "")).startswith("miss_math"):
            color = "background-color: #fff3cd"  # discrepancia sistema vs acuerdo LLM-humano
        elif str(row.get("priority_status", "")) == "miss_llm":
            color = "background-color: #f8d7da"  # rojo claro: culpa del LLM
        elif str(row.get("priority_status", "")) == "hit" and not str(row.get("flags_status", "")).startswith(("FP", "miss")):
            color = "background-color: #d1e7dd"  # verde: todo ok
        return [color] * len(row)

    styled = df_view.style.apply(_row_style, axis=1)
    st.dataframe(styled, use_container_width=True, hide_index=True)

    st.download_button(
        "Descargar CSV de la tabla",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="comparativa_humano_vs_sistema.csv",
        mime="text/csv",
    )


# ──────────────────────────────────────────────────────────────────────────────
#  UI
# ──────────────────────────────────────────────────────────────────────────────


def main() -> None:
    st.set_page_config(
        page_title="Labeled Subset Review — Global66 VoC",
        page_icon="🧪",
        layout="wide",
    )
    st.title("🧪 Revisión del subset etiquetado")
    st.caption(
        "Cada caso: thread, propuesta inicial del agente, y salida persistida del batch (LLM + fórmula). "
        "Aprueba, edita la **verdad de referencia** o rechaza. La edición no modifica el análisis en la BD: "
        "actualiza los campos `expected_*` que usa `scripts/evaluate.py` y el bucle de recalibración (`config/scoring.yaml`)."
    )

    with st.expander("Bucle de mejora continua (para el examinador / operación)", expanded=False):
        st.markdown(
            """
1. **Etiquetar un subset pequeño** (esta app): defines ground truth en `expected_*` (prioridad aceptable, flags regulatorios que *sí* aplican o lista vacía si no debe haber ninguno, escalar sí/no).
2. **`python scripts/evaluate.py --review-status APPROVED --review-status EDITED --output reports/eval_*.json`**: mide humano vs sistema en el subset “útil” (incluye EDITED si corregiste etiquetas pero el caso sigue sirviendo como verdad).
3. **Ajustar `config/scoring.yaml`** y ejecutar `scripts/rescore.py` (sin tokens) para auditar el score numérico; la prioridad final sigue al LLM salvo red de seguridad por riesgo severo (véase `docs/04_scoring_and_escalation.md`).
4. Volver a ejecutar el batch (`process_batch.py`) para repersistir análisis tras cambiar el **prompt** del modelo.
5. Iterar el **prompt** cuando el veredicto del LLM y la etiqueta humana sigan en desacuerdo.

**Confusión típica:** la columna “Análisis actual” muestra lo que el sistema **guardó** (`priority_final = max(math, llm)` salvo override). El formulario “Editar” es lo que **declaras** como correcto para métricas, no necesariamente lo mismo que la columna izquierda si el sistema se equivocó.
            """.strip()
        )

    if not LABELED_PATH.exists():
        st.error(f"No se encuentra `{LABELED_PATH}`.")
        return

    data = _load_labels()
    cases = data.get("cases", [])
    if not cases:
        st.error("El subset está vacío.")
        return

    if not DB_PATH.exists():
        st.warning(
            f"No se encuentra `{DB_PATH}`. Para ver el análisis ACTUAL del LLM por caso, "
            "ejecuta `python scripts/process_batch.py` primero."
        )

    # ── Sidebar: progress + filtros + global save state
    statuses = [c.get("review_status", "PENDING") for c in cases]
    counts = pd.Series(statuses).value_counts().to_dict()
    total = len(cases)
    approved = counts.get("APPROVED", 0)
    edited = counts.get("EDITED", 0)
    rejected = counts.get("REJECTED", 0)
    pending = total - approved - edited - rejected

    with st.sidebar:
        st.header("Progreso")
        st.progress((approved + edited + rejected) / total)
        st.metric("Aprobados", approved)
        st.metric("Editados", edited)
        st.metric("Rechazados", rejected)
        st.metric("Pendientes", pending)
        st.divider()

        filter_status = st.multiselect(
            "Filtrar por estado",
            ["PENDING", "APPROVED", "EDITED", "REJECTED"],
            default=["PENDING"],
        )
        st.caption(
            "Por defecto solo **PENDIENTES**: casos ya aprobados/rechazados/editados salen del foco. "
            "Marca EDITED/APPROVED/REJECTED si quieres revisarlos de nuevo. "
            "Si no marcas ningún estado, se muestran TODOS."
        )

        st.divider()
        view_mode = st.radio(
            "Vista",
            ["Caso a caso (revisar / editar)", "Tabla comparativa (humano vs sistema)"],
            index=0,
            help=(
                "La tabla comparativa es la mejor para revisar de un saque qué cambió "
                "tras recalibrar `config/scoring.yaml` y volver a correr `process_batch.py`."
            ),
        )

        st.divider()
        if st.button("Marcar 'review_status': APPROVED en _meta y guardar"):
            data["_meta"]["review_status"] = "APPROVED_BY_HUMAN"
            data["_meta"]["labeler"] = "vicente_muster"
            data["_meta"]["labeled_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            _save_labels(data, "marked review complete")
            st.success("Subset marcado como APPROVED_BY_HUMAN.")
            st.rerun()

    # ── Filtrar casos
    if filter_status:
        visible_cases = [
            (i, c) for i, c in enumerate(cases)
            if c.get("review_status", "PENDING") in filter_status
        ]
    else:
        visible_cases = list(enumerate(cases))

    if not visible_cases:
        st.success("No hay casos en este filtro. Cambia el filtro en el panel lateral.")
        return

    if view_mode.startswith("Tabla"):
        _render_comparison_table(visible_cases)
        return

    # ── Selector de caso
    options = [
        f"[{c.get('review_status', 'PENDING'):8s}] {c['case_id']} — {c.get('category', '')}"
        for _, c in visible_cases
    ]
    chosen_idx = st.selectbox(
        f"Caso ({len(visible_cases)} visibles de {total})",
        range(len(visible_cases)),
        format_func=lambda i: options[i],
    )
    real_idx, case = visible_cases[chosen_idx]

    case_id = case["case_id"]
    db_data = _load_thread_and_analysis(case_id)
    thread = db_data["thread"]
    analysis = db_data["analysis"]

    # ── Layout 3 columnas: thread | propuesta agente | análisis LLM
    col_thread, col_proposed, col_actual = st.columns([1.3, 1, 1])

    with col_thread:
        st.subheader(f"📜 Thread — {case_id}")
        if not thread:
            st.warning("Sin thread en la BD (¿ejecutaste el batch?).")
        for msg in thread:
            direction = msg["direction"]
            color = "blue" if direction == "INBOUND" else "gray"
            st.markdown(
                f"<div style='border-left: 3px solid {color}; padding: 4px 8px; margin: 4px 0;'>"
                f"<b>[{direction}]</b> <small>{msg.get('language', '')}</small><br>"
                f"{msg['text']}</div>",
                unsafe_allow_html=True,
            )
        if case.get("notes"):
            st.info(f"📝 Notas del agente (al etiquetar): {case['notes']}")

    with col_proposed:
        st.subheader("🤖 Propuesta del agente")
        st.markdown(f"**Categoría:** `{case.get('category', '—')}`")
        st.markdown(f"**Sentimiento esperado:** `{case.get('expected_sentiment_label', '—')}`")
        st.markdown(f"**Topics esperados:** {', '.join(f'`{t}`' for t in case.get('expected_topics', []))}")
        st.markdown(f"**Debe escalar:** {'✅ sí' if case.get('expected_should_escalate') else '⛔ no'}")
        st.markdown(f"**Priority esperada (alguna):** {', '.join(f'`{p}`' for p in case.get('expected_priority_in', []))}")
        if case.get("expected_regulatory_flag_types"):
            st.markdown(f"**Regulatory flags:** {', '.join(f'`{t}`' for t in case['expected_regulatory_flag_types'])}")
        if case.get("expected_urgency_signals"):
            st.markdown(f"**Urgency signals:** {', '.join(f'`{u}`' for u in case['expected_urgency_signals'])}")

    with col_actual:
        st.subheader("📊 Análisis actual del LLM")
        if not analysis:
            st.warning("Sin análisis en la BD para este caso.")
        else:
            st.markdown(f"**Sentimiento real:** `{analysis.get('sentiment_label', '—')}` (score={analysis.get('sentiment_score')})")
            st.markdown(f"**Emoción primaria:** `{analysis.get('primary_emotion', '—')}`")
            actual_topics = _topics_from(analysis.get("weak_points") or [])
            st.markdown(f"**Topics detectados:** {', '.join(f'`{t}`' for t in actual_topics) if actual_topics else '—'}")
            esc = analysis.get("escalation") or {}
            st.markdown(f"**Escalado (LLM `escalation.needed`):** {'✅ sí' if esc.get('needed') else '⛔ no'}")
            br = _triage_breakdown(case_id, analysis)
            if br:
                st.markdown(
                    "**Prioridad (recalculada con `config/scoring.yaml` actual, sin volver a llamar al LLM):**"
                )
                policy_label = {
                    "trust_llm": "trust_llm — la prioridad final es la que dijo el LLM",
                    "math_overrides_due_to_severe_risk": "math_overrides — el LLM bajó pero hay riesgo severo objetivo, la fórmula sube",
                    "resolved_override": "resolved_override — thread cerrado con gratitud, forzado a p4",
                }.get(br.get("priority_policy") or "", br.get("priority_policy") or "—")
                st.markdown(
                    f"- `priority_math` (solo fórmula, auditoría): **`{br['priority_math']}`** — score_final={br['score_final']}\n"
                    f"- `priority_llm` (juicio del modelo): **`{br['priority_llm']}`**\n"
                    f"- `priority_final` persistido: **`{br['priority_final']}`**\n"
                    f"- política aplicada: `{policy_label}`"
                    + (f"\n- riesgos severos detectados: `{', '.join(br['severe_risk_signals'])}`" if br.get("severe_risk_signals") else "")
                )
            else:
                st.markdown(f"**Priority en BD (final):** `{analysis.get('priority', '—')}`")
                st.markdown(f"**Priority en JSON del LLM (`escalation.priority`):** `{esc.get('priority', '—')}`")
            actual_flags = _regulatory_types(analysis.get("regulatory_flags") or [])
            if actual_flags:
                st.markdown(f"**Regulatory flags detectados (sistema):** {', '.join(f'`{t}`' for t in actual_flags)}")
            actual_urgency = _urgency_active(analysis.get("urgency_signals") or {})
            if actual_urgency:
                st.markdown(f"**Urgency signals detectados:** {', '.join(f'`{u}`' for u in actual_urgency)}")
            st.markdown(f"**Confianza:** `{analysis.get('confidence', '—')}`")

    st.divider()

    # ── Acciones por caso
    current_status = case.get("review_status", "PENDING")
    st.markdown(f"**Estado actual de este caso:** `{current_status}`")

    action_cols = st.columns(4)

    with action_cols[0]:
        if st.button("✅ Aprobar", key=f"approve_{case_id}", type="primary", use_container_width=True):
            data["cases"][real_idx]["review_status"] = "APPROVED"
            data["cases"][real_idx]["reviewed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _save_labels(data, f"approved {case_id}")
            st.success(f"{case_id} aprobado.")
            st.rerun()

    with action_cols[1]:
        if st.button("✏️ Editar etiqueta", key=f"edit_{case_id}", use_container_width=True):
            st.session_state[f"editing_{case_id}"] = True

    with action_cols[2]:
        if st.button("⛔ Rechazar (etiqueta inválida)", key=f"reject_{case_id}", use_container_width=True):
            data["cases"][real_idx]["review_status"] = "REJECTED"
            data["cases"][real_idx]["reviewed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _save_labels(data, f"rejected {case_id}")
            st.warning(f"{case_id} rechazado.")
            st.rerun()

    with action_cols[3]:
        if st.button("⏭️ Omitir por ahora (revisar después)", key=f"skip_{case_id}", use_container_width=True):
            st.info("Pasamos al siguiente caso. Sin cambios guardados.")

    # ── Editor inline
    if st.session_state.get(f"editing_{case_id}"):
        st.divider()
        st.subheader("✏️ Editar verdad de referencia (`expected_*` → evaluate.py)")
        st.info(
            "**No puedes “borrar” flags del JSON del LLM desde acá** (eso está en la BD del batch). "
            "Para decir que **no** debían aparecer flags regulatorios: deja **Regulatory flags esperados** vacío. "
            "`evaluate.py` registra entonces los falsos positivos del sistema (véase `false_positive_flags` en el JSON). "
            "Para prioridad: elige en **expected_priority_in** la prioridad correcta (p.ej. solo `p3`); "
            "si el sistema muestra una prioridad distinta a la del modelo y a la suya, documente el caso y use el bucle YAML/prompt descrito arriba; "
            "la corrección del sistema es recalibrar `config/scoring.yaml` o el prompt, no “rechazar” salvo que el caso sea inútil."
        )
        with st.form(key=f"edit_form_{case_id}"):
            new_sentiment = st.selectbox(
                "expected_sentiment_label",
                SENTIMENT_LABELS,
                index=SENTIMENT_LABELS.index(case.get("expected_sentiment_label", "neutral"))
                if case.get("expected_sentiment_label") in SENTIMENT_LABELS else 2,
            )
            new_topics = st.multiselect(
                "expected_topics",
                TOPICS,
                default=case.get("expected_topics", []),
            )
            new_should_escalate = st.checkbox(
                "expected_should_escalate", value=case.get("expected_should_escalate", False)
            )
            new_priority = st.multiselect(
                "expected_priority_in",
                PRIORITIES,
                default=case.get("expected_priority_in", []),
            )
            new_reg_flags = st.multiselect(
                "expected_regulatory_flag_types (vacío = ninguno debía detectarse; penaliza FP en evaluate)",
                REGULATORY_FLAGS,
                default=case.get("expected_regulatory_flag_types", []),
            )
            new_urgency = st.multiselect(
                "expected_urgency_signals",
                URGENCY_SIGNALS,
                default=case.get("expected_urgency_signals", []),
            )
            new_notes = st.text_area("notes", value=case.get("notes", ""), height=100)

            submitted = st.form_submit_button("💾 Guardar edición")
            if submitted:
                data["cases"][real_idx].update({
                    "expected_sentiment_label": new_sentiment,
                    "expected_topics": new_topics,
                    "expected_should_escalate": new_should_escalate,
                    "expected_priority_in": new_priority,
                    "expected_regulatory_flag_types": new_reg_flags,
                    "expected_urgency_signals": new_urgency,
                    "notes": new_notes,
                    "review_status": "EDITED",
                    "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                })
                _save_labels(data, f"edited {case_id}")
                st.session_state[f"editing_{case_id}"] = False
                st.success(f"{case_id} editado.")
                st.rerun()


if __name__ == "__main__":
    main()
