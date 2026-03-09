# Taekwondo WT Scoreboard

Sistema web de marcación para combates de Taekwondo WT con roles, rings, vista de espectador en tiempo real, historial y reporte PDF.

## Características

- Login y roles: `admin`, `professor`, `spectator`.
- Gestión de rings (crear/eliminar).
- Scoreboard en vivo para profesor/admin.
- Vistas separadas:
  - Panel árbitro rojo (`/red/<ring>`)
  - Panel árbitro azul (`/blue/<ring>`)
  - Scoreboard principal (`/score/<ring>`)
  - Espectador (`/spectator/<ring>`)
  - Pantalla TV (`/tv/<ring>`)
- Historial de combates en base de datos.
- Reporte PDF por ring (`/report/<ring>.pdf`).
- Registro:
  - Admin puede registrar `admin` y `professor`.
  - Espectador se registra desde `/register_spectator`.
- Seguridad base:
  - Passwords con hash + salt.
  - Validaciones de input y roles.
  - Headers de seguridad.
  - Control de origen en requests mutables.

## Stack

- Backend: Flask + SQLite
- Frontend: HTML/CSS/JS (vanilla)
- Deploy sugerido: PythonAnywhere

## Requisitos

- Python 3.10+ (recomendado)
- `pip`

## Instalación local

1. Clonar repo:

```bash
git clone <tu-repo>
cd <tu-repo>
```

2. Crear entorno virtual:

```bash
python -m venv .venv
```

3. Activar entorno:

- Windows (PowerShell):

```powershell
.venv\Scripts\Activate.ps1
```

- Linux/macOS:

```bash
source .venv/bin/activate
```

4. Instalar dependencias:

```bash
pip install flask
```

5. Configurar variables de entorno:

- Windows (PowerShell):

```powershell
$env:SECRET_KEY="cambia_esto_por_un_valor_largo_y_seguro"
$env:FLASK_DEBUG="1"
$env:SESSION_COOKIE_SECURE="0"
```

- Linux/macOS:

```bash
export SECRET_KEY="cambia_esto_por_un_valor_largo_y_seguro"
export FLASK_DEBUG="1"
export SESSION_COOKIE_SECURE="0"
```

6. Ejecutar:

```bash
python app.py
```

App disponible en `http://127.0.0.1:5000`.

## Variables de entorno

- `SECRET_KEY` (obligatoria): clave de sesión Flask.
- `FLASK_DEBUG` (opcional): `1` en desarrollo, `0` en producción.
- `SESSION_COOKIE_SECURE` (opcional): `1` en HTTPS/producción, `0` en local HTTP.
- `PROJECT_HOME` (opcional): usado por WSGI en PythonAnywhere.

## Deploy en PythonAnywhere (resumen)

1. Subir código al home de tu cuenta.
2. Crear web app Flask manual.
3. En archivo WSGI, apuntar al proyecto y exportar `application`.
4. Definir `SECRET_KEY` en el archivo WSGI si no usás consola de variables.
5. Recargar web app desde el panel.

Archivo ejemplo incluido:
- `julirexs_pythonanywhere_com_wsgi.py`

## Estructura principal

- `app.py`: backend Flask (API, auth, seguridad, reportes).
- `static/script.js`: sync de estado, acciones, polling, presencia.
- `scoreboard.html`: marcador principal.
- `spectator.html`: vista espectador.
- `tv.html`: vista de pantalla pública del ring.
- `database.db`: base SQLite.

## Endpoints útiles

- `GET /` Home (requiere login)
- `POST /login`
- `POST /logout`
- `GET /rooms`
- `POST /create_room`
- `POST /delete_room/<room>`
- `GET /state/<room>`
- `POST /action/<room>`
- `GET /match_history`
- `GET /report/<room>.pdf`

## Notas de producción

- Si esperás muchas solicitudes concurrentes, evitar estado crítico en memoria para escalar múltiples workers.
- Mantener HTTPS activo y `SESSION_COOKIE_SECURE=1`.
- Hacer backups periódicos de `database.db`.

## Licencia

MIT

