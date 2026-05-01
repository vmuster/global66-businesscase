# Global66 — VoC Intelligence | Guía de entrega

**Autor:** Vicente Muster · **Fecha:** mayo 2026

Repositorio público para el jurado: **[github.com/vmuster/global66-businesscase](https://github.com/vmuster/global66-businesscase)**.

---

## Qué es este proyecto (una frase)

Recibe mensajes (webhook o batch desde el Excel en `data/`), los analiza con un LLM multilingüe, calcula un score auditable y aplica triaje P1–P4 con política **trust LLM** + red de seguridad (ver `config/scoring.yaml` y la respuesta del webhook: `priority_breakdown`).

---

## Documentación: qué leer primero

| Prioridad | Archivo | Contenido |
|-----------|---------|-----------|
| 1 | **Este archivo** (`Entregables/README_ENTREGA.md`) | Mapa de `Entregables/`, Colab, Postman, costes, dashboard |
| 2 | **`README.md`** (raíz del repo) | Instalación rápida y comandos |
| 3 | **`Entregables/code_source/GUIA_EVALUADOR.md`** | Paso a paso para reproducir pruebas |

Opcional: **`Entregables/code_source/instrucciones_ia_setup.md`** — texto para pegar en un asistente de IA si quiere ayuda con el setup.

---

## Contenido de `Entregables/`

```
Entregables/
├── README_ENTREGA.md                 ← este documento
├── One_Pager_Vicente_Muster.pdf      ← one-pager ejecutivo (único formato entregado aquí)
├── system_prompt_final.txt           ← copia del prompt de análisis
├── voc_postman_collection.json       ← Postman / Insomnia
├── code_source/
│   ├── README.md                     ← puntero corto
│   ├── GUIA_EVALUADOR.md
│   └── instrucciones_ia_setup.md
├── notebook_colab/
│   └── run_in_colab.ipynb
└── data_outputs/                     ← trazabilidad del batch (CSV + JSON + cola)
    ├── traceability_dataset.csv
    ├── results_audit.json
    ├── cost_report.json
    └── escalations.jsonl
```

Tras cambiar código y volver a ejecutar el batch en un workspace completo, regenere estas salidas con `python scripts/post_batch_entrega.py` (ese script existe en el repo de trabajo del autor; en el repo público ya vienen exportadas).

---

## Cómo ejecutarlo

### Ruta A — Google Colab

1. [colab.research.google.com](https://colab.research.google.com/) → **File → Upload notebook** → `Entregables/notebook_colab/run_in_colab.ipynb`
2. **Runtime → Run all**
3. Pegar API key cuando lo pida (p. ej. Gemini en [aistudio.google.com](https://aistudio.google.com/))
4. El notebook procesa una muestra y muestra resultados; para el dataset completo use la ruta local o ejecute `scripts/process_batch.py` desde la raíz del repo.

### Ruta B — Local (Python 3.11+)

```bash
git clone https://github.com/vmuster/global66-businesscase.git
cd global66-businesscase

python -m venv venv
# Windows: venv\Scripts\activate
# macOS/Linux: source venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
# Editar .env: LLM_PROVIDER + API key (GEMINI / OPENAI / ANTHROPIC)

python scripts/process_batch.py --max-cost-usd 2.0

uvicorn src.api.main:app --reload --port 8000
# Otra terminal:
streamlit run src/dashboard/app.py
```

Evaluación opcional del subset etiquetado: `python scripts/evaluate.py --review-status APPROVED --review-status EDITED --output reports/eval_entrega_final.json`

---

## Webhook (Postman, Insomnia o cURL)

Con el servidor en `http://localhost:8000`:

- Importar **`Entregables/voc_postman_collection.json`** (variable `base_url`).
- Ejemplo cURL:

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{"case_id":"TEST-001","message_id":"TEST-001-MSG-1","user_id":"user-test","direction":"INBOUND","text":"Mi transferencia a Colombia lleva 3 días y no llega. Es urgente.","pais_usuario":"Chile","platform":"whatsapp"}'
```

La respuesta incluye análisis estructurado, score y **`priority_breakdown`** (math vs LLM vs política aplicada).

### Casos en la colección (resumen)

Health; emergencia médica + dinero retenido; fraude/cuenta hackeada; typos multilingües; idiomas EN/PT/FR; neutros que no deben escalar; idempotencia; `?async=true`; edge cases; validación 422; GET case / escalations.

---

## Costes y latencia (orientativos)

Valores detallados y proyección a 100k mensajes: `Entregables/data_outputs/cost_report.json`. Referencias típicas (según proveedor y plan): del orden de **~0,0003 USD/mensaje** en modelos económicos; batch de ~250 casos en pago suele ser **centavos** y unos **minutos** con límites altos; tier gratuito más lento (~decenas de minutos).

Latencias de ejemplo (batch real, proveedor rápido): p50 ~1,4 s, p95 ~2,1 s por mensaje.

---

## Dashboard

```bash
streamlit run src/dashboard/app.py
```

Pestañas: Operación (escalaciones), Producto / weak points, Salud de marca, Costo / calidad IA.

---

## Comportamientos verificados (resumen)

- Multilingüe semántico (no solo keywords); idempotencia por `message_id`; pseudonimización de usuario; cadena de failover entre proveedores; **input guard** (payloads tipo `"string"` de Postman sin llamada al LLM); **override de hilo resuelto** (gratitud sin riesgo duro → no escalar).

Limitaciones: posibles desajustes ±1 nivel vs etiqueta humana en un subconjunto pequeño; vigilar que `evidence_quote` sea literal del hilo.

---

## One-pager

Único artefacto en esta carpeta: **`One_Pager_Vicente_Muster.pdf`**.

---

## Si algo no corre

1. `python --version` ≥ 3.11  
2. venv activado  
3. `pip install -r requirements.txt` sin errores  
4. API key correcta en `.env`  
5. Mensajes de preflight/cuota del propio proveedor  

Contacto en el PDF del one-pager.

— Vicente Muster
