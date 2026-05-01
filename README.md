# Global66 VoC Intelligence

Sistema de automatización de **Voice of Customer** para fintech: ingesta vía webhook, análisis con LLM (salida JSON estructurada), triaje P1–P4 y escalación. Proyecto de prueba técnica — el evaluador configura su propia API key.

**Repo:** https://github.com/vmuster/global66-businesscase · **PDF en `Entregables/`:** **Business Case VoC** (one-pager ejecutivo) + **Informe VoC** con gráficos (`Informe_VoC.pdf` o `Informe_VoC_dashboard.pdf`). Ambos suelen adjuntarse en el correo junto al enlace (y ZIP si aplica).

**Documentación:** [`Entregables/README_ENTREGA.md`](Entregables/README_ENTREGA.md) (guía principal) · **Manual paso a paso:** [`Entregables/code_source/GUIA_EVALUADOR.md`](Entregables/code_source/GUIA_EVALUADOR.md) · **Colab:** [`Entregables/notebook_colab/run_in_colab.ipynb`](Entregables/notebook_colab/run_in_colab.ipynb)

## Requisitos

- Python 3.11+
- API key de **Google Gemini**, **OpenAI** o **Anthropic** (ver `.env.example`)

## Instalación rápida

```bash
python -m venv venv
# Windows: venv\Scripts\activate  |  Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # en Windows: copy .env.example .env
```

Edite `.env`: `LLM_PROVIDER` y la variable de key correspondiente.

## Comandos esenciales

**Batch histórico** (Excel en `data/`):

```bash
python scripts/process_batch.py
```

**API webhook** (local):

```bash
uvicorn src.api.main:app --reload --port 8000
```

**Dashboard:**

```bash
streamlit run src/dashboard/app.py
```

**Postman / Insomnia:** `postman/voc_collection.json` o `Entregables/voc_postman_collection.json`.

**Trazabilidad incluida (sin re-ejecutar batch):** `Entregables/data_outputs/traceability_dataset.csv`, `results_audit.json`, `cost_report.json`.

## Estructura principal

- `src/` — API FastAPI, motor LLM, dashboards
- `scripts/` — batch, evaluación, utilidades
- `config/scoring.yaml` — reglas de scoring
- `Entregables/` — prompt en texto, **Business Case VoC** (PDF one-pager), **Informe VoC** (PDF con gráficos), colección HTTP, trazabilidad (`data_outputs/`), notebook Colab

**One-pager (negocio):** `Entregables/Business Case VoC Global66 - Vicente Muster.pdf`. **Informe (gráficos):** `Entregables/Informe_VoC.pdf` (o `Informe_VoC_dashboard.pdf`).

## Licencia y uso

Material para evaluación técnica Global66. **No** incluir claves reales en el repositorio; use solo `.env` local (ignorado por git).
