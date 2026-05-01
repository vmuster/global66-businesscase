"""Streamlit dashboard for the VoC Intelligence demo.

Reads SQLite directly. No background jobs. Cached queries refresh on demand.

Tabs:
  1. Operación        — escalation queue + heatmap país × topic
  2. Producto         — weak points aggregated → backlog
  3. Salud de Marca   — sentiment trends + emotion mix + regulatory rate
  4. Costo / IA       — tokens, cost, latency, confidence, eval metrics
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.silence import silence_thirdparty_imports  # noqa: E402
silence_thirdparty_imports()

from src.core.pricing import call_cost_usd, price_for  # noqa: E402


def _db_path() -> Path:
    url = os.getenv("DATABASE_URL", "sqlite:///data/voc.db")
    if url.startswith("sqlite:///"):
        return PROJECT_ROOT / url.replace("sqlite:///", "", 1)
    return Path(url)


@st.cache_data(ttl=30)
def load_data() -> dict[str, pd.DataFrame]:
    path = _db_path()
    if not path.exists():
        return {}
    with sqlite3.connect(path) as conn:
        cases = pd.read_sql_query("SELECT * FROM cases", conn)
        messages = pd.read_sql_query("SELECT * FROM messages", conn)
        analyses = pd.read_sql_query("SELECT * FROM analyses", conn)
        escalations = pd.read_sql_query("SELECT * FROM escalations", conn)

    for col in ("created_at", "case_started_at"):
        if col in cases.columns:
            cases[col] = pd.to_datetime(cases[col], errors="coerce", utc=True)
    if "created_at" in messages.columns:
        messages["created_at"] = pd.to_datetime(messages["created_at"], errors="coerce", utc=True)
    if "analyzed_at" in analyses.columns:
        analyses["analyzed_at"] = pd.to_datetime(analyses["analyzed_at"], errors="coerce", utc=True)

    for jcol in ("weak_points", "regulatory_flags", "urgency_signals", "escalation"):
        if jcol in analyses.columns:
            analyses[jcol] = analyses[jcol].apply(_safe_json_loads)

    return {
        "cases": cases,
        "messages": messages,
        "analyses": analyses,
        "escalations": escalations,
    }


def _safe_json_loads(s: Any) -> Any:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    if isinstance(s, str):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return None
    return s


def _dedupe_escalations_by_message(escalations: pd.DataFrame) -> pd.DataFrame:
    """Una fila por message_id (la más reciente) para KPIs y cola.

    La tabla permitía duplicados si se reejecutaba `process_batch.py` sin limpiar:
    eso inflaba «Escalados» y podía llevar la tasa por encima del 100 %.
    """
    if escalations.empty or "message_id" not in escalations.columns:
        return escalations
    df = escalations.copy()
    sort_cols: list[str] = []
    if "id" in df.columns:
        sort_cols.append("id")
    elif "created_at" in df.columns:
        df["_ct"] = pd.to_datetime(df["created_at"], errors="coerce")
        sort_cols.append("_ct")
    if not sort_cols:
        return df.drop_duplicates(subset=["message_id"], keep="last")
    df = df.sort_values(sort_cols[0], ascending=True)
    out = df.drop_duplicates(subset=["message_id"], keep="last")
    return out.drop(columns=[c for c in ("_ct",) if c in out.columns], errors="ignore")


def _latency_percentiles_ms(lat_ms: pd.Series) -> tuple[float, float]:
    """p50 y p95 en milisegundos, solo filas > 0 (excluye ceros residuales)."""
    s = lat_ms.dropna()
    s = s[s > 0]
    if s.empty:
        return 0.0, 0.0
    p50 = float(s.median())
    if len(s) == 1:
        return p50, p50
    p95 = float(s.quantile(0.95, interpolation="higher"))
    return p50, max(p95, p50)


def _fmt_latency_metric(p50: float, p95: float) -> tuple[str, str]:
    """Texto legible (segundos) + ayuda con ms y contexto."""
    if p50 <= 0 and p95 <= 0:
        return "—", "Aún no hay mediciones de tiempo de respuesta del modelo en estos datos."
    s50, s95 = p50 / 1000.0, p95 / 1000.0
    main = f"{s50:.1f} s / {s95:.1f} s"
    help_txt = (
        f"Valor central ≈ {p50:,.0f} ms; el 5 % más lento llega a ≈ {p95:,.0f} ms. "
        "Incluye el viaje de red, el cálculo del modelo y las **esperas por reintentos** cuando el servicio va lento o devuelve error temporal. "
        "Por eso el segundo número suele subir en corridas con saturación del API (p. ej. mensajes del tipo «alta demanda») aunque el flujo sea correcto."
    )
    return main, help_txt


def _read_cost_report_flag() -> dict[str, Any]:
    path = PROJECT_ROOT / "data" / "cost_report.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _audience_note_dup_escalations(dup_esc: int) -> str:
    return (
        f"**Nota:** había **{dup_esc}** registros repetidos en la cola del mismo mensaje (suele ocurrir al reprocesar datos). "
        "Para las cifras de arriba se contó solo la versión más reciente por mensaje."
    )


def _audience_note_escalation_rate(
    esc_rate: float,
    n_esc_ok: int,
    n_analyses_ok: int,
    n_esc_ok_p12: int,
    n_esc_ok_p34: int,
) -> str:
    return (
        f"**Qué mide el {esc_rate:.1f} %:** de **{n_analyses_ok}** conversaciones analizadas con éxito, **{n_esc_ok}** fueron enviadas a **revisión humana** "
        f"(prioridad de urgencia: **{n_esc_ok_p12}** en banda alta P1–P2 y **{n_esc_ok_p34}** en P3–P4). "
        "«Urgencia» y «¿necesita persona?» no son lo mismo: un caso puede estar en P3 o P4 y aun así requerir intervención por política o por lo que indicó el modelo. "
        "Un porcentaje alto no significa automáticamente que el motor falle; a menudo refleja un libro de tickets delicado o reglas prudentes."
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Layout
# ──────────────────────────────────────────────────────────────────────────────


def main() -> None:
    st.set_page_config(
        page_title="Global66 VoC Intelligence",
        page_icon="📡",
        layout="wide",
    )
    st.title("Global66 VoC Intelligence")
    st.caption(
        "Clasificación de mensajes de clientes (voz del cliente), priorización y propuesta de escalación. "
        "El histórico de demostración puede incluir fechas generadas solo para análisis de series temporales."
    )

    data = load_data()
    if not data:
        st.warning(
            "No se encontró la base de datos del proyecto. "
            "Hace falta generarla ejecutando el procesamiento por lotes incluido en el repositorio (carpeta `scripts`)."
        )
        return

    cases, messages, analyses, escalations = (
        data["cases"], data["messages"], data["analyses"], data["escalations"],
    )

    # ─────────────────────────────────────────────────────────────────────
    #  Banda Resumen Ejecutivo — KPIs vendibles arriba de TODO
    #  (visible en cualquier tab, especialmente útil para screenshots)
    # ─────────────────────────────────────────────────────────────────────
    render_executive_summary(cases, messages, analyses, escalations)
    st.divider()

    # Sidebar filters
    with st.sidebar:
        st.header("Filtros")

        countries = sorted([c for c in cases.get("country_iso", pd.Series()).dropna().unique()])
        country_filter = st.multiselect("País (ISO)", countries, default=[])

        languages = sorted([l for l in messages.get("language", pd.Series()).dropna().unique()])
        language_filter = st.multiselect("Idioma", languages, default=[])

        models = sorted([m for m in analyses.get("model_used", pd.Series()).dropna().unique()])
        model_filter = st.multiselect("Modelo LLM", models, default=models)

        any_synthetic = (cases.get("synthetic_ts", pd.Series([0]).astype(int)) == 1).any()
        if any_synthetic:
            st.info(
                "Parte de los datos usa fechas generadas para la demo (no provienen del ticket original). "
                "Sirve para gráficos de evolución en el laboratorio."
            )

        st.divider()
        if st.button("Actualizar números desde la base de datos"):
            load_data.clear()
            st.rerun()

    # Apply filters
    f_cases = cases.copy()
    if country_filter:
        f_cases = f_cases[f_cases["country_iso"].isin(country_filter)]
    f_case_ids = set(f_cases["case_id"]) if not f_cases.empty else set(cases["case_id"])

    f_messages = messages[messages["case_id"].isin(f_case_ids)]
    if language_filter:
        f_messages = f_messages[f_messages["language"].isin(language_filter)]

    f_message_ids = set(f_messages["message_id"])
    f_analyses = analyses[analyses["message_id"].isin(f_message_ids)]
    if model_filter:
        f_analyses = f_analyses[f_analyses["model_used"].isin(model_filter)]

    f_escalations = escalations[escalations["case_id"].isin(f_case_ids)]

    # Tabs
    tab_ops, tab_product, tab_brand, tab_cost = st.tabs(
        ["📋 Operación", "🛠 Producto / Weak Points", "💚 Salud de Marca", "💲 Costo / Calidad IA"]
    )

    with tab_ops:
        render_operations(f_cases, f_analyses, f_escalations)

    with tab_product:
        render_product(f_analyses)

    with tab_brand:
        render_brand_health(f_messages, f_analyses)

    with tab_cost:
        render_cost(f_analyses, f_messages, f_cases)


# ──────────────────────────────────────────────────────────────────────────────
#  Executive summary band (always visible at the top)
# ──────────────────────────────────────────────────────────────────────────────


def render_executive_summary(
    cases: pd.DataFrame,
    messages: pd.DataFrame,
    analyses: pd.DataFrame,
    escalations: pd.DataFrame,
) -> None:
    """Top-of-page summary intended to be self-explanatory in a single screenshot.

    Shows: volume, escalation rate, cost projection, latency, and the count of
    quality safeguards (resolved-thread overrides + low-confidence analyses).
    """
    n_cases = cases["case_id"].nunique() if "case_id" in cases.columns else 0
    n_msgs = len(messages)
    n_analyses_ok = (
        (analyses["analysis_status"] == "ok").sum() if "analysis_status" in analyses.columns else 0
    )
    esc_raw = len(escalations)
    esc_dedup = _dedupe_escalations_by_message(escalations)
    n_esc = len(esc_dedup)

    if not analyses.empty and "message_id" in analyses.columns and "analysis_status" in analyses.columns:
        ok_ids = analyses.loc[analyses["analysis_status"] == "ok", "message_id"]
        esc_mid = set(esc_dedup["message_id"]) if "message_id" in esc_dedup.columns else set()
        n_esc_ok = int(ok_ids.isin(esc_mid).sum())
        esc_rate = (float(n_esc_ok) / float(n_analyses_ok) * 100.0) if n_analyses_ok else 0.0
        ok_id_set = set(ok_ids.tolist())
        esc_ok_rows = esc_dedup[esc_dedup["message_id"].isin(ok_id_set)] if not esc_dedup.empty else esc_dedup
        if not esc_ok_rows.empty and "priority" in esc_ok_rows.columns:
            n_esc_ok_p12 = int(esc_ok_rows["priority"].isin(["p1", "p2"]).sum())
            n_esc_ok_p34 = int(esc_ok_rows["priority"].isin(["p3", "p4"]).sum())
        else:
            n_esc_ok_p12 = n_esc_ok_p34 = 0
    else:
        esc_rate = 0.0
        n_esc_ok = 0
        n_esc_ok_p12 = n_esc_ok_p34 = 0

    dup_esc = esc_raw - n_esc

    # Cost: live, computed from the same per-row price table the batch uses.
    if not analyses.empty and {"tokens_in", "tokens_out", "model_used"}.issubset(analyses.columns):
        tokens_in = int(analyses["tokens_in"].sum())
        tokens_out = int(analyses["tokens_out"].sum())
        cost = sum(
            call_cost_usd(int(r.tokens_in or 0), int(r.tokens_out or 0), r.model_used)
            for r in analyses.itertuples()
        )
        cost_per_msg = (cost / n_analyses_ok) if n_analyses_ok else 0
        proj_100k = cost_per_msg * 100_000
        # Latencia: solo filas con llamada al modelo (no input_guard).
        lat_col = analyses["latency_ms"]
        if "model_used" in analyses.columns:
            mask_llm = analyses["model_used"].fillna("") != "input_guard"
            lat_series = analyses.loc[mask_llm, "latency_ms"]
        else:
            lat_series = lat_col
        p50, p95 = _latency_percentiles_ms(lat_series)
    else:
        tokens_in = tokens_out = 0
        cost = cost_per_msg = proj_100k = 0
        p50 = p95 = 0

    # Resolved-thread overrides: count cases where the math/LLM said escalate
    # but our anti-memory-effect guard clamped to p4. Read from the audit JSON.
    audit_path = PROJECT_ROOT / "data" / "results_audit.json"
    overrides_count = 0
    if audit_path.exists():
        try:
            with audit_path.open("r", encoding="utf-8") as f:
                audit = json.load(f)
            results = audit.get("results", []) if isinstance(audit, dict) else audit
            for r in results:
                score = r.get("score") or {}
                if score.get("resolved_override"):
                    overrides_count += 1
        except json.JSONDecodeError:
            pass

    low_conf = (
        (analyses["confidence"].fillna(0) < 0.5).sum() if "confidence" in analyses.columns else 0
    )
    failed = (
        (analyses["analysis_status"] != "ok").sum() if "analysis_status" in analyses.columns else 0
    )

    st.markdown("### Resumen ejecutivo")
    cols = st.columns(6)
    cols[0].metric(
        "Casos en la base",
        f"{n_cases:,}",
        help="Cantidad de conversaciones (hilos) distintas.",
    )
    cols[1].metric(
        "Mensajes guardados",
        f"{n_msgs:,}",
        help="Total de mensajes en el histórico; suele superar el número de análisis con modelo.",
    )
    cols[2].metric(
        "Enviados a revisión humana",
        f"{esc_rate:.1f}%",
        help="De los análisis que salieron bien, qué fracción abrió una tarea para una persona (cualquier nivel P1–P4). "
        "Detalle en el texto debajo del cuadro.",
    )
    cols[3].metric(
        "Costo medio por análisis",
        f"${cost_per_msg:.6f}",
        help="Dólares estimados por cada llamada al modelo según consumo de tokens.",
    )
    cols[4].metric(
        "Proyección a 100 mil/mes",
        f"${proj_100k:,.2f}",
        help="Orden de magnitud mensual si el volumen fuera 100.000 análisis con el mismo coste medio.",
    )
    _lat_val, _lat_help = _fmt_latency_metric(p50, p95)
    cols[5].metric(
        "Tiempo del modelo (típico / cola lenta)",
        _lat_val,
        help=_lat_help,
    )

    cols2 = st.columns(4)
    cols2[0].metric(
        "Análisis con error",
        int(failed),
        help="No se obtuvo resultado válido; normalmente se deriva a soporte.",
    )
    cols2[1].metric(
        "Baja confianza",
        int(low_conf),
        help="El sistema indicó explícitamente poca seguridad en la respuesta.",
    )
    cols2[2].metric(
        "Cierre confirmado por el cliente",
        overrides_count,
        help="El cliente dio el tema por resuelto en el último mensaje; se evitó enviar a persona solo por problemas antiguos del hilo.",
    )
    cols2[3].metric(
        "Volumen de tokens",
        f"{(tokens_in + tokens_out):,}",
        help="Texto enviado y recibido hacia el modelo; base habitual de facturación.",
    )

    cost_snap = _read_cost_report_flag()
    if p50 > 0 and (cost_snap.get("aborted_reason") or "").strip():
        st.info(
            "En el último procesamiento por **lotes** consta que la corrida terminó antes de tiempo por límites o saturación del proveedor. "
            "Eso suele **subir mucho el tiempo del lado lento** (el segundo valor arriba), porque cada reintento suma segundos de espera. "
            "El valor **típico** (primero) suele estar más cercano a la experiencia normal cuando el servicio responde estable."
        )

    if dup_esc > 0:
        st.caption(_audience_note_dup_escalations(dup_esc))

    if n_analyses_ok > 0:
        st.caption(
            _audience_note_escalation_rate(
                esc_rate, n_esc_ok, n_analyses_ok, n_esc_ok_p12, n_esc_ok_p34
            )
        )


# ──────────────────────────────────────────────────────────────────────────────
#  Tabs
# ──────────────────────────────────────────────────────────────────────────────


def _row_escalation_dict(esc: Any) -> dict[str, Any]:
    if esc is None or (isinstance(esc, float) and pd.isna(esc)):
        return {}
    if isinstance(esc, dict):
        return esc
    if isinstance(esc, str):
        try:
            return json.loads(esc)
        except json.JSONDecodeError:
            return {}
    return {}


def _explain_why_no_human_queue(esc: dict[str, Any]) -> str:
    """Texto legible: por qué no hay fila en cola humana pese al JSON del modelo."""
    if not esc:
        return "No hay texto de escalación guardado para este mensaje."
    needed = esc.get("needed")
    reason = (esc.get("reason") or "").strip()
    if needed is False:
        intro = "El análisis concluyó que el caso **no requiere intervención humana** con las reglas actuales."
        return f"{intro} {reason}" if reason else intro
    if needed is True:
        intro = (
            "El modelo **sí** sugería revisión por una persona, pero **otra regla del sistema** impidió abrir la cola "
            "(por ejemplo: el cliente dio el tema por cerrado sin riesgo severo, u otra salvaguarda de política)."
        )
        return f"{intro} Lo que indicó el modelo: {reason}" if reason else intro
    return reason or "—"


def render_automation_candidates(
    cases: pd.DataFrame,
    analyses: pd.DataFrame,
    esc_dedup: pd.DataFrame,
) -> None:
    """Casos con análisis OK que no generaron fila en la cola humana."""
    st.subheader("Conversaciones sin revisión humana obligatoria")
    st.caption(
        "Análisis que **sí** terminaron correctamente y **no** abrieron tarea para un agente, con las reglas actuales. "
        "Suelen ser **candidatos** a respuesta automática en el futuro (chat, base de conocimiento, mensajes guiados), "
        "pero **no** son una promesa: solo indica que este motor no pidió intervención humana; negocio y cumplimiento deben validar cada uso."
    )
    if analyses.empty or "analysis_status" not in analyses.columns:
        st.info("No hay análisis con los filtros aplicados.")
        return

    ok_df = analyses[analyses["analysis_status"] == "ok"].copy()
    esc_ids: set[str] = set()
    if not esc_dedup.empty and "message_id" in esc_dedup.columns:
        esc_ids = set(esc_dedup["message_id"].astype(str))

    ok_df["_mid"] = ok_df["message_id"].astype(str)
    with_queue = ok_df["_mid"].isin(esc_ids).sum()
    auto_df = ok_df[~ok_df["_mid"].isin(esc_ids)].drop(columns=["_mid"])

    c1, c2, c3 = st.columns(3)
    c1.metric(
        "Sin cola humana",
        int(len(auto_df)),
        help="Análisis exitosos que no generaron tarea para persona.",
    )
    c2.metric(
        "Con cola humana",
        int(with_queue),
        help="Análisis exitosos que sí generaron tarea para persona.",
    )
    c3.metric("Análisis exitosos (total)", int(len(ok_df)))

    if auto_df.empty:
        st.info("Ningún caso cumple este criterio con los filtros actuales.")
        return

    mid_to_cid = _msg_to_case_factory(analyses)
    auto_df = auto_df.copy()
    auto_df["case_id"] = auto_df["message_id"].map(mid_to_cid)
    merge_cols = [c for c in ("case_id", "country_iso") if c in cases.columns]
    if merge_cols:
        auto_df = auto_df.merge(cases[merge_cols], on="case_id", how="left")

    auto_df["por_que_sin_cola"] = auto_df["escalation"].apply(
        lambda e: _explain_why_no_human_queue(_row_escalation_dict(e))
    )

    display_cols = [
        c
        for c in (
            "case_id",
            "message_id",
            "country_iso",
            "priority",
            "sentiment_label",
            "score_final",
            "confidence",
            "model_used",
            "por_que_sin_cola",
        )
        if c in auto_df.columns
    ]
    view = auto_df[display_cols].sort_values(
        ["case_id", "message_id"] if "case_id" in auto_df.columns else ["message_id"]
    )
    view_show = view.head(300).rename(
        columns={"por_que_sin_cola": "Motivo (sin cola humana)"}
    )
    st.dataframe(view_show, use_container_width=True, hide_index=True)
    st.caption(
        "**Motivo (sin cola humana):** combina la decisión del modelo con su frase de justificación (como la razón en la cola humana). "
        "Si el modelo pidiera persona pero no hay cola, se indica que una **regla del producto** lo impidió. "
        f"Muestra de hasta **300** de **{len(view)}** filas; use filtros en el panel izquierdo."
    )


def render_operations(cases: pd.DataFrame, analyses: pd.DataFrame, escalations: pd.DataFrame) -> None:
    n_cases = len(cases)
    n_analyses = len(analyses)
    esc_dedup = _dedupe_escalations_by_message(escalations)
    n_esc = len(esc_dedup)
    p1 = (esc_dedup["priority"] == "p1").sum() if "priority" in esc_dedup.columns else 0
    p2 = (esc_dedup["priority"] == "p2").sum() if "priority" in esc_dedup.columns else 0

    cols = st.columns(5)
    cols[0].metric(
        "Casos (tras filtros)",
        n_cases,
        help="Conversaciones que pasan el filtro de país u otros criterios del panel lateral.",
    )
    cols[1].metric(
        "Análisis con modelo",
        n_analyses,
        help="Cantidad de mensajes para los que se ejecutó el análisis automático en esta vista. "
        "En la demo suele haber uno por conversación (último mensaje del cliente).",
    )
    cols[2].metric(
        "En cola para persona",
        n_esc,
        help="Tareas abiertas para revisión humana; cada fila corresponde a un mensaje (sin duplicados por reproceso).",
    )
    cols[3].metric(
        "Urgencia alta (P1) en cola",
        int(p1),
        help="Entre las tareas para persona, cuántas están marcadas como máxima urgencia relativa.",
    )
    cols[4].metric(
        "Urgencia media-alta (P2) en cola",
        int(p2),
        help="Segundo nivel de urgencia en la cola humana.",
    )

    st.caption(
        "Hay más **mensajes** en el histórico que **análisis con modelo**: no todos los mensajes de una conversación se envían al motor, "
        "solo el punto elegido para la demo (típicamente el último del cliente). Las métricas P1/P2 cuentan filas en la cola humana, "
        "incluidos fallos del análisis que generan una tarea de respaldo; por eso pueden diferir del resumen ejecutivo, que separa solo análisis exitosos."
    )

    st.subheader("Cola para revisión humana")
    st.caption(
        "Lista priorizada de lo que el sistema considera que debe atender una persona: equipo sugerido, motivo breve y, al desplegar, el detalle técnico enviado a sistemas externos."
    )
    if esc_dedup.empty:
        st.info("No hay tareas en cola con los filtros actuales.")
    else:
        sortable = esc_dedup.copy()
        sortable["created_at"] = pd.to_datetime(sortable["created_at"], errors="coerce")
        priority_order = {"p1": 0, "p2": 1, "p3": 2, "p4": 3}
        sortable["__pri"] = sortable["priority"].map(priority_order).fillna(99)
        sortable = sortable.sort_values(["__pri", "created_at"], ascending=[True, False])
        view = sortable[
            ["created_at", "case_id", "message_id", "priority", "suggested_team", "reason"]
        ].head(50)
        st.dataframe(view, use_container_width=True, hide_index=True)

        with st.expander("Ver detalle técnico del primer caso de la lista (integración)"):
            try:
                payload = json.loads(sortable.iloc[0]["payload_json"])
                st.json(payload)
            except Exception:
                st.write(sortable.iloc[0]["payload_json"])

    render_automation_candidates(cases, analyses, esc_dedup)

    st.subheader("Mapa país × tema de mejora")
    st.caption(
        "Frecuencia de temas de producto o servicio por país. Ayuda a ver dónde concentrar mejoras operativas o contenidos de ayuda."
    )
    rows = []
    for r in analyses.itertuples():
        wps = getattr(r, "weak_points") or []
        if not wps:
            continue
        for wp in wps:
            rows.append({"message_id": r.message_id, "topic": wp.get("topic"), "severity": wp.get("severity")})
    if not rows:
        st.info("No hay suficientes temas etiquetados para dibujar el mapa.")
        return
    wp_df = pd.DataFrame(rows)
    msg_to_case = dict(zip(analyses["message_id"], analyses["message_id"].map(_msg_to_case_factory(analyses))))
    wp_df["case_id"] = wp_df["message_id"].map(msg_to_case)
    wp_df = wp_df.merge(cases[["case_id", "country_iso"]], on="case_id", how="left")
    pivot = wp_df.pivot_table(index="country_iso", columns="topic", values="severity", aggfunc="count", fill_value=0)
    if not pivot.empty:
        fig = px.imshow(pivot, aspect="auto", color_continuous_scale="Reds", labels=dict(color="frecuencia"))
        st.plotly_chart(fig, use_container_width=True)


def _msg_to_case_factory(analyses: pd.DataFrame):
    """Quick lookup helper.

    We need message_id → case_id, but `analyses` does not contain `case_id`.
    Re-load from the DB (cached).
    """
    @st.cache_data(ttl=30)
    def _build():
        with sqlite3.connect(_db_path()) as conn:
            df = pd.read_sql_query("SELECT message_id, case_id FROM messages", conn)
        return dict(zip(df["message_id"], df["case_id"]))
    return _build()


def render_product(analyses: pd.DataFrame) -> None:
    rows = []
    for r in analyses.itertuples():
        wps = getattr(r, "weak_points") or []
        for wp in wps:
            rows.append(
                {
                    "topic": wp.get("topic"),
                    "specific_issue": wp.get("specific_issue"),
                    "severity": wp.get("severity") or 0,
                    "message_id": r.message_id,
                }
            )
    if not rows:
        st.info("No hay temas de producto detectados en este recorte.")
        return

    wp_df = pd.DataFrame(rows)

    st.subheader("Top topics por frecuencia")
    st.caption("¿Cuáles son los problemas que aparecen MÁS VECES? Volumen puro, sin pesar severidad.")
    freq = wp_df["topic"].value_counts().reset_index()
    freq.columns = ["topic", "count"]
    st.plotly_chart(px.bar(freq, x="topic", y="count"), use_container_width=True)

    st.subheader("Top topics por severidad promedio")
    st.caption(
        "¿Cuáles son los más SEVEROS cuando aparecen? "
        "Un topic puede ser raro pero crítico (ej. fraud_security): aquí saltan a la vista destacados."
    )
    sev = wp_df.groupby("topic", as_index=False)["severity"].mean().sort_values("severity", ascending=False)
    st.plotly_chart(px.bar(sev, x="topic", y="severity"), use_container_width=True)

    st.subheader("Issues específicos (entrada directa para el roadmap de Producto)")
    st.caption(
        "Cada fila es un problema concreto formulado en el lenguaje del cliente. "
        "Ordenado por frecuencia × severidad. Esto reemplaza la 'lectura manual de tickets' "
        "que típicamente toma 2-3 días por mes."
    )
    grouped = (
        wp_df.groupby(["topic", "specific_issue"], as_index=False)
        .agg(occurrences=("message_id", "count"), avg_severity=("severity", "mean"))
        .sort_values(["occurrences", "avg_severity"], ascending=[False, False])
    )
    st.dataframe(grouped.head(50), use_container_width=True, hide_index=True)


def render_brand_health(messages: pd.DataFrame, analyses: pd.DataFrame) -> None:
    if analyses.empty:
        st.info("No hay datos de análisis con los filtros actuales.")
        return

    df = analyses.copy()

    st.subheader("Distribución de sentimiento")
    st.caption(
        "Distribución de los 5 niveles de sentimiento (-1 muy negativo → +1 muy positivo). "
        "El sesgo a negativo es esperable: el dataset son mensajes inbound a soporte, "
        "no encuestas representativas de la base de usuarios."
    )
    if "sentiment_label" in df.columns:
        dist = df["sentiment_label"].value_counts().reset_index()
        dist.columns = ["label", "count"]
        st.plotly_chart(px.bar(dist, x="label", y="count"), use_container_width=True)

    st.subheader("Distribución de emoción primaria")
    st.caption(
        "10 emociones cerradas (anger, frustration, gratitude, confusion, ...). "
        "Útil para ver la 'temperatura emocional' agregada del libro de quejas."
    )
    if "primary_emotion" in df.columns:
        emo = df["primary_emotion"].value_counts().reset_index()
        emo.columns = ["emotion", "count"]
        st.plotly_chart(px.pie(emo, names="emotion", values="count"), use_container_width=True)

    st.subheader("Net Sentiment Score por país")
    st.caption(
        "Promedio del score de sentimiento por jurisdicción. Países más negativos arriba: "
        "ahí conviene focalizar esfuerzo de retención y comunicación proactiva."
    )
    msg_country = messages.merge(
        pd.read_sql_query("SELECT case_id, country_iso FROM cases", sqlite3.connect(_db_path())),
        on="case_id",
        how="left",
    )
    df_with_country = df.merge(msg_country[["message_id", "country_iso"]], on="message_id", how="left")
    if "sentiment_score" in df_with_country.columns and "country_iso" in df_with_country.columns:
        agg = (
            df_with_country.groupby("country_iso", as_index=False)["sentiment_score"]
            .mean()
            .sort_values("sentiment_score")
            .dropna()
        )
        st.plotly_chart(px.bar(agg, x="country_iso", y="sentiment_score"), use_container_width=True)

    st.subheader("Tasa de regulatory_flags")
    st.caption(
        "Porcentaje de mensajes que disparan algún flag regulatorio (fraude, AML, "
        "consumer protection, data privacy, unauthorized charge). "
        "Estos casos son los que el equipo de Compliance recibe en su cola priorizada."
    )
    has_flag = df["regulatory_flags"].apply(lambda x: bool(x) and len(x) > 0)
    rate = has_flag.mean() if not df.empty else 0
    st.metric("Mensajes con flags regulatorios", f"{rate*100:.1f}%")


def render_cost(
    analyses: pd.DataFrame,
    messages: pd.DataFrame,
    cases: pd.DataFrame,
) -> None:
    """Cost / quality tab — reacts to sidebar filters.

    Why this is computed live (not from cost_report.json): the JSON file is
    a snapshot of the LAST batch run as a whole. As soon as the user filters
    by country/language/model the totals are no longer the right answer.
    Computing per-row cost with the SAME pricing table the batch uses keeps
    the two views consistent and lets ops people slice by cost-center.
    """
    if analyses.empty:
        st.info("No hay análisis en este recorte. Afloje o borre filtros en el panel izquierdo.")
        return

    df = analyses.copy()
    # Per-row cost using the model that ACTUALLY served each call.
    df["cost_usd"] = [
        call_cost_usd(int(r.tokens_in or 0), int(r.tokens_out or 0), r.model_used)
        for r in df.itertuples()
    ]

    # Join country (from cases via messages) so we can slice cost by country.
    msg_to_case = messages[["message_id", "case_id", "language"]]
    df = df.merge(msg_to_case, on="message_id", how="left")
    df = df.merge(cases[["case_id", "country_iso"]], on="case_id", how="left")

    total_cost = df["cost_usd"].sum()
    n = len(df)
    cost_per_msg = total_cost / n if n else 0
    proj_100k = cost_per_msg * 100_000

    # ── Headline metrics (REACT to filters)
    cols = st.columns(5)
    cols[0].metric("Mensajes (filtrados)", n)
    cols[1].metric("Costo total (USD)", f"${total_cost:.4f}")
    cols[2].metric("Costo/mensaje (USD)", f"${cost_per_msg:.6f}")
    cols[3].metric("Proyección 100k/mes", f"${proj_100k:,.2f}")
    cols[4].metric("Latencia p50 (ms)", f"{df['latency_ms'].median():.0f}")

    cols2 = st.columns(3)
    cols2[0].metric("Tokens in (avg)", f"{df['tokens_in'].mean():.0f}")
    cols2[1].metric("Tokens out (avg)", f"{df['tokens_out'].mean():.0f}")
    cols2[2].metric("Tokens totales", f"{int(df['tokens_in'].sum() + df['tokens_out'].sum()):,}")

    st.divider()

    # ── Cost imputation by country (the cost-center pie)
    st.subheader("Imputación de costo por país")
    st.caption(
        "Suma del costo USD de todos los análisis del país. Útil para imputar "
        "el gasto de IA al centro de costos del país operativo correspondiente."
    )
    by_country = (
        df.dropna(subset=["country_iso"])
        .groupby("country_iso", as_index=False)
        .agg(
            cost_usd=("cost_usd", "sum"),
            messages=("message_id", "count"),
            tokens_in=("tokens_in", "sum"),
            tokens_out=("tokens_out", "sum"),
        )
        .sort_values("cost_usd", ascending=False)
    )
    if not by_country.empty:
        c1, c2 = st.columns([1, 1])
        with c1:
            fig_pie = px.pie(
                by_country,
                names="country_iso",
                values="cost_usd",
                title="Distribución del costo por país",
                hole=0.35,
            )
            fig_pie.update_traces(textposition="inside", textinfo="percent+label")
            st.plotly_chart(fig_pie, use_container_width=True)
        with c2:
            view = by_country.copy()
            view["cost_usd"] = view["cost_usd"].map(lambda x: f"${x:.4f}")
            view["proj_100k_share"] = (
                by_country["cost_usd"] / by_country["cost_usd"].sum() * proj_100k
            ).map(lambda x: f"${x:,.2f}")
            view = view.rename(
                columns={
                    "country_iso": "país",
                    "cost_usd": "costo run",
                    "messages": "msgs",
                    "tokens_in": "tok in",
                    "tokens_out": "tok out",
                    "proj_100k_share": "share 100k/mes",
                }
            )
            st.dataframe(view, hide_index=True, use_container_width=True)
    else:
        st.info("No hay país asignado en este recorte; revise los filtros.")

    # ── Cost by language (secondary slice)
    st.subheader("Costo por idioma del cliente")
    by_lang = (
        df.dropna(subset=["language"])
        .groupby("language", as_index=False)
        .agg(cost_usd=("cost_usd", "sum"), messages=("message_id", "count"))
        .sort_values("cost_usd", ascending=False)
    )
    if not by_lang.empty:
        st.plotly_chart(
            px.bar(by_lang, x="language", y="cost_usd", text="messages",
                   labels={"cost_usd": "USD", "language": "idioma"}),
            use_container_width=True,
        )

    # ── Cost by model (relevant under cascade fallback when more than one model serves calls)
    by_model = (
        df.dropna(subset=["model_used"])
        .groupby("model_used", as_index=False)
        .agg(
            cost_usd=("cost_usd", "sum"),
            calls=("message_id", "count"),
            tokens_in=("tokens_in", "sum"),
            tokens_out=("tokens_out", "sum"),
        )
        .sort_values("cost_usd", ascending=False)
    )
    if len(by_model) > 1:
        st.subheader("Costo por modelo LLM (cascade fallback)")
        st.caption(
            "Si la cadena Gemini→OpenAI→Anthropic activó failover durante la corrida, "
            "aquí se ve qué porción del costo vino de cada modelo."
        )
        st.dataframe(by_model, hide_index=True, use_container_width=True)

    st.divider()

    # ── Reference: snapshot of last full batch run (static, for context)
    cost_path = PROJECT_ROOT / "data" / "cost_report.json"
    if cost_path.exists():
        with st.expander("Última corrida por lotes (archivo guardado, no cambia con filtros)"):
            st.caption(
                "Resumen guardado en disco de la última ejecución masiva. Las cifras de las tarjetas superiores de esta pestaña sí responden a los filtros del panel lateral."
            )
            with cost_path.open("r", encoding="utf-8") as f:
                cost = json.load(f)
            st.json(cost)

    # ── Quality
    st.divider()
    st.subheader("Señales de calidad (errores y baja confianza)")
    fails = (analyses["analysis_status"] != "ok").sum()
    low_conf = (analyses["confidence"].fillna(0) < 0.5).sum()
    c1, c2 = st.columns(2)
    c1.metric("Análisis fallidos", int(fails))
    c2.metric("Confianza < 0.5", int(low_conf))


if __name__ == "__main__":
    main()
