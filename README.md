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
- **Celery + Redis** — game analysis runs out-of-band: the view enqueues a Celery
  task (`chessdotcom_ai_coach/tasks.py`) with Redis as broker and result backend;
  a hidden HTMX poller reveals the result once the worker finishes
- **APScheduler** — background scheduler (`manage.py run_scheduler`) that, every
  5 seconds, syncs each linked user's current games from Chess.com into the
  local DB and then auto-enqueues analysis when it's the user's turn
  (`chessdotcom_ai_coach/services/scheduler.py`); runs once inside the web
  container. This is the only path that keeps game data fresh — the home page
  (below) just reads what the scheduler already synced.
- **HTMX** — the whole UI is server-rendered fragments, vendored via
  `django-htmx`: the game-list polling (a plain DB read — see above) and the
  game detail page, where move-by-move navigation, the coach card and the live
  game poll are all htmx fragment swaps (no custom JavaScript)
- **Server-rendered board** — the FEN is expanded into a glyph board in Python
  (`chessdotcom_ai_coach/services/board.py`); there is no client-side JS framework
- **Gunicorn** — WSGI server in the container
- Custom hand-written CSS theme (no Tailwind) in the `theme` app
  (`theme/static/css/styles.css`)

## Run with Docker

```bash
docker compose up --build
```

Compose starts the whole stack: `web` (Gunicorn + the APScheduler process,
started by `entrypoint.sh` after `migrate`/`collectstatic`), a `worker` running
the Celery worker, plus `redis`, `postgres` and `ollama`. The app is served on
http://localhost:8000. In Compose the `POSTGRES_HOST`, `OLLAMA_HOST`/`OLLAMA_PORT`
and `REDIS_URL` are overridden to reach the `postgres`, `ollama` and `redis`
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
| `REDIS_URL` | Celery broker + result backend (default `redis://redis:6379/0`; use `redis://localhost:6379/0` locally) |
| `STOCKFISH_PATH` | path to the Stockfish binary (default `stockfish`, resolved from `PATH`) |

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

Analysis runs asynchronously, so it needs **Redis** plus a **Celery worker** and
the **scheduler** running alongside `runserver` — otherwise a requested analysis
stays stuck on "Analyzing…" forever. Start Redis (the `redis` service in
`docker-compose.yaml` works, or `redis-server` locally), then in two more shells:

```bash
uv run celery -A chessdotcom_ai_coach worker -l info   # the analysis worker
uv run python manage.py run_scheduler                  # the APScheduler process
```

### Analysing a whole game

The scheduler only analyses the position it's your turn to play, so reviewing a
past game shows the coach's take on just those moves. To backfill the rest,
enqueue analysis for every one of a user's moves in a game:

```bash
uv run python manage.py analyze_game <game_id> [--user <username>]
```

It reads the stored snapshot (no Chess.com call) and is idempotent — moves that
are already analysed or queued are skipped, so it's safe to re-run. `--user` is
only needed when the same game id is stored for more than one user. The results
appear on the game detail page as you step through the moves (a Celery worker
must be running).

Then open http://localhost:8000, sign in, and set your **Chess.com username**
on the user via the admin at http://localhost:8000/admin/ (field
`chessdotcom_username`; it falls back to the login username if left blank).

The AI coach requires the Ollama service with the `llama3.2:3b` model pulled:

```bash
docker compose exec ollama ollama pull llama3.2:3b
```
