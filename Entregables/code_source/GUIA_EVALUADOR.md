# Guía para evaluar — Global66 VoC Intelligence

Manual para **revisar y probar** el entregable: instalación mínima, API, dashboards y verificación de calidad.

**Tiempo orientativo de configuración:** unos minutos (dependiendo de red y descargas).  
**Tiempo del batch completo (~250 llamadas al modelo):** depende del **proveedor de IA**, de los **límites de peticiones** que tenga su cuenta y de la configuración en `.env`; consulte la documentación oficial del proveedor elegido.

> **Ruta con asistente de IA:** puede pegar el contenido de **`instrucciones_ia_setup.md`** (misma carpeta) en ChatGPT, Claude u otro proveedor para que le guíe el setup; esta guía sirve para seguir **paso a paso a mano** o para contrastar.

---

## 1. Estructura del proyecto (desde la raíz del repositorio)

Tras clonar o descomprimir el proyecto, la **raíz** es la carpeta que contiene `requirements.txt`, `src/` y `scripts/`. Referencias como `python scripts/...` se ejecutan **desde ahí**.

```
<raíz-del-repo>/
├── README.md                          Visión general y comandos rápidos
├── requirements.txt
├── .env.example                       Plantilla de configuración (copiar a .env)
├── src/                               Código (API, motor LLM, dashboards)
├── scripts/                           Batch, evaluación, exportación
├── config/                            Reglas de scoring (YAML)
├── data/
│   └── Business tech case 1 - BBDD.xlsx   Dataset histórico de la demo
├── tests/fixtures/
│   └── labeled_subset.json            Subconjunto para etiquetado / evaluación
├── postman/
│   └── voc_collection.json          Colección Postman (copia en Entregables)
├── Entregables/                     Artefactos de entrega y documentación
│   ├── README_ENTREGA.md            Guía principal de la entrega
│   ├── One_Pager_Vicente_Muster.pdf One-pager ejecutivo
│   ├── system_prompt_final.txt      Copia del prompt de análisis
│   ├── voc_postman_collection.json  Misma colección que postman/voc_collection.json
│   ├── data_outputs/                Trazabilidad del batch (CSV + JSON)
│   ├── notebook_colab/
│   │   └── run_in_colab.ipynb      Opción Google Colab
│   └── code_source/
│       ├── README.md                Punteros a esta guía y README_ENTREGA
│       ├── GUIA_EVALUADOR.md        Este documento
│       └── instrucciones_ia_setup.md  Texto para pegar en un asistente IA
```

---

## 2. Requisitos

- **Python 3.11 o superior**
- Al menos **una** clave de API de un proveedor compatible: Google Gemini, OpenAI o Anthropic (según variables en `.env.example`).
- Opcional: **Postman** o **Insomnia** para probar el webhook con la colección incluida.

---

## 3. Instalación

Abra una terminal en la **raíz del repositorio** (no dentro de `Entregables` a menos que solo tenga esa subcarpeta sin el resto del código: en ese caso necesita el repo completo).

**Windows (PowerShell):**

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

**macOS / Linux:**

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edite `.env` y asigne al menos una clave (`GEMINI_API_KEY`, `OPENAI_API_KEY` o `ANTHROPIC_API_KEY`) según el proveedor que vaya a usar. Los límites de velocidad (`*_RATE_LIMIT_RPM`) y el tope de coste del batch (`MAX_BATCH_COST_USD`) deben ajustarse a **su** plan; los valores del ejemplo son orientativos.

---

## 4. Procesar el dataset histórico (batch)

```bash
python scripts/process_batch.py
```

Comportamiento relevante para la evaluación:

- Lee el Excel en `data/` y carga **SQLite** en `data/voc.db`.
- Por defecto usa **reanudación**: no vuelve a llamar al modelo en mensajes ya analizados correctamente.
- Genera `data/results_audit.json`, `data/cost_report.json`, `data/escalations.jsonl`.

Modos útiles:

```bash
python scripts/process_batch.py --limit 30          # Prueba con 30 casos
python scripts/process_batch.py --no-resume         # Recalcula todo (más llamadas y coste)
python scripts/process_batch.py --max-cost-usd 2.0 # Tope de coste del run
```

---

## 5. API HTTP (webhook)

En otra terminal, con el entorno virtual activado:

```bash
uvicorn src.api.main:app --reload --port 8000
```

**Qué debería ver:** líneas de registro indicando que el servidor escucha en `http://127.0.0.1:8000` y que el **cliente LLM** está listo. El **nombre del modelo** y la **cadena de respaldo** dependen de su `.env`; no tiene por qué coincidir literalmente con otra máquina.

| Método | Ruta | Uso |
|--------|------|-----|
| GET | `/health` | Comprobación rápida |
| POST | `/webhook` | Envía un mensaje; respuesta con análisis |
| POST | `/webhook?async=true` | Variante asíncrona |
| GET | `/case/{case_id}` | Hilo y análisis del caso |
| GET | `/escalations` | Cola de escalación |

---

## 6. Probar el webhook con Postman o Insomnia

**Archivo de la colección (el contenido es idéntico en ambos):**

- `Entregables/voc_postman_collection.json`, o  
- `postman/voc_collection.json`

**Postman**

1. **File → Import** → seleccione uno de los archivos anteriores.  
2. En la colección, defina la variable **`base_url`** = `http://localhost:8000` (o el host donde corre `uvicorn`).  
3. Envíe primero **00 — Health**; luego cualquier **POST** de la lista.  

**Insomnia**

1. **Import** → archivo → el mismo JSON (formato colección Postman v2.1).  
2. Configure la variable **`base_url`** igual que arriba.  
3. Ejecute las peticiones en el mismo orden lógico.

**Cuerpo JSON que acepta el webhook:** el esquema coincide con los cuerpos de la colección. Campos:

| Campo | Obligatorio | Notas |
|-------|-------------|--------|
| `case_id` | Sí | Identificador del hilo |
| `message_id` | Sí | Debe ser **único** por mensaje nuevo |
| `user_id` | Sí | Identificador de usuario (puede ser pseudónimo) |
| `direction` | Sí | `"INBOUND"` o `"OUTBOUND"` |
| `text` | Sí | Texto del mensaje |
| `pais_usuario` | No | País (texto libre o normalizado) |
| `platform` | No | Ej. `whatsapp`, `app` |
| `timestamp` | No | ISO 8601; si falta, el servidor asigna hora de recepción |

Para **sus propios casos:** duplique una petición existente, genere `message_id` nuevos y mantenga el mismo formato JSON.

**Línea de comandos (sin Postman):**

```bash
curl -s -X POST http://localhost:8000/webhook -H "Content-Type: application/json" -d "{\"case_id\":\"TEST-001\",\"message_id\":\"TEST-001-MSG-1\",\"user_id\":\"u1\",\"direction\":\"INBOUND\",\"text\":\"Hola, consulta sobre comisiones.\",\"pais_usuario\":\"Chile\",\"platform\":\"app\"}"
```

---

## 7. Dashboard operativo (Streamlit)

```bash
streamlit run src/dashboard/app.py
```

Navegador en `http://localhost:8501`: resumen ejecutivo, cola humana, casos **sin** envío obligatorio a persona, producto, marca y costes.

---

## 8. Revisión de calidad con el subconjunto etiquetable

Objetivo: que **usted** (el evaluador) defina o ajuste criterios de referencia y compare contra el sistema, **no** revisar borradores de otra persona.

1. Ejecute al menos un batch que deje `data/voc.db` con análisis.  
2. Inicie la herramienta de etiquetado:

   ```bash
   streamlit run src/dashboard/label_review.py
   ```

3. Abra casos del fichero `tests/fixtures/labeled_subset.json`: puede **editar** campos de expectativa (prioridad aceptable, flags regulatorios esperados, si debe escalar o no, etc.) y **guardar** desde la interfaz.  
4. Opcionalmente ejecute la evaluación automática frente a lo que haya validado (p. ej. solo estados que use como referencia):

   ```bash
   python scripts/evaluate.py --review-status APPROVED --review-status EDITED --output reports/eval_mia.json
   ```

Los informes en `reports/` resumen diferencias de prioridad, escalación y falsos positivos regulatorios **respecto a sus propias etiquetas** guardadas.

---

## 9. Google Colab (opción sin entorno local)

1. Abra [Google Colab](https://colab.research.google.com/).  
2. **Archivo → Subir cuaderno** y cargue `Entregables/notebook_colab/run_in_colab.ipynb` desde este proyecto.  
3. Ajuste la URL del repositorio en la celda de clonación si fork/clon es distinto.  
4. **Entorno → Ejecutar todo** y siga las instrucciones del cuaderno (clave API, smoke test, etc.).

El cuaderno clona el **repo completo**; no sustituye la guía local si necesita modificar código.

---

## 10. Datos generados útiles para la revisión

```
data/
├── voc.db                 Base SQLite
├── results_audit.json     Auditoría del último batch ejecutado en esta máquina
├── cost_report.json       Agregados de tokens y coste
└── escalations.jsonl      Cola simulada (append)
```

Para alinear con la carpeta `Entregables/data_outputs/` del paquete de entrega, use el script del repo: `python scripts/post_batch_entrega.py`.

---

## 11. Incidencias frecuentes

| Síntoma | Qué revisar |
|---------|-------------|
| Error de API key al arrancar | `.env` activo, variable correcta para el proveedor elegido |
| Preflight / cuota | Límites actuales del proveedor; otro proveedor en cadena si está configurado |
| Dashboard vacío | Ejecutar antes `process_batch.py` para crear `data/voc.db` |
| 503 / errores transitorios | Reintentos del cliente; re-ejecutar más tarde |

---

## 12. Documentación técnica adicional

Profundidad de arquitectura, scoring y cumplimiento: carpeta **`docs/`** en la raíz del repositorio (lectura opcional para la evaluación funcional).
