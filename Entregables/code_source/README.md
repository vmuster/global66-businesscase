# Código fuente — notas de distribución

Hay dos formas habituales de entregar el proyecto al evaluador: **ZIP** o **repositorio Git público**. En ambos casos el evaluador debe quedar en la **raíz del repo** (donde están `src/`, `scripts/`, `requirements.txt`).

## Documentación para el evaluador (no duplicar en otros sitios)

| Archivo | Uso |
|---------|-----|
| **`GUIA_EVALUADOR.md`** | Manual completo: rutas correctas, API, colección Postman, dashboards, subset y Colab. |
| **`instrucciones_ia_setup.md`** | Párrafo inicial + bloque para **pegar en un asistente IA** (ChatGPT, Claude, etc.) o seguir la guía a mano. |

El **`README.md` en la raíz del repositorio`** es la descripción técnica breve del proyecto.  
El **`Entregables/README_ENTREGA.md`** describe qué contiene la carpeta `Entregables/` (one-pager, datos exportados, notebook, etc.). Los tres se complementan.

## Ruta A — ZIP (ejemplo PowerShell desde la raíz del repo clonado)

Ajuste rutas si su carpeta tiene otro nombre. Incluya el código necesario para ejecutar el sistema; no incluya `.env`, `venv/`, ni bases de datos generadas si no desea compartirlas.

```powershell
Compress-Archive -Path @(
    ".\src",
    ".\scripts",
    ".\config",
    ".\docs",
    ".\postman",
    ".\tests",
    ".\Entregables",
    ".\.env.example",
    ".\.gitignore",
    ".\README.md",
    ".\requirements.txt",
    ".\data\Business tech case 1 - BBDD.xlsx"
) -DestinationPath ".\Entregables\code_source\global66-voc.zip" -Force
```

## Ruta B — Git clone

```bash
git clone https://github.com/<usuario>/<repo>.git
cd <repo>
python -m venv venv
# Windows: venv\Scripts\activate
# macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Luego siga **`GUIA_EVALUADOR.md`** en esta carpeta (o use **`instrucciones_ia_setup.md`** con un asistente IA).
