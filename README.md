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
| `AUTO_ANALYZE_ENABLED` | enable the auto-analysis worker (default `true`) |
| `AUTO_ANALYZE_INTERVAL` | seconds between Celery Beat ticks (default `1.0`) |
| `AUTO_ANALYZE_MAX_PER_TICK` | max analyses started per tick (default unlimited) |
| `CELERY_BROKER_URL` | Celery broker URL (default `redis://redis:6379/0`) |

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

## Auto-analysis worker (Celery)

Auto-analysis runs as a periodic Celery task. `beat` schedules the task every
`AUTO_ANALYZE_INTERVAL` seconds and `worker` executes it. Each tick scans every
user with active games, re-fetches current games from Chess.com, and
automatically starts coach analysis for positions where it is the user's turn
and no analysis exists yet.

Analysis is started **at most once per position**: the `(user, game_id, fen)`
uniqueness of `CoachSuggestion` guarantees no duplicate work, and the worker
never overwrites an existing analysis — the manual "Re-analyze" button stays the
only way to refresh a position.

Each analysis blocks a worker slot for its full duration (~2s Stockfish plus
~20-30s local LLM inference), so tune `AUTO_ANALYZE_INTERVAL` and
`AUTO_ANALYZE_MAX_PER_TICK` to stay within your node's resources.
