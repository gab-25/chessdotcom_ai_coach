# Architecture

The app is not a single Django process. It is **four processes plus two backing
services**, and understanding why is the fastest way into the codebase.

The reason is latency: a coach analysis costs ~2s of Stockfish plus 20–30s of
CPU LLM inference. That can't happen inside a request, so it happens in a Celery
worker. Something has to keep the local mirror of your games up to date — that's
APScheduler. The web process, as a result, never talks to Chess.com and never
runs the engine: it only reads the database.

**The app reviews games, it does not watch them.** Your whole Chess.com archive
is mirrored locally — every finished game, live and daily — and the coach
comments on the moves you played, when you ask it to. Three consequences shape
everything below:

- there is **no live poll** anywhere, and the position you are *about* to play is
  never analysed;
- games come from the **monthly archives**, not from the current-games endpoint,
  which is daily-only;
- **no schedule enqueues analysis.** A full archive is thousands of games at
  dozens of analyses each — more than a worker would ever finish — so analysis
  starts from a button.

## Components

```mermaid
graph TD
    Browser["Browser<br/><i>HTMX fragments, no JS framework</i>"]

    subgraph app["Application processes"]
        Web["<b>web</b> — Gunicorn + Django<br/>views.py, templates/"]
        Sched["<b>scheduler</b> — APScheduler<br/>manage.py run_scheduler<br/><i>10min tick</i>"]
        Worker["<b>worker</b> — Celery<br/>analyze_game_task"]
    end

    subgraph infra["Backing services"]
        PG[("PostgreSQL<br/>Game, CoachSuggestion<br/>ArchiveImport")]
        Redis[("Redis<br/>broker + results")]
    end

    subgraph ext["External / local engines"]
        ChessCom["Chess.com public API"]
        SF["Stockfish<br/><i>local subprocess</i>"]
        LLM["Ollama<br/><i>OpenAI-compatible</i>"]
    end

    Browser -->|"navigation, paging, analyse request<br/>pending card poll every 2s"| Web
    Web --> PG
    Sched -->|"monthly archives<br/>+ current games"| ChessCom
    Sched --> PG
    Web -->|"enqueue task<br/><i>when you ask</i>"| Redis
    Redis --> Worker
    Worker --> SF
    Worker --> LLM
    Worker -->|"persist suggestion"| PG

    linkStyle 1 stroke:#4a7a52,stroke-width:2px
```

Note what is **missing** from that graph: there is no arrow from `web` to
Chess.com, to Stockfish or to the LLM. Every view renders from the stored
snapshot alone, which is what makes navigating a game as cheap as opening it.

## How a game gets here, and how it gets analysed

Two separate stories, and keeping them apart is the key to the codebase.

**Games arrive from the monthly archives.** `/player/{u}/games/{yyyy}/{mm}` is the
only Chess.com endpoint that carries live games at all — the current-games
endpoint everyone reaches for first is documented as *"Daily Chess games that a
player is currently playing"*, which is why the app saw nothing but daily games
before. The import walks the archive list a month at a time, so a five-year
account fills in over a few hours rather than in one burst that Chess.com would
rate-limit.

**Analysis is requested, never scheduled.** An imported archive is thousands of
games; at dozens of analyses each, and up to 152s apiece, no schedule could drain
that queue. So the coach runs when you press **Analyse this game** (or ask for a
single move), and `enqueue_game_analysis` — idempotent, as ever — turns that into
one task per move you played.

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser (HTMX)
    participant S as Scheduler
    participant C as Chess.com API
    participant DB as PostgreSQL
    participant Q as Redis / Celery
    participant W as Worker
    participant E as Stockfish + LLM

    rect rgba(120,140,180,0.10)
    Note over S,DB: every 10min — the sync tick (enqueues nothing)
    S->>C: my_current_games() per linked user
    Note over S,C: daily-only, in-progress-only
    S->>DB: upsert_current_games() — mark every game<br/>that vanished as is_active=False

    S->>C: archive_months()
    C-->>S: one URL per month the player was active
    S->>C: finished_games(yyyy, mm) — the current month,<br/>plus one backlog month per run
    C-->>S: every finished game: final PGN, result, time class
    S->>DB: upsert_finished_games() + record ArchiveImport
    Note over S,DB: variants (chess960, crazyhouse, …) are skipped:<br/>Stockfish and the PGN replay assume standard chess

    S->>DB: requeue_stale_analyses() — rows RUNNING past ANALYSIS_TIMEOUT
    S->>Q: requeue_orphaned_analyses() — queue empty + nothing RUNNING?
    Note over S,Q: then no PENDING row can still have a message,<br/>so hand them back (Redis lost the queue)
    end

    rect rgba(120,160,120,0.10)
    Note over B,Q: on demand — the user asks
    B->>DB: POST /game/<id>/analyze-game
    DB->>DB: for each user ply still missing:<br/>get_or_create CoachSuggestion(user, game_id, fen_before)
    alt row was just created
        DB->>Q: analyze_game_task.delay(...)
    else row already exists
        Note over DB: already queued, running or done — skip.<br/>The row IS the lock, so a double click is free.
    end
    end

    Q->>W: deliver task
    W->>DB: claim the row: status RUNNING, attempts += 1
    W->>E: get_best_move(fen, pgn)
    E-->>W: eval + best move (Stockfish, 2s limit)
    E-->>W: coaching prose (LLM, 150s timeout)
    Note over W,E: LLM failure → Stockfish-only fallback text,<br/>the analysis still completes
    W->>DB: update_or_create → status DONE

    loop every 2s while pending or running
        B->>DB: GET /game/<id>/analyze
    end
    DB-->>B: coach card + out-of-band eval bar, arrows, moves grid
```

### Why the import is a reconciliation, not a one-off

`upsert_finished_games` updates in place on `(user, game_id)`, and the current
month is re-read on every run. That is what lets a game finished five minutes ago
appear without anything watching for it, and what lets a game whose closing moves
were still settling be corrected later. Re-running the import — on a schedule, or
by hand with `manage.py import_archives` — adds nothing the second time.

`sync_current_games` survives alongside it for one narrow job: a daily game still
being played is in no archive, so without it a game you just finished would keep
showing as "still in progress" until the next archive read caught up.

### Surviving a worker restart

The task is acknowledged after it runs, not when it is delivered
(`CELERY_TASK_ACKS_LATE` with `CELERY_TASK_REJECT_ON_WORKER_LOST`, in
[`settings.py`](../chessdotcom_ai_coach/settings.py)), so an analysis in flight
when the worker goes down is not simply lost. How fast it comes back depends on
*how* the worker died, and the difference is worth knowing:

- **Graceful stop** (`docker compose restart`/`stop`, a redeploy) — Celery hands
  its un-acked messages back to the broker on the way out. The analysis is
  re-delivered within seconds and starts over from the beginning.
- **Hard kill** (OOM, `docker kill`, a stop that outruns its grace period while
  an analysis is mid-LLM-call) — nothing gets to hand anything back. The messages
  sit in Redis' `unacked` set, and kombu only re-delivers them after its
  visibility timeout, an hour by default.

So the broker is *not* the guarantee in the case that matters most. That is
`requeue_stale_analyses`, which returns any row left RUNNING for
`ANALYSIS_TIMEOUT` to the queue — 10 minutes, whatever the broker is doing.
Treat re-delivery as an optimisation on top of it, not the mechanism.

Either way a task can be run more than once, so `attempts` bounds it: the worker
counts the attempt as it claims the row, and the fourth claim retires the
position as FAILED instead of running it again.

## Component reference

| Component | Entry point | Notes |
| --- | --- | --- |
| Scheduler job | [`management/commands/run_scheduler.py`](../chessdotcom_ai_coach/management/commands/run_scheduler.py) | One, `TICK_INTERVAL_MINUTES = 10`, with `max_instances=1` and `coalesce=True` so a slow run never overlaps the next. |
| Tick body | [`services/scheduler.py`](../chessdotcom_ai_coach/services/scheduler.py) | `sync_current_games`, `import_archive_month`, `requeue_stale_analyses`, `requeue_orphaned_analyses` — each called in its own `try/except` so a Chess.com outage still leaves the local recovery checks running. **Enqueues no analysis.** |
| Archive import | [`services/scheduler.py`](../chessdotcom_ai_coach/services/scheduler.py) | `import_archive_month` — where games come from. `_months_to_import` picks the current month (always, since it keeps growing) plus one backlog month per run, so a multi-year account fills in over hours instead of one burst. `import_all_archives` is the same work unpaced, behind `manage.py import_archives`. |
| Stuck-analysis recovery | [`services/scheduler.py`](../chessdotcom_ai_coach/services/scheduler.py) | Two halves: `requeue_stale_analyses` for a row whose *worker* died (RUNNING past `ANALYSIS_TIMEOUT`), `requeue_orphaned_analyses` for one whose *message* did (PENDING while the broker queue is empty and nothing is RUNNING). |
| Celery task | [`tasks.py`](../chessdotcom_ai_coach/tasks.py) | `analyze_game_task` claims the row (RUNNING, `attempts += 1`), then wraps the async coach in `async_to_sync`. Kept thin deliberately, so `services/coach.py` stays untouched and its test mocking seam still applies. |
| Coach | [`services/coach.py`](../chessdotcom_ai_coach/services/coach.py) | `get_best_move(fen, pgn)` → a `Suggestion` TypedDict. Stockfish first (2s), then the LLM (150s timeout); on LLM error it returns Stockfish-only prose rather than failing. |
| Chess.com IO | [`services/chess_client.py`](../chessdotcom_ai_coach/services/chess_client.py) | `archive_months()` and `finished_games(y, m)` (the archive — every game type), plus `my_current_games()` (daily, in progress only). Pure IO + shape normalisation, no DB access. |
| Persistence | [`services/game_store.py`](../chessdotcom_ai_coach/services/game_store.py) | Pure DB reads/writes: `upsert_finished_games`, `upsert_current_games`, `past_games` (a **queryset**, so the home page pages in the database), `time_classes`, `stored_game`. No Chess.com access. |
| Board rendering | [`services/board.py`](../chessdotcom_ai_coach/services/board.py) | Expands FEN/PGN into what templates can iterate over. |
| Views | [`views.py`](../chessdotcom_ai_coach/views.py) | Thin, except `_position_context` (see below). |
| Whole-game analysis | [`services/analysis.py`](../chessdotcom_ai_coach/services/analysis.py) | `enqueue_game_analysis` — the idempotent enqueue, applied to every move the user played in a game. Driven by the **Analyse this game** button (`views.analyze_game`) and by `manage.py analyze_game`. `_covered_plies` reads the game's existing rows in one query and matches on `(move_no, side to move)`, so the FEN spellings never diverge into duplicates. |

## Board rendering

Django's template language can't parse a FEN, so [`services/board.py`](../chessdotcom_ai_coach/services/board.py)
does it in Python:

- **`fen_to_cells(fen, highlight, flipped)`** — expands a FEN into a flat list of
  64 cell dicts (`glyph`, `light`, `highlight`, `white`) in reading order, or
  reversed when the board is flipped for a black player.
- **`moves_from_pgn(pgn)`** — one entry per ply: `{move_no, color, san, uci, fen_before}`.
  `fen_before` is the position the player was about to play — **the same position
  the coach analyses**, which is what ties a suggestion back to its move.
- **`positions_from_pgn(pgn)`** — the FEN after each ply, initial position first,
  index-aligned with `moves_from_pgn` (`positions[i + 1]` is the position reached
  by `moves[i]`). The review page renders any selected move straight from this
  list, so nothing has to re-implement castling, promotion or en passant.
- **`annotate_moves(moves, suggestions)`** — joins suggestions onto moves by
  **`(move_no, color)`, not by FEN**. Chess.com and python-chess format the
  en-passant and halfmove-clock fields differently, so a string comparison on FEN
  would silently miss; `(move_no, color)` is a unique key for a ply within a game
  and survives that difference.

Every one of these degrades gracefully: a malformed FEN yields an empty board, an
unparseable PGN yields `[]`.

## `_position_context`

[`views.py::_position_context`](../chessdotcom_ai_coach/views.py) is the biggest
function in the project and produces everything the position fragment needs for
one ply: board cells, eval-bar fill, SVG arrow coordinates, the moves grid, the
analysis-history timeline and the coach card's mode.

The concept worth knowing is the **ply cursor `sel`**:

- `sel = 0` — the starting position.
- `head` — the number of plies in the PGN, i.e. the last move actually played,
  and the end of the timeline. `sel` is clamped to it.

There is deliberately **no cursor past `head`**. A position the player never
reached has no move to comment on, so the timeline simply stops — which is also
why a `CoachSuggestion` row left over for such a position is invisible without
any filtering: `annotate_moves` joins rows onto the plies in the PGN, and there
is no ply to join to.

The coach card is then rendered in one of five modes — `start`, `opponent`,
`unanalyzed`, `pending`, `failed`, `analyzed` — and the eval bar carries the last
analysed value forward across un-analysed plies so it never snaps back to 50%.

## The HTMX layer

There is no custom JavaScript. Everything is a fragment swap:

- **Home** (`home.html`) does not poll. Its **Refresh** button, the time-control
  filter and the pager all fetch `/games` — a plain DB read — and swap the grid in
  place; the fragment also carries an `hx-swap-oob` copy of the count line that
  lives outside the swapped container. Each control carries the *other*'s state in
  its query string (the filter drops the page, the pager keeps the filter), so
  they never cancel out.
- **Detail** (`partials/position.html`) does not poll either. A finished game does
  not change, so navigation is the only thing that swaps `#gr-view` — that, and
  the **Analyse this game** POST, which re-renders it with the plies now pending.
- **Pending coach cards** (`partials/coach_card.html`) self-poll
  `/game/<id>/analyze` `every 2s` until the worker finishes. PENDING and RUNNING
  both render as that pending card — the distinction matters to the scheduler,
  not to someone waiting for an answer.
- **Keyboard navigation** is done with HTMX triggers, not JS:
  `hx-trigger="click, keydown[key=='ArrowLeft'] from:body"`.
- The coach card uses `hx-swap-oob` to update the eval bar, board arrows, moves
  grid and history **out of band**, so a freshly-arrived analysis refreshes the
  board without re-swapping the whole view.

## Layering rules

These are conventions, not enforced by tooling, but the whole codebase follows
them and new code should too:

1. **`services/chess_client.py` does Chess.com IO only** — no DB, no Django models.
2. **`services/game_store.py` does DB only** — no HTTP calls.
3. **`services/scheduler.py` orchestrates the two** and is where per-user
   exception handling lives (a bad account is logged and skipped, never allowed
   to break the batch).
4. **Views stay thin and never call Chess.com.** They read the snapshot.
5. **Idempotency via `get_or_create` on a unique key** is the standard way to
   enqueue work — see `analysis.enqueue_game_analysis`. It is what lets the
   enqueue path be a reconciliation that can run on a schedule, rather than a
   one-shot trigger whose failure loses the work.
6. **Only finished games are analysed or shown.** `views._reviewable_game` is the
   single gate for the second half of that, and `game_store.past_games` for the
   first; nothing else should reach for `is_active` on its own.
7. **Nothing on a schedule enqueues analysis.** If you find yourself adding a job
   that queues work over the whole archive, re-read the sizing above: it is the
   reason the button exists.

## One layout quirk

The Django *project* package and the *app* are the same module. `settings.py`,
`urls.py`, `wsgi.py` and `celery.py` sit alongside `models.py`, `views.py` and
`migrations/` inside [`chessdotcom_ai_coach/`](../chessdotcom_ai_coach/). If you
expected the usual `project/` + `app/` split, this is why you can't find it. The
only other app is [`theme/`](../theme/), which is static assets only (CSS, fonts,
images) and has no Python beyond the app config.
