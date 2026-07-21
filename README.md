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
- **Celery + Redis** — background scheduler/worker for automatic live-game analysis
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
| `REDIS_URL` | Redis broker/backend used by Celery (`redis://localhost:6379/0`) |
| `ANALYSIS_SCHEDULER_INTERVAL_SECONDS` | Celery Beat interval in seconds (default `1`) |
| `ANALYSIS_SCHEDULER_BATCH_SIZE` | max active games enqueued per scheduler tick (default `10`) |
| `ANALYSIS_MAX_CONCURRENCY` | worker process concurrency (default `2`) |
| `ANALYSIS_TASK_TIMEOUT_SECONDS` | max time a queued analysis task stays valid (default `120`) |

Move evaluation needs a **Stockfish** binary. In Docker it is bundled into the
image (see the `Dockerfile`). For local runs, download the same official
`sf_18` build the container uses — into the repo root — so behaviour matches:

```bash
# Run from the repo root. avx2 works on any x86-64 CPU since ~2013; if you hit
# "Illegal instruction" (older CPU / VM), swap avx2 for sse41-popcnt.
curl -fL https://github.com/official-stockfish/Stockfish/releases/download/sf_18/stockfish-ubuntu-x86-64-avx2.tar \
  | tar -x --strip-components=1 -C . stockfish/stockfish-ubuntu-x86-64-avx2
mv stockfish-ubuntu-x86-64-avx2 stockfish
chmod +x stockfish
./stockfish --version   # -> Stockfish ... sf_18
```

Then point `STOCKFISH_PATH` at it in your `.env` (the leading `./` makes it a
path rather than a `PATH` lookup):

```
STOCKFISH_PATH=./stockfish
```

The `stockfish` binary in the repo root is git-ignored, so it is never committed.

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Then open http://localhost:8000, sign in, and set your **Chess.com username**
on the user via the admin at http://localhost:8000/admin/ (field
`chessdotcom_username`; it falls back to the login username if left blank).

The AI coach requires the Ollama service with the `llama3.2:3b` model pulled:

```bash
docker compose exec ollama ollama pull llama3.2:3b
```

Background analysis for active games is started by Celery Beat every second; workers
consume those analysis tasks. For a one-off manual scheduler tick:

```bash
python manage.py analyze_active_games
```
