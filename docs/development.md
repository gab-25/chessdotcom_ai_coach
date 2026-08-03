# Development

## Prerequisites

- **Python 3.13+** and [uv](https://docs.astral.sh/uv/)
- **PostgreSQL** and **Redis** — the `postgres` and `redis` services in
  [`docker-compose.yaml`](../docker-compose.yaml) work fine on their own:
  `docker compose up -d postgres redis`
- **Stockfish** — see [configuration.md](configuration.md#stockfish)
- **llama-server** (optional) — without it the coach falls back to Stockfish-only
  prose, which is perfectly usable for development: `docker compose up -d llm`

## Setup

```bash
cp .env.example .env          # then edit: SECRET_KEY, and the local hosts
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser
```

## Running: you need four processes

This is the part that trips people up. `runserver` alone gives you a working UI
that **never analyses anything**.

```bash
uv run python manage.py runserver                        # 1. the web app
uv run celery -A chessdotcom_ai_coach worker -l info     # 2. the analysis worker
uv run python manage.py run_scheduler                    # 3. the APScheduler tick
                                                         # 4. Redis + Postgres
```

Symptoms when one is missing:

| Missing | What you see |
| --- | --- |
| Scheduler | The home page stays empty (or frozen at an old state) — nothing ever syncs from Chess.com. |
| Celery worker | Games appear, but every analysis sits on **"Analyzing…"** forever. The `CoachSuggestion` row is `PENDING` and nothing consumes the queue. |
| Redis | The scheduler logs connection errors on every tick. |

## First run

1. Open http://localhost:8000 and sign in with the superuser you created.
2. Go to http://localhost:8000/admin/, open your user, and set
   **`chessdotcom_username`** to your Chess.com account. It falls back to the
   Django login name if left blank — but the scheduler only polls users whose
   field is non-empty, so set it explicitly.
3. Within 5 seconds the scheduler picks up your current games and the home page
   poll reveals them.

If nothing appears, check the scheduler's output — a bad username is logged and
skipped rather than raised, so it fails quietly by design.

## URL map

From [`urls.py`](../chessdotcom_ai_coach/urls.py):

| Route | View | Kind |
| --- | --- | --- |
| `/` | `home` | Full page — current games + past-games history |
| `/games` | `game_list` | **HTMX fragment**, polled every 5s |
| `/game/<id>` | `game_detail` | Full page — the review/live board |
| `/game/<id>/view` | `game_position` | **HTMX fragment** — position at ply `?sel=N` |
| `/game/<id>/live` | `game_live` | **HTMX poll** every 5s — returns **204** when `head` is unchanged |
| `/game/<id>/analyze` | `analyze_position` | **HTMX fragment** — `GET` is the pending self-poll (2s), `POST` requests analysis |
| `/login`, `/logout` | Django `LoginView`, `logout_view` | Session auth |
| `/admin/` | Django admin | Where you link the Chess.com account |

All game views are `@login_required` and scoped to `request.user`, so a game id
belonging to another user reads as not found.

## Management commands

### `run_scheduler`

```bash
uv run python manage.py run_scheduler
```

The APScheduler process — **the only scheduling in the project** (there is no
Celery Beat). Runs one blocking scheduler with a 5-second interval job, and must
exist exactly once: an in-process scheduler under Gunicorn would start once per
worker and enqueue duplicates.

### `analyze_game`

```bash
uv run python manage.py analyze_game <game_id> [--user <username>]
```

The scheduler only analyses positions it's *your* turn to play, so reviewing a
past game shows the coach's take on just those moves. This command backfills the
rest — it enqueues analysis for **every** move you played in the game.

It reads the stored snapshot (no Chess.com call) and is idempotent: moves already
analysed or queued are skipped, so re-running is safe. `--user` is only needed
when the same game id is stored for more than one user, which happens when both
players use the app. A Celery worker must be running; results appear on the
detail page as you step through the moves.

## Code conventions

There is no linter or formatter configured — these are conventions the codebase
follows consistently, not enforced rules.

- **Comments explain *why*, not *what*.** Many carry historical rationale (why
  the non-manifest WhiteNoise backend, why a file-backed SQLite in tests, why
  `--timeout 180`). Keep that habit: the *what* is readable from the code.
- **Private helpers are `_`-prefixed** (`_position_context`, `_suggestion`,
  `_linked_users`), and so are private template partials (`_evalfill.html`,
  `_arrows_svg.html`).
- **Module docstrings state the module's job and its boundary** — see
  [`services/game_store.py`](../chessdotcom_ai_coach/services/game_store.py) for
  the pattern. Respect the [layering rules](architecture.md#layering-rules).
- **Defensive parsing everywhere.** A malformed FEN yields an empty board, an
  unparseable PGN yields `[]`, a per-user API failure is logged and skipped so
  one bad account never breaks a batch.
- **Idempotency via `get_or_create` on a unique key** whenever work is enqueued.
- `from __future__ import annotations` plus `typing` in the newer service
  modules; `TypedDict` for structured returns crossing a boundary.
- **Templates carry `{% comment %}` blocks** explaining their swap semantics —
  worth reading before changing `partials/coach_card.html` or
  `partials/position.html`, which use `hx-swap-oob`.
- **Commit style:** short imperative subject with the PR number, e.g.
  `Show the move you're about to play as a live slot in the moves grid (#41)`.
  Comments, commit messages and PR descriptions are written in English.

## Templates

```
templates/
├── base.html          # shell: header with version badge, htmx script
├── home.html          # current games + history, polls /games
├── game_detail.html   # the review page shell
├── login.html
├── error.html
└── partials/
    ├── game_list.html     # the 5s home poll target
    ├── position.html      # the whole review view (#gr-view)
    ├── board.html         # 64 cells
    ├── coach_card.html    # coach panel + the 2s pending self-poll
    ├── moves_grid.html    # move list, including the live slot
    ├── history_list.html  # analysis timeline
    ├── _evalfill.html     # eval bar (swapped out-of-band)
    └── _arrows_svg.html   # SVG arrow overlay (swapped out-of-band)
```

Styling is hand-written CSS in [`theme/static/css/styles.css`](../theme/static/css/styles.css)
— no Tailwind, no build step. It opens with a full CSS custom-property design
system (the "Gambit" theme) and uses BEM-ish naming with a `gr-` prefix for
game-review components. Fonts are self-hosted woff2.

## Testing

See [testing.md](testing.md). Short version: `uv run pytest`, no external
services needed.
