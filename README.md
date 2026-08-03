# chessdotcom_ai_coach

Chess Coach AI — a **Django** web app that lists your live Chess.com games,
renders the board, and asks a local LLM to analyze the position like a
grandmaster coach.

## Stack

- **Django 5** — ORM, templates, admin, session auth (custom `User` model)
- **PostgreSQL** — via `psycopg2-binary`
- **Ollama** — serves the local LLM behind an OpenAI-compatible API on `/v1`; the
  app reaches it with the `openai` async client for the AI coach prose
  (`chessdotcom_ai_coach/services/coach.py`). `OLLAMA_KEEP_ALIVE` unloads the
  model between analyses, so the ~2GB of weights don't sit in RAM permanently
- **Stockfish** — UCI engine for move evaluation, run as a local subprocess via
  `python-chess` (`chessdotcom_ai_coach/services/coach.py`)
- **Chess.com API** — via `chess-com` (`chessdotcom_ai_coach/services/chess_client.py`)
- **Celery + Redis** — analysis runs out-of-band: a task is enqueued
  (`chessdotcom_ai_coach/tasks.py`) with Redis as broker and result backend, and
  a hidden HTMX poller reveals the result once the worker finishes
- **APScheduler** — background scheduler (`manage.py run_scheduler`) that every
  5 seconds syncs each linked user's current games from Chess.com into the local
  DB and auto-enqueues analysis when it's the user's turn
  (`chessdotcom_ai_coach/services/scheduler.py`). This is the only path that
  keeps game data fresh — the pages just read what it already synced.
- **HTMX** — the whole UI is server-rendered fragments, vendored via
  `django-htmx`: game-list polling, move-by-move navigation, the coach card and
  the live game poll are all fragment swaps, with no custom JavaScript
- **Server-rendered board** — the FEN is expanded into a glyph board in Python
  (`chessdotcom_ai_coach/services/board.py`); there is no client-side JS framework
- **Gunicorn** — WSGI server in the container
- Custom hand-written CSS theme (no Tailwind) in the `theme` app
  (`theme/static/css/styles.css`)

## Documentation

| Page | What it covers |
| --- | --- |
| [Architecture](docs/architecture.md) | The four processes, the analysis flow end to end, the layering rules |
| [Data model](docs/data-model.md) | `User`, `Game`, `CoachSuggestion` and the invariants the code relies on |
| [Configuration](docs/configuration.md) | Every environment variable, Docker overrides, Stockfish and LLM setup |
| [Development](docs/development.md) | Running locally, URL map, management commands, code conventions |
| [Deployment](docs/deployment.md) | Docker Compose, container entrypoint, CI/CD, reverse proxy |
| [Testing](docs/testing.md) | Running the suite and how it stays dependency-free |

## Run with Docker

```bash
cp .env.example .env
docker compose up --build
docker compose exec ollama ollama pull llama3.2:3b   # once, ~2GB
```

Compose starts the whole stack: `web` (Gunicorn + the APScheduler process,
started by `entrypoint.sh` after `migrate`/`collectstatic`), a `worker` running
the Celery worker, plus `redis`, `postgres` and `ollama`. The app is served on
http://localhost:8000.

Compose overrides `POSTGRES_HOST`, `LLM_BASE_URL`, `LLM_MODEL`, `REDIS_URL` and
`STOCKFISH_PATH` so the containers reach each other by service name; everything
else comes from `.env`.

**The `ollama` service starts empty** — it downloads no model on its own, so the
pull above is required. It is stored in the `ollama-data` volume and survives
restarts, so you only do it once. Skip it and the app still works, but every
analysis falls back to Stockfish-only text with no coach prose. See
[configuration.md](docs/configuration.md#pulling-the-model) for how to use a
different model.

Then create a user (see [First run](#first-run) below).

## Run locally

Requires Python 3.13+, [uv](https://docs.astral.sh/uv/), and a running PostgreSQL
and Redis — the `postgres` and `redis` services in `docker-compose.yaml` work on
their own (`docker compose up -d postgres redis`).

```bash
cp .env.example .env    # then set SECRET_KEY and check the local hosts
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser
```

Move evaluation needs a **Stockfish** binary; in Docker it's bundled into the
image, locally you download it into the repo root. See
[docs/configuration.md](docs/configuration.md#stockfish) for the exact command,
and for the full environment-variable reference.

Analysis is asynchronous, so a local run needs **four processes** — without the
worker and the scheduler, a requested analysis stays stuck on "Analyzing…"
forever:

```bash
uv run python manage.py runserver                        # the web app
uv run celery -A chessdotcom_ai_coach worker -l info     # the analysis worker
uv run python manage.py run_scheduler                    # the APScheduler process
                                                         # + Redis and PostgreSQL
```

## First run

Open http://localhost:8000, sign in, then set your **Chess.com username** on the
user via the admin at http://localhost:8000/admin/ (field `chessdotcom_username`;
it falls back to the login username if left blank, but the scheduler only polls
users whose field is non-empty). Your current games appear within a few seconds.

## Analysing a whole game

The scheduler only analyses the position it's your turn to play, so reviewing a
past game shows the coach's take on just those moves. To backfill the rest:

```bash
uv run python manage.py analyze_game <game_id> [--user <username>]
```

It reads the stored snapshot (no Chess.com call) and is idempotent, so it's safe
to re-run. A Celery worker must be running.

## Tests

```bash
uv run pytest
```

No external services needed — see [docs/testing.md](docs/testing.md).
