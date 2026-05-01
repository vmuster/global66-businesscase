# Global66 — VoC Intelligence | Entrega

**Autor:** Vicente Muster
**Fecha:** Mayo 2026

---

Este repositorio contiene el sistema **Voice-of-Customer** para la prueba técnica: batch histórico + API webhook, análisis LLM multilingüe, triaje P1–P4 y dashboard. Las siguientes secciones indican cómo ejecutarlo y qué contiene cada carpeta.

**Prioridad en runtime:** por defecto se usa la prioridad del modelo (`priority_llm`). La fórmula en `config/scoring.yaml` es **red de seguridad** cuando el modelo asignó p3/p4 y existe riesgo severo objetivo (fraude/AML/cargo no autorizado con confianza alta, riesgo humano o emergencia explícita). La respuesta del webhook incluye `priority_breakdown` (math, llm, final, política).

**Repositorio público de entrega:** [github.com/vmuster/global66-businesscase](https://github.com/vmuster/global66-businesscase). Ahí está el **paquete mínimo para el jurado** (código, datos del caso, `Entregables/` con trazabilidad, **PDF** del one-pager, HTML fuente, Colab, Postman, `GUIA_EVALUADOR.md`). En una copia local de autor pueden existir además `PAQUETE_EVALUACION_GLOBAL66.md`, `COMO_SUBIR_GITHUB_Y_OPCIONES.md` o carpetas `docs/`: no forman parte de ese remoto.

**Sincronización con el jurado (repo de desarrollo):** los números en `Entregables/data_outputs/` no se actualizan solos al editar código; tras un batch estable ejecute `python scripts/post_batch_entrega.py` en su workspace completo. Checklist extendido: `docs/REVISION_FINAL_ENTREGA.md` (solo en repo de trabajo).

---

## Qué hace el sistema, en una frase

Recibe mensajes (webhook o batch Excel), los analiza con un LLM respetando el idioma del hilo, calcula un score auditable y decide escalación según la política anterior. La latencia y el costo exactos del último run están en `Entregables/data_outputs/cost_report.json` (valores orientativos previos: ~0,00032 USD/mensaje en Gemini Flash-Lite de pago).

---

## Qué hay dentro de esta carpeta `Entregables/`

```
Entregables/
├── README_ENTREGA.md                      ← punto de entrada
├── PAQUETE_EVALUACION_GLOBAL66.md         ← checklist: requisitos oficiales → artefactos
├── COMO_SUBIR_GITHUB_Y_OPCIONES.md         ← Git personal, ZIP, Colab (tras build_entrega_package.ps1)
├── One_Pager_Vicente_Muster_source.html   ← versión final (textos acordados)
├── one_pager_final.html                   ← mismo HTML (nombre sugerido para PDF)
├── one_pager_voc.html                     ← mismo HTML (alias)
├── One_Pager_Vicente_Muster.md            ← puntero + cómo generar PDF
├── One_Pager_Vicente_Muster.pdf           ← entregable al jurado (`python scripts/build_onepager_pdf.py`)
├── system_prompt_final.txt                ← prompt activo del análisis
├── voc_postman_collection.json            ← Postman / Insomnia (17 casos)
├── plantilla_README_repo_publico.md       ← usada por build → README.md del paquete público
├── plantilla_gitignore_repo_publico       ← usada por build → .gitignore del paquete público
├── _interno/                               ← notas autor (NO van al paquete público)
├── code_source/
│   ├── README.md                         ← ZIP / clone + mapa de documentos
│   ├── GUIA_EVALUADOR.md                 ← manual paso a paso para el evaluador
│   └── instrucciones_ia_setup.md         ← texto para pegar en un asistente IA
├── notebook_colab/
│   └── run_in_colab.ipynb                 ← Google Colab (ajustar REPO_URL al publicar)
├── data_outputs/                          ← ejecutar `scripts/post_batch_entrega.py` tras el batch
│   ├── cost_report.json                   ← tokens reales + proyección a 100k/mes
│   ├── escalations.jsonl                  ← cola de escalaciones humanas (un JSON por línea)
│   ├── results_audit.json                 ← auditoría completa del batch
│   └── traceability_dataset.csv           ← BBDD original × análisis lado a lado (Excel-friendly)
└── (paquete copiado a ../Global66_entrega_publica/ por scripts/build_entrega_package.ps1)
```

---

## Cómo correrlo (dos rutas)

### Ruta A — Google Colab (sin instalar nada)

> **Ruta rápida** para ver el sistema en unos minutos.  
> Requiere cuenta de Google y una clave de API gratuita de Gemini.

1. Abra https://colab.research.google.com/
2. **File → Upload notebook** → suba `Entregables/notebook_colab/run_in_colab.ipynb` (ruta completa dentro del repo clonado).
   *(Alternativa: abrir desde GitHub directamente — instrucciones en la primera celda del notebook.)*
3. **Runtime → Run all**.
4. Cuando te lo pida, pega tu API key de Gemini (sale ~30 segundos cargarla en https://aistudio.google.com/ → Get API key).
5. El notebook procesa 30 casos del dataset, te muestra el `cost_report.json` y los top-5 casos escalados inline.
6. Para más casos, siga las celdas del cuaderno o ejecute el batch en local según `code_source/GUIA_EVALUADOR.md`.

**Coste:** depende del proveedor y del plan asociado a su clave; consulte la consola de facturación y límites de ese proveedor.

---

### Ruta B — Código fuente local (Python)

> **Instalación local:** Python 3.11 o superior y una clave de API (Gemini, OpenAI o Anthropic).

```bash
# 1. Clonar / descomprimir el repo
git clone https://github.com/<tu-usuario>/global66-voc.git
cd global66-voc

# 2. Crear entorno virtual
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# 3. Instalar dependencias
pip install -r requirements.txt

# 4. Configurar tu API key
cp .env.example .env
# Edita .env y pega tu GEMINI_API_KEY (o OPENAI_API_KEY o ANTHROPIC_API_KEY)

# 5. Procesar el historico (621 filas / 250 casos; reanudación por defecto)
python scripts/process_batch.py --max-cost-usd 2.0

# 6. Exportar trazabilidad y copiar cost_report / escalations a Entregables/data_outputs/
python scripts/post_batch_entrega.py

# 7. Evaluar subconjunto etiquetado (opcional)
python scripts/evaluate.py --review-status APPROVED --review-status EDITED --output reports/eval_entrega_final.json

# 8. Levantar webhook (otra terminal)
uvicorn src.api.main:app --reload --port 8000

# 9. Levantar dashboard (otra terminal)
streamlit run src/dashboard/app.py
```

**Manual de evaluación:** `code_source/GUIA_EVALUADOR.md`. **Texto para asistente IA:** `code_source/instrucciones_ia_setup.md`. **Resumen del repo:** `README.md` en la raíz del clon.

---

## Cómo probar el webhook (Postman, Insomnia o cURL)

Una vez que `uvicorn` esté corriendo en `http://localhost:8000`, tienes tres formas de enviarle solicitudes.

### Con Postman

1. Abre Postman.
2. **File → Import** → arrastra `voc_postman_collection.json`.
3. Configura la variable de entorno `base_url = http://localhost:8000`.
4. Click en cualquier request de la colección → **Send**.

### Con Insomnia

1. Abre Insomnia.
2. Menú principal → **Import** → **From File** → selecciona `voc_postman_collection.json` (Insomnia importa colecciones Postman desde 2023).
3. La variable `base_url` ya viene con `http://localhost:8000`. Si corres el servidor en otro puerto, edítala.
4. Click en cualquier request → **Send**.

### Con cURL (sin instalar nada)

```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "case_id": "TEST-001",
    "message_id": "TEST-001-MSG-1",
    "user_id": "user-test",
    "direction": "INBOUND",
    "text": "Mi transferencia a Colombia lleva 3 días y no llega. Es urgente, necesito el dinero para una cirugía.",
    "pais_usuario": "Chile",
    "platform": "whatsapp"
  }'
```

La respuesta JSON incluye sentimiento, weak_points, regulatory_flags, urgency_signals, escalación, score y **priority_breakdown** (math, llm, final, política y override de hilo resuelto si aplica).

---

## La colección Postman trae 17 casos pre-armados

| # | Caso | Qué valida |
|---|---|---|
| 00 | Health | Servidor vivo + provider activo |
| 01 | Emergencia médica + dinero retenido | P1 + escalación |
| 02 | Cuenta hackeada (fraude) | P1 + `fraud_ops` |
| 03 | Typo intencional ("rovo", "estafasión") | Detección semántica con typos |
| 04-06 | Inglés / portugués / francés | Multilingüe |
| 07-08 | Casos neutros / positivos | NO debe escalar |
| 09 | Reenvío del mismo `message_id` | Idempotencia |
| 10 | Modo asíncrono (`?async=true`) | Encola y responde 202 |
| 11-13 | Solo emojis / mensaje corto / mezcla idiomas | Edge cases |
| 14 | Payload sin `text` | Validación 422 |
| 15-16 | GET case / GET escalations | Auditoría |

---

## Costos exactos y tiempos esperados

| Ruta | Modelo | Tier | Costo aprox. por mensaje | Costo dataset 250 casos | Tiempo dataset 250 |
|---|---|---|---|---|---|
| Free | Gemini 2.5 Flash-Lite | Free (15 RPM) | $0 (cuota gratis) | $0 | ~30 min |
| Paid | Gemini 2.5 Flash-Lite | Paid (4000 RPM) | **$0.000324 USD** | **$0.081 USD** | **~3 min** |
| Paid | OpenAI gpt-4.1-nano | Tier 1 | $0.000301 USD | $0.075 USD | ~3 min |
| Paid | Anthropic Haiku 4.5 | Build | $0.001 USD | $0.25 USD | ~5 min |

**Proyección a 100.000 mensajes/mes con Gemini paid: ~$32 USD/mes.**

> **Tip operativo:** Google regala $300 USD de crédito al activar billing en AI Studio. Eso cubre 9+ meses de operación a 100k/mes.

---

## ¿Cuánto tarda en analizar UN caso?

Medido en el batch real con paid Gemini:
- **Latencia mediana (p50):** 1356 ms (1.4 segundos).
- **Latencia p95:** 2060 ms (2.1 segundos).

O sea: **el 95% de los casos se resuelven en menos de 2.1 segundos**.

---

## Si vas a usar el modelo gratuito (Gemini free), sigue esto

1. Ve a https://aistudio.google.com/ → "Get API key" → "Create API key in new project".
2. Copia la key. Pégala en `.env` en la línea `GEMINI_API_KEY=...`.
3. Deja los rate limits del `.env.example` tal cual (`GEMINI_RATE_LIMIT_RPM=8`).
4. Ejecuta `python scripts/process_batch.py`. Cuenta con ~30 minutos.
5. Si la cuota diaria se agota a mitad de ejecución, el sistema aborta con mensaje claro indicando cuándo suele reiniciarse la cuota gratuita (00:00 hora del Pacífico, EE. UU.).
6. Cuando reinicie, ejecute de nuevo `python scripts/process_batch.py` — el modo resume omite mensajes ya analizados.

## Si vas a usar tu cuenta paga, sigue esto

1. Activa billing en https://aistudio.google.com/ (Google regala $300 USD la primera vez).
2. Pega la key en `.env` igual que en free, pero cambia:
   ```dotenv
   GEMINI_RATE_LIMIT_RPM=200       # paid cap real es 4000 RPM, vas con 95% de margen
   BATCH_CONCURRENCY=8             # 8 workers en paralelo
   MAX_BATCH_COST_USD=1.00         # red de seguridad (el batch real cuesta ~$0.08)
   ```
3. Ejecuta `python scripts/process_batch.py`. En ~3-5 minutos debería terminar.
4. Costo real: **$0.08 USD por todo el dataset**. Cero sustos.

---

## Mini herramienta de labeling (uso interno del autor)

Si abres el dataset etiquetado (`tests/fixtures/labeled_subset.json`) hay una mini app Streamlit pensada para acelerar el review de las 30 etiquetas:

```bash
streamlit run src/dashboard/label_review.py
```

Muestra cada caso con (a) el thread completo del cliente, (b) la etiqueta propuesta por el agente y (c) el análisis ACTUAL del LLM lado a lado. Tiene botones Aprobar / Editar / Rechazar y guarda backup automático antes de cada edición.

## Qué hace el dashboard

```bash
streamlit run src/dashboard/app.py
```

Se abre en `http://localhost:8501`. Tiene 4 tabs y una **banda de Resumen Ejecutivo** arriba con 10 KPIs grandes (volumen, tasa de escalación, costo/mensaje, proyección 100k, latencia p50/p95, fallidos, baja confianza, overrides anti-memoria, tokens totales).

| Tab | Para qué sirve |
|---|---|
| **Operación** | Cola de escalaciones priorizadas + heatmap país × tópico |
| **Producto / Weak Points** | Top de problemas técnicos = entrada directa al roadmap |
| **Salud de Marca** | Sentimiento, emoción, Net Sentiment por país, tasa regulatoria |
| **Costo / Calidad IA** | Tokens, costo, proyección a 100k, breakdown por país y modelo |

Todas las tablas y gráficos vienen con captions explicativas. Si guardó capturas para uso propio, pueden vivir bajo `Entregables/_interno/` (no forman parte del paquete público).

---

## Cosas que validé que funcionan bien (y que también la IA hace mal a veces)

✅ **Multilingüe semántico** (no por keywords): detecta fraude en español, portugués, inglés y francés, incluso con typos intencionales (`rovo`, `estafasión`).
✅ **Idempotencia**: reenviar el mismo `message_id` no duplica ni re-cobra tokens.
✅ **Pseudonimización HMAC-SHA256** del `user_id` antes de log o DB.
✅ **Cadena de failover Gemini → OpenAI → Anthropic**: si uno cae, el siguiente toma la carga.
✅ **Input guard**: si llega un payload de prueba (`text="string"` que es el default de Postman/Swagger), el sistema corta antes del LLM y devuelve `confidence=0` sin gastar tokens. **Ya no alucina** en payloads de prueba.
✅ **Override anti-efecto-memoria**: si el último mensaje del cliente fue de gratitud/resolución, el sistema NO escala aunque haya señales históricas de problema. En el batch real esto evitó **18 escalaciones falsas** sobre 250 casos (= 7.2% de waste reducido).

⚠️ **Limitaciones conocidas** (iteración futura):
- Discrepancias de ±1 nivel entre etiquetador humano y modelo en un subconjunto pequeño; métricas en `reports/eval_*.json`.
- `evidence_quote` debe ser sustexto literal; reforzar en prompt si aparece parafraseo puntual.

---

## Estructura del onepager

`One_Pager_Vicente_Muster.md` (y su versión HTML/PDF):
- Problema y solución en 1 página.
- Decisiones técnicas con justificación corta.
- Resultados medidos (no proyectados) del batch real.
- Plan de escalabilidad a 100k/mes.
- Compliance multipaís pragmática.
- Cómo conecta con Soporte y Producto.
- Próximos pasos honestos (lo que NO hicimos y por qué).

---

## Lecturas extras (si quieres profundizar en una decisión específica)

Dentro del repo (Ruta B) hay 8 documentos técnicos en `docs/`:

| Doc | Tema |
|---|---|
| `01_architecture.md` | Arquitectura general |
| `02_data_pipeline.md` | Encoding, idioma, hash, dedup, timestamps sintéticos |
| `03_ai_engine.md` | Multi-LLM, schema Pydantic, retries |
| `04_scoring_and_escalation.md` | Pesos del score y reglas de escalación |
| `05_compliance_multipais.md` | Mapa de regulators (plantilla, etapa avanzada) |
| `06_evaluation_and_kpis.md` | Subset etiquetado de 30 casos + métricas |
| `07_dashboard.md` | Decisiones del dashboard |
| `08_testing_plan.md` | Plan de testing |

---

## Soporte

Si algo no corre, antes de abrir un issue verifica:

1. `python --version` ≥ 3.11.
2. El venv está activo (verás `(venv)` en el prompt).
3. `pip install -r requirements.txt` terminó sin errores.
4. La key del proveedor está pegada y empieza con el prefijo correcto (`AIza…` Gemini, `sk-…` OpenAI, `sk-ant-…` Anthropic).
5. Si tira `Preflight FAILED`, lee el consejo que imprime: ahí aparece cuándo resetea la cuota.

Cualquier duda, mi correo está en el One-Pager.

— Vicente Muster
