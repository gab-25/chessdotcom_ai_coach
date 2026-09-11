# Development

## Prerequisites

- **Python 3.13+** and [uv](https://docs.astral.sh/uv/)
- **PostgreSQL** and **Redis** — the `postgres` and `redis` services in
  [`docker-compose.yaml`](../docker-compose.yaml) work fine on their own:
  `docker compose up -d postgres redis`
- **Stockfish** — see [configuration.md](configuration.md#stockfish)
- **Ollama** (optional) — without it the coach falls back to Stockfish-only
  prose, which is perfectly usable for development. To get real coach prose:
  `docker compose up -d ollama` and then, once,
  `docker compose exec ollama ollama pull llama3.2:3b`
  (see [configuration.md](configuration.md#pulling-the-model))

## Setup

```bash
cp .env.example .env          # then edit: SECRET_KEY, and the local hosts
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser
```

## Running: you need four processes

This is the part that trips people up. `runserver` alone gives you a UI with
**no games in it and nothing that can analyse them**.

```bash
uv run python manage.py runserver                        # 1. the web app
uv run celery -A chessdotcom_ai_coach worker -l info     # 2. the analysis worker
uv run python manage.py run_scheduler                    # 3. the archive importer
                                                         # 4. Redis + Postgres
```

Symptoms when one is missing:

| Missing | What you see |
| --- | --- |
| Scheduler | The home page stays empty (or frozen at an old state) — nothing ever imports from Chess.com. `manage.py import_archives` fills it in without one. |
| Celery worker | Games appear, but every analysis you ask for sits on **"Analyzing…"** forever. The `CoachSuggestion` row stays `PENDING` and never reaches `RUNNING` — nothing consumes the queue. (A row stuck on `RUNNING` is a different problem: the worker is there but the analysis hung, and the scheduler retries it after 10 minutes.) |
| Redis | Pressing **Analyse this game** errors, and the scheduler logs connection errors on every tick. |

## First run

1. Open http://localhost:8000 and sign in with the superuser you created.
2. Go to http://localhost:8000/admin/, open your user, and set
   **`chessdotcom_username`** to your Chess.com account. It falls back to the
   Django login name if left blank — but the scheduler only polls users whose
   field is non-empty, so set it explicitly.
3. Within 10 minutes the scheduler imports the current month of your archive and
   the games show up on the home page. To not wait, run
   `uv run python manage.py import_archives --months 1`.

If nothing appears, check the scheduler's output — a bad username is logged and
skipped rather than raised, so it fails quietly by design.

## URL map

From [`urls.py`](../chessdotcom_ai_coach/urls.py):

| Route | View | Kind |
| --- | --- | --- |
| `/` | `home` | Full page — a page of the finished games available to review |
| `/games` | `game_list` | **HTMX fragment** — the game grid, for the Refresh button, the time-control filter (`?time_class=`) and the pager (`?page=`) |
| `/game/<id>` | `game_detail` | Full page — the review board. **404** for a game still in progress |
| `/game/<id>/view` | `game_position` | **HTMX fragment** — position at ply `?sel=N` |
| `/game/<id>/analyze` | `analyze_position` | **HTMX fragment** — `GET` is the pending self-poll (2s), `POST` requests analysis of one move |
| `/game/<id>/analyze-game` | `analyze_game` | **HTMX fragment** — `POST` queues every move you played in the game |
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
Celery Beat). One blocking scheduler running one 10-minute job: import a month of
each linked user's archive, mark finished daily games, revive stuck analyses. It
must exist exactly once: an in-process scheduler under Gunicorn would start once
per worker and duplicate every archive fetch.

It enqueues no analysis — that is on demand, from the detail page.

### `import_archives`

```bash
uv run python manage.py import_archives [--user <username>] [--months N]
```

The scheduler mirrors your archive a month per tick, which takes hours on a
multi-year account. This reads every monthly archive in sequence instead, newest
first. `--months` caps it to the most recent N months for a quick partial
catch-up; without `--user` every linked user is imported. Idempotent: a game
already stored is updated in place, so re-running adds nothing.

### `analyze_game`

```bash
uv run python manage.py analyze_game <game_id> [--user <username>]
```

The command-line half of the **Analyse this game** button: it enqueues analysis
for **every** move you played in the game. Also the way to re-run a game whose
analyses were retired as `FAILED`.

It reads the stored game (no Chess.com call) and is idempotent: moves already
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
  `_months_to_import`), and so are private template partials (`_evalfill.html`,
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
  `Analyse every move you played — on a schedule, not once (#44)`.
  Comments, commit messages and PR descriptions are written in English.

## Templates

```
templates/
├── base.html          # shell: header with version badge, htmx script
├── home.html          # the finished-games grid, refreshed on demand
├── game_detail.html   # the review page shell
├── login.html
├── error.html
└── partials/
    ├── game_list.html     # the home Refresh target
    ├── position.html      # the whole review view (#gr-view)
    ├── board.html         # 64 cells
    ├── coach_card.html    # coach panel + the 2s pending self-poll
    ├── moves_grid.html    # move list — played moves only
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
