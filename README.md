# chessdotcom_ai_coach

Chess Coach AI — a **Django** web app that lists your live Chess.com games,
renders the board, and asks an Ollama LLM to analyze the position like a
grandmaster coach.

## Stack

- **Django 5** — ORM, templates, admin, session auth (custom `User` model)
- **PostgreSQL** — via `psycopg2-binary`
- **Ollama** — the AI coach prose (`chessdotcom_ai_coach/services/coach.py`)
- **Stockfish** — UCI engine for move evaluation, run as a local subprocess via
  `python-chess` (`chessdotcom_ai_coach/services/coach.py`)
- **Chess.com API** — via `chess-com` (`chessdotcom_ai_coach/services/chess_client.py`)
- **HTMX** — on-demand game loading and coach analysis, vendored via `django-htmx`
- **Alpine.js** — board rendering (loaded from CDN, only on the game page)
- **Gunicorn** — WSGI server in the container
- Custom hand-written CSS theme (no Tailwind) in the `theme` app
  (`theme/static/css/styles.css`)

## Run with Docker

```bash
docker compose up --build
```

The `entrypoint.sh` runs `migrate` and `collectstatic` on start, then serves
with Gunicorn on http://localhost:8000. In Compose the `POSTGRES_HOST` and
`OLLAMA_HOST`/`OLLAMA_PORT` are overridden to reach the `postgres` and `ollama`
services; everything else comes from `.env`. Create a user via the admin (see
below).

## Run locally

Requires Python 3.13+ and a running PostgreSQL (the `postgres` service in
`docker-compose.yaml` works). Configure `.env` — the settings read these keys
(defaults in parentheses):

| Key | Purpose |
| --- | --- |
| `SECRET_KEY` | Django secret key |
| `DEBUG` | `true`/`false` (default `true`) |
| `ALLOWED_HOSTS` | comma-separated hosts (default `*`) |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_HOST` / `POSTGRES_PORT` | database connection (`postgres`/`postgres`/`password`/`localhost`/`5432`) |
| `OLLAMA_HOST` / `OLLAMA_PORT` | Ollama host and port (e.g. `localhost`/`11434`) |
| `STOCKFISH_PATH` | path to the Stockfish binary (default `stockfish`, resolved from `PATH`) |

Move evaluation needs a **Stockfish** binary. In Docker it is bundled into the
image (see the `Dockerfile`); for local runs install it (e.g.
`apt install stockfish` / `brew install stockfish`) or set `STOCKFISH_PATH`.

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Then open http://localhost:8000, sign in, and set your **Chess.com username**
on the user via the admin at http://localhost:8000/admin/ (field
`chessdotcom_username`; it falls back to the login username if left blank).

The AI coach requires the Ollama service with the `llama3:8b` model pulled:

```bash
docker compose exec ollama ollama pull llama3:8b
```
