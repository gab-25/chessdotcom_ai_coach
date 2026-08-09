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
- **APScheduler** — background scheduler (`manage.py run_scheduler`) with two
  jobs (`chessdotcom_ai_coach/services/scheduler.py`): every 5 seconds it syncs
  each linked user's current games from Chess.com into the local DB and enqueues
  the analyses those games are missing, and every 10 minutes it does the same
  sweep over the finished ones. This is the only path that keeps game data fresh
  — the pages just read what it already synced.
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

## Monitoring the analyses

Analyses are queued, so a game fills in over minutes rather than all at once.
To follow what the worker is doing:

```bash
docker compose logs -f worker      # live task log
```

The line to watch for is `Task ... succeeded in Ns`. Reading the log:

- **Nothing but `received`, never `succeeded`** → tasks are arriving but not
  finishing. Check the LLM: `docker compose logs ollama`.
- **`LLM Error` followed by a task that still succeeds** → the analysis fell back
  to Stockfish-only text. If those come in bursts, the worker is outrunning
  Ollama — see the `--concurrency` note in `docker-compose.yaml`.
- **`Giving up on analysis ... after 3 attempts`** → that position is `failed`
  and is not retried automatically. Use the card's "Try again", or
  `manage.py analyze_game <game_id>`.

### The queue itself, from Redis

The worker log says what's being worked on; Redis says how much is waiting and
how much is in flight:

```bash
docker compose exec redis redis-cli llen celery      # queued, not yet delivered
docker compose exec redis redis-cli hlen unacked     # delivered, not yet acked
docker compose exec redis redis-cli dbsize           # + one key per stored result
```

`unacked` is where `acks_late` lives: a task enters it on delivery and only
leaves once it has finished. A steadily falling `llen celery` means the backlog
is draining — expect a move a minute or so, since the analyses are serialised
behind the LLM.

`unacked` above the worker's `--concurrency` is normal after a worker was killed
mid-analysis: the messages it was holding are left behind. Their age tells them
apart from the live ones — anything younger than the worker's uptime is genuinely
running:

```bash
docker compose exec redis redis-cli zrange unacked_index 0 -1 WITHSCORES \
  | paste - - | awk -v now=$(date +%s) '{printf "%s  age=%ds\n", substr($1,1,8), now-$2}'
```

```
545272c9  age=535s     <- left by a killed worker
2aa00083  age=498s     <- left by a killed worker
6782fed1  age=92s      <- running
cb959676  age=43s      <- running
```

Those leftovers are cosmetic, not lost work: the analysis they represent is
recovered from the app side, where the scheduler returns any row left `running`
for 10 minutes to the queue. Expect the count to stay stale-ish until then. If
`llen celery` never falls and nothing succeeds in the worker log, the worker is
down.

To watch commands flow in real time (noisy, Ctrl-C to stop):

```bash
docker compose exec redis redis-cli monitor
```

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

Every move you played gets its own analysis, and you don't have to ask for it.
The scheduler doesn't just react to the position it happens to see — on each run
it compares the game's PGN against the analyses already stored and queues the
difference. A turn that came and went between two polls, a task lost to a worker
restart, a game that was already over when you linked the account: all of it is
picked up on a later run. Active games are reconciled on the 5 second tick,
finished ones every 10 minutes, for as long as they are stored.

Analyses are queued, not instant — a whole game is dozens of them, each a few
seconds of Stockfish plus an LLM call — so a game you just finished fills in
gradually. To skip the wait for one game, or to retry one whose analyses were
given up on:

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
