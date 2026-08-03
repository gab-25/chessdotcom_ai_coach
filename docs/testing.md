# Testing

```bash
uv run pytest
```

That's it. **No PostgreSQL, no Redis, no Stockfish binary and no LLM server are
required** — the suite is deliberately self-contained, which is why CI runs it
with nothing but `uv sync --group dev`.

## Configuration

pytest is configured in [`pyproject.toml`](../pyproject.toml):

```toml
[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "chessdotcom_ai_coach.settings"
python_files = "test_*.py"
asyncio_mode = "auto"
testpaths = ["tests"]
```

`asyncio_mode = "auto"` means async test functions run without an explicit
`@pytest.mark.asyncio` marker. Dev dependencies are `pytest`, `pytest-asyncio`
and `pytest-django`.

## Layout

148 tests across 9 modules in [`tests/`](../tests/), mirroring the source layout:

| Module | Covers |
| --- | --- |
| [`test_views.py`](../tests/test_views.py) | 64 tests — the largest by far. Grouped into classes per concern: `TestHome`, `TestGameDetail`, `TestAnalyzePosition`, `TestCoachCardModes`, `TestMovesGrid`, `TestLiveMoveSlot`, `TestHistoryList`, … |
| [`test_scheduler.py`](../tests/test_scheduler.py) | The tick: syncing, result backfill, enqueue deduplication |
| [`test_chess_client.py`](../tests/test_chess_client.py) | Chess.com response parsing, including the archive result codes |
| [`test_coach.py`](../tests/test_coach.py) | Every evaluation branch and the LLM fallback |
| [`test_board.py`](../tests/test_board.py) | FEN/PGN expansion |
| [`test_game_store.py`](../tests/test_game_store.py) | Upsert, retire, result persistence |
| [`test_analysis.py`](../tests/test_analysis.py) | Whole-game enqueue idempotency |
| [`test_models.py`](../tests/test_models.py) | Properties and constraints |
| [`test_tasks.py`](../tests/test_tasks.py) | The Celery task's persistence |

DB-backed tests carry `@pytest.mark.django_db`, usually on the class.

## How the suite stays dependency-free

[`conftest.py`](../conftest.py) does two things, and both are worth understanding
before you touch it.

### 1. Environment defaults before import

`SECRET_KEY` and `LLM_BASE_URL` are `setdefault`-ed at the very top of the file,
**before** `import pytest`. Several modules read environment variables at import
time (`services/coach.py` reads `LLM_BASE_URL`, `STOCKFISH_PATH` and `LLM_MODEL`
with `os.getenv` at module level), so setting them later would be too late.

### 2. The database is swapped for SQLite

The `django_db_modify_db_settings` fixture is overridden to rewrite
`settings.DATABASES["default"]` to SQLite, then rebuild the connection handler
with `connections.__init__()`.

Two details that look odd and are both intentional:

- **It overrides `django_db_modify_db_settings`, not `django_db_setup`.** That
  hook runs *before* pytest-django's default setup, so migrations still run
  normally — they just create the tables in SQLite instead of the PostgreSQL
  configured in `settings.py`.
- **It uses a temp *file*, not `:memory:`.** The async views are driven through
  the ASGI test client on worker threads. A shared file database (with a 30s busy
  timeout) is reachable from every connection, whereas each connection to
  `:memory:` would get its own empty database and the tests would fail in
  confusing ways.

## Mocking seams

There are three, and they're stable — new tests should reuse them rather than
inventing their own.

| Dependency | Patch target |
| --- | --- |
| **Stockfish** | `chess.engine.popen_uci` — returns `(transport, engine)`, so the stub returns a pair of mocks with `engine.play` as an `AsyncMock`. No subprocess is ever spawned. |
| **LLM** | `AsyncOpenAI` — the response shape the code reads is `response.choices[0].message.content`. Making the call raise exercises the Stockfish-only fallback branch. |
| **Celery** | `analyze_game_task` at its *import site* — `chessdotcom_ai_coach.services.scheduler.analyze_game_task`, `...services.analysis.analyze_game_task`, `chessdotcom_ai_coach.views.analyze_game_task`. Tests assert on `.delay` calls; nothing is ever enqueued. |

Chess.com is patched the same way, at the import site:
`chessdotcom_ai_coach.services.scheduler.Client`.

Note the pattern: **patch where the name is used, not where it's defined.** This
is also why [`tasks.py`](../chessdotcom_ai_coach/tasks.py) was kept as a thin
wrapper around `get_best_move` instead of absorbing its logic — the coach's
existing seam keeps working.

`test_coach.py` builds its stubs with a `_engine(...)` context manager and feeds
them **real** `python-chess` score objects (`Cp`, `Mate`, `PovScore`), so the
evaluation-text branches are exercised against genuine score arithmetic rather
than hand-rolled fakes.

## Adding a test

Put it in the module matching the code under test, and copy the closest existing
case — the fixtures and helper builders there are the fastest path:

- **A view or template fragment?** [`test_views.py`](../tests/test_views.py).
  Find the class matching the concern (`TestCoachCardModes` for a new coach card
  state, `TestMovesGrid` for grid rendering) and add to it. Assertions are mostly
  against rendered HTML.
- **Scheduler behaviour?** [`test_scheduler.py`](../tests/test_scheduler.py) —
  patch `Client` and `analyze_game_task`, then assert on `.delay` call counts.
  The deduplication tests are the model for anything touching the enqueue lock.
- **A new evaluation branch?** [`test_coach.py`](../tests/test_coach.py), reusing
  the `_engine` helper.
- **Pure FEN/PGN logic?** [`test_board.py`](../tests/test_board.py) — no DB, no
  mocks, no `django_db` marker needed.

## What isn't covered

No coverage tooling is configured, and there's no linter or formatter (`ruff`,
`black`, `flake8` are all absent). There are also no integration tests that run
the real engine or a real LLM — every external dependency is mocked, so a change
in Stockfish's UCI output or in the LLM's response shape would pass CI and
fail at runtime.
