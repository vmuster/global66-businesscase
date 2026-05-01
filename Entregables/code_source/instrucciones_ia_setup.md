# Instrucciones para pegar en su asistente de IA

**Péguele esto a su asistente de IA** (ChatGPT, Claude, Copilot, etc.) y le ayudará a dejar el entorno listo; **o** siga usted mismo **`GUIA_EVALUADOR.md`** paso a paso para verificar todo manualmente.

---

## Texto listo para copiar y pegar

```
Necesito instalar y probar un proyecto Python llamado Global66 VoC Intelligence.

Contexto:
- Raíz del repositorio: la carpeta donde están requirements.txt, src/, scripts/, data/, Entregables/, archivo .env.example.
- Python 3.11+.
- Debo crear venv, pip install -r requirements.txt, copiar .env.example a .env y poner al menos una API key (GEMINI, OPENAI o ANTHROPIC según el .env).

Objetivos en orden:
1) Ejecutar python scripts/process_batch.py --limit 30 como prueba (o sin --limit si ya tengo cuota), generando data/voc.db.
2) Levantar uvicorn src.api.main:app --reload --port 8000.
3) Importar la colección Postman desde Entregables/voc_postman_collection.json (o postman/voc_collection.json), variable base_url=http://localhost:8000, y enviar GET /health y POST /webhook.
4) Levantar streamlit run src/dashboard/app.py y streamlit run src/dashboard/label_review.py para explorar.
5) No inventes límites de cuota ni créditos gratuitos de terceros; si discuto costes, di que dependen del plan del proveedor que el usuario tenga.

Si algo falla, pide el mensaje de error exacto. Todas las rutas de archivos son relativas a la raíz del repo clonado.
```

---

## Para el evaluador (humano)

1. **`README.md`** en la raíz — instalación y comandos mínimos.  
2. **`Entregables/README_ENTREGA.md`** — qué contiene la entrega y rutas A/B (Colab / local).  
3. **`GUIA_EVALUADOR.md`** — detalle de pruebas, webhook y subset etiquetado.
