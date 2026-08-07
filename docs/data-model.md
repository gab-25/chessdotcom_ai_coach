# Data model

Three models, all in [`models.py`](../chessdotcom_ai_coach/models.py). Two of
them exist because of the same constraint: **Chess.com only serves games that are
still in progress.** The moment a game ends it disappears from the `current`
endpoint, so if the app didn't snapshot it, the game — and its whole move list —
would be gone.

```mermaid
erDiagram
    USER ||--o{ GAME : "plays"
    USER ||--o{ COACHSUGGESTION : "requests"

    USER {
        int id PK
        string username "Django login"
        string chessdotcom_username "linked account, nullable"
        bool is_active
    }

    GAME {
        int id PK
        int user_id FK
        string game_id "last segment of the Chess.com URL"
        text pgn "snapshot — source of the move history"
        string fen "current position"
        string white_name
        string black_name
        bool is_active "seen in the latest fetch"
        string result "win|loss|draw|unknown"
        string result_detail "checkmate, resignation, timeout, ..."
    }

    COACHSUGGESTION {
        int id PK
        int user_id FK
        string game_id "no FK — decoupled from Game"
        string fen "the analysed position = join key"
        int move_no
        string status "pending|done"
        string eval_text
        float eval_cp "White POV, drives the eval bar"
        string best_move_san
        string best_move_uci "board arrow"
        text analysis "coach prose"
    }
```

`Game` and `CoachSuggestion` are linked by `(user, game_id)` but there is **no
foreign key** between them — a suggestion survives independently of the game row.

## `User`

Django's `AbstractUser` plus one field:

- **`chessdotcom_username`** — the linked Chess.com account. Nullable and blank.
- **`chess_username`** (property) — the username to actually query, falling back
  to the Django login name when the field is blank. Always use this property;
  the scheduler, views and analysis code all do.

Only users with `is_active=True` **and** a non-empty `chessdotcom_username` are
polled (`scheduler._linked_users`). A user who never set the field is invisible
to the scheduler — this is the most common reason for "nothing shows up on the
home page".

## `Game`

A snapshot, not a live view. Fields worth calling out:

| Field | Meaning |
| --- | --- |
| `game_id` | The last segment of the Chess.com URL (e.g. `944768131` from `https://www.chess.com/game/daily/944768131`). Not globally unique across users in this table — see the constraint below. |
| `pgn` | The move history. Everything the review page shows is derived from this. |
| `fen` | The current position, i.e. the one the coach analyses when it's your turn. |
| `is_active` | `True` = seen in the most recent `current games` fetch. `upsert_current_games` flips to `False` every row it *didn't* just see, which is how a game moves to the "past games" history. |
| `result` / `result_detail` | The outcome relative to **this row's user**, plus how it ended. |

**Constraints:** unique on `(user, game_id)`; default ordering `-updated_at`.
The same Chess.com game can therefore be stored twice if both players use the
app — one row each, with opposite `result` values. This is why the `analyze_game`
management command takes an optional `--user`.

### Why `result` starts as `unknown`

A snapshot taken while the game is still current carries a PGN whose `Result` tag
is `*` — the outcome simply isn't in the data. It's resolved afterwards by
`scheduler.backfill_results()`, which reads the **monthly archives** (a different
Chess.com endpoint, where each side carries a `result` code) and calls
`game_store.set_result()`.

The backfill is bounded by `RESULT_BACKFILL_WINDOW` (3 days, in
[`services/scheduler.py`](../chessdotcom_ai_coach/services/scheduler.py)): only
games that ended recently are retried, so a game that can never be resolved — for
instance one played under an alias the archive doesn't match — stops being
re-fetched forever. At a month boundary the previous month's archive is merged in
as a fallback, since long daily games routinely end in a different month from the
one they're queried in.

Two helpers make templates readable: `has_result` (is it resolved?) and
`result_label` (`"Win"` / `"Loss"` / `"Draw"`, or `""` while unknown).

## `CoachSuggestion`

One row per analysed position: **at most one per `(user, game_id, fen)`**.

The FEN identifies the position the player was about to play. Re-analysing the
same position overwrites the row, so each move keeps a single latest analysis
instead of accumulating duplicates.

### The row is the lock

This is the central design decision in the project. There is no separate lock
table, no Redis lock, no `in_flight` flag:

```python
_row, created = CoachSuggestion.objects.get_or_create(
    user=game.user, game_id=game.game_id, fen=game.fen,
    defaults={"status": CoachSuggestion.Status.PENDING, ...},
)
if created:
    analyze_game_task.delay(...)
```

Because the unique constraint on `(user, game_id, fen)` makes `get_or_create`
atomic, `created=True` happens exactly once per position. The scheduler runs this
every 5 seconds against every active game and enqueues **only** on the tick that
created the row — a position already pending or done is skipped for free.

The same pattern appears in
[`services/analysis.py`](../chessdotcom_ai_coach/services/analysis.py), which is
what makes `manage.py analyze_game` safe to re-run.

The one place that deliberately bypasses it is the explicit **re-analyze** button
([`views.py::analyze_position`](../chessdotcom_ai_coach/views.py)): on `POST`, the
row is reset to `PENDING` with its fields cleared and re-enqueued whatever state
it was in, pending included. That's a user asking for a fresh take, not a
duplicate — and it's the manual way out of the deadlock described next.

### The lock needs an expiry

Making the row the lock has one failure mode: if the task dies with its worker
(an OOM kill, say — Celery does not redeliver by default), nothing ever writes
the row to `DONE`. It stays `PENDING`, every later `get_or_create` finds it and
enqueues nothing, and the coach card self-polls for ever. The lock is held by a
task that no longer exists.

`scheduler.requeue_stale_analyses()` gives it an expiry. A row untouched for
`STALE_PENDING_AFTER` is handed back to the worker and its `attempts` bumped; the
save refreshes `updated_at`, which spaces out the next retry. Past
`MAX_ANALYSIS_ATTEMPTS` the position is retired as `DONE` carrying the same
"unavailable" shape `coach.get_best_move` produces on engine failure, so the card
stops spinning without inventing a new status.

`STALE_PENDING_AFTER` is deliberately generous (30 minutes). It has to exceed the
worst-case **queue** wait, not the runtime of one analysis: a whole-game backfill
is ~40 moves × (2s Stockfish + up to 150s LLM), so a task can sit queued far
longer than it takes to run. Too low a value re-queues tasks that are still alive
and grows the very backlog it's reacting to. A spurious re-enqueue is wasteful but
never corrupting — `analyze_game_task` upserts on the same key.

### Status lifecycle

```mermaid
stateDiagram-v2
    [*] --> PENDING: scheduler / analyze_game / re-analyze<br/>creates the row
    PENDING --> DONE: analyze_game_task persists the result
    DONE --> PENDING: user clicks "Re-analyze"
    PENDING --> PENDING: HTMX self-polls every 2s
    PENDING --> PENDING: requeue_stale_analyses<br/>revives a lost task
    PENDING --> DONE: retired after MAX_ANALYSIS_ATTEMPTS
```

A row stuck at `PENDING` for a few seconds is normal. One that stays there across
several scheduler ticks *and* is never revived means **no Celery worker is
running** — the task was enqueued into Redis and nothing consumed it. That is
exactly the "Analyzing…" symptom described in [development.md](development.md).

### Duplicate rows for one ply

The unique key is the raw FEN, but the same ply can reach the DB under two
spellings: the live scheduler stores Chess.com's FEN while
[`services/analysis.py`](../chessdotcom_ai_coach/services/analysis.py) stores
python-chess's `board.fen()`, and the two can differ in the halfmove clock or the
en-passant field. `_ply_already_covered` therefore matches on `(move_no, side to
move)` — the ply identity `board_utils.annotate_moves` already joins on — before
creating anything, so the end-of-game backfill doesn't re-analyse every move the
coach already handled live.

### Evaluation fields

- **`eval_cp`** — centipawns / 100, always from **White's perspective**, so the
  eval bar has a single consistent orientation. A forced mate is pegged to
  `±10.0` rather than the raw mate score, so the bar saturates instead of
  overflowing.
- **`eval_text`** — the human-readable version of the same thing
  (`"White is clearly better (+1.24)."`).
- **`best_move_san`** / **`best_move_uci`** — the same move twice: SAN for
  display, UCI because `views._uci_to_squares` slices it into from/to squares for
  the board arrow overlay.
- **`analysis`** — the LLM's prose, or the Stockfish-only fallback text when the
  LLM was unreachable. It's never empty for a `DONE` row.

**Constraints:** unique on `(user, game_id, fen)`; default ordering
`["move_no", "-updated_at"]`, which is why the analysis-history timeline comes
out in move order without any explicit sort.

## Migrations

`migrations/0001` … `0004`, in [`chessdotcom_ai_coach/migrations/`](../chessdotcom_ai_coach/migrations/).
Applied automatically by [`entrypoint.sh`](../entrypoint.sh) on container start;
run `uv run python manage.py migrate` by hand for local development.
