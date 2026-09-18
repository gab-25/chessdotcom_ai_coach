# chessdotcom_ai_coach

Chess Coach AI — a **Django** web app that mirrors your whole Chess.com archive
locally, **every finished game, live and daily**, and replays any of them move by
move. Ask it to analyse a game and, for each move you played, it renders the board
and asks Stockfish and a local LLM what it would have played instead, like a
grandmaster coach going over the game with you.

It reviews, it does not watch. A game you are still playing does not appear until
it ends, a move you have not played yet is never analysed, and nothing is analysed
until you ask — a full archive is more games than any worker would get through.

## Stack

- **Django 5** — ORM, templates, admin, session auth (custom `User` model)
- **PostgreSQL** — via `psycopg2-binary`
- **OpenRouter** — the LLM behind the AI coach prose, reached with the `openai`
  async client against its OpenAI-compatible `/v1`
  (`chessdotcom_ai_coach/services/coach.py`). It is the only provider, and it is
  optional: without an `OPENROUTER_API_KEY` every analysis falls back to
  Stockfish-only text. With one, the coach becomes a paid, networked dependency —
  each analysed move is one billed request, and the FEN and PGN of the game go to
  a third party
- **Stockfish** — UCI engine for move evaluation, run as a local subprocess via
  `python-chess` (`chessdotcom_ai_coach/services/coach.py`)
- **Chess.com API** — via `chess-com` (`chessdotcom_ai_coach/services/chess_client.py`)
- **Celery + Redis** — both background jobs run out-of-band: a task is enqueued
  (`chessdotcom_ai_coach/tasks.py`) with Redis as broker and result backend, and
  a hidden HTMX poller reveals the result once the worker finishes
- **No scheduler** — there is no cron, no Celery Beat and no scheduler process.
  Importing a user's Chess.com archive is started by that user pressing
  **Sync** (`chessdotcom_ai_coach/services/sync.py`), rate-limited to once per
  `SYNC_COOLDOWN_SECONDS` by a claim on their own row, and handed to the worker.
  Merely opening a page fetches nothing, so an idle deployment makes no Chess.com
  requests at all, and the `web` service scales to as many replicas as you like
  without duplicating any of it.
- **HTMX** — the whole UI is server-rendered fragments, vendored via
  `django-htmx`: the home refresh, move-by-move navigation and the coach card are
  all fragment swaps, with no custom JavaScript
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
# optional: put your OpenRouter key in .env for real coach prose
docker compose up --build
```

Compose starts the whole stack: `web` (Gunicorn, started by `entrypoint.sh`
after `migrate`/`collectstatic`), a `worker` running the Celery worker, plus
`redis` and `postgres`. The app is served on http://localhost:8000.

Compose overrides `POSTGRES_HOST`, `REDIS_URL` and `STOCKFISH_PATH` so the
containers reach each other by service name; everything else, the OpenRouter key
included, comes from `.env`.

**Set `OPENROUTER_API_KEY` for real coach prose** — get one at
[openrouter.ai/keys](https://openrouter.ai/keys). Leave it empty and the app still
works, but every analysis falls back to Stockfish-only text with no coaching
prose; `docker compose logs worker` says so on every move. There is no model to
download and no other first-install step. See
[configuration.md](docs/configuration.md#choosing-a-model) for how to use a
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
  finishing. Stockfish is the local suspect; the LLM call is capped at 60s and
  falls back rather than hanging.
- **`LLM Error` followed by a task that still succeeds** → the analysis fell back
  to Stockfish-only text. `OPENROUTER_API_KEY is not set` means exactly that; a
  `401` means the key is wrong or revoked; a `429` in bursts means you are past
  your OpenRouter rate limit, and the fix is fewer `worker` replicas or a higher
  limit.
- **`Giving up on analysis ... after 3 attempts`** → that position is `failed`
  and is not retried automatically. Use the card's "Try again", or
  `manage.py analyze_game <game_id>`.
- **Silence, with analyses still outstanding** → the queue and the database
  disagree: rows say `PENDING` but no message is waiting for them, so nothing
  picks them up. Compare the two, `docker compose exec redis redis-cli llen
  celery` against the `pending` count. Opening one of those positions repairs it
  (`Re-enqueued N analyses that were PENDING with an empty queue` in
  `docker compose logs web`); seeing it repeatedly means the queue is being lost,
  so check that the `redis-data` volume is mounted.

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

Analysis is asynchronous, so a local run needs **three processes** — without the
worker, a requested analysis stays stuck on "Analyzing…" forever and no archive
is ever imported:

```bash
uv run python manage.py runserver                        # the web app
uv run celery -A chessdotcom_ai_coach worker -l info     # analysis + archive import
                                                         # + Redis and PostgreSQL
```

## First run

Open http://localhost:8000, sign in, then set your **Chess.com username** on the
user via the admin at http://localhost:8000/admin/ (field `chessdotcom_username`;
it falls back to the login username if left blank, but only a non-empty field
counts as a linked account). Open the home page and press **Sync**: your
archive starts importing — the whole history on the first pass, so a multi-year
account takes a few minutes. To re-read it later, ignoring what has already been imported:

```bash
uv run python manage.py import_archives [--user <username>] [--months N]
```

## Analysing a whole game

Open a game and press **Analyse this game**: every move you played is queued, one
analysis each. Nothing is analysed until you ask, because a full archive is
thousands of games at dozens of analyses apiece — no worker would ever finish
that queue, and the games you actually want to review would sit behind years of
ones you don't.

Expect a game to fill in gradually rather than at once: a whole game is dozens of
analyses, each a couple of seconds of Stockfish plus one OpenRouter call, so the card for each
move turns from pending to answered as the worker gets to it. Pressing the button
again costs nothing — the enqueue is idempotent. The command-line equivalent, and
the way to retry a game whose analyses were given up on:

```bash
uv run python manage.py analyze_game <game_id> [--user <username>]
```

It reads the stored game (no Chess.com call) and is idempotent, so it's safe
to re-run. A Celery worker must be running.

## Tests

```bash
uv run pytest
```

No external services needed — see [docs/testing.md](docs/testing.md).
