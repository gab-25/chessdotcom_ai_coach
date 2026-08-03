# Documentation

Chessdotcom AI Coach is a Django web app that keeps a local mirror of your
Chess.com games, renders the board server-side, and — whenever it's your turn —
runs Stockfish and a local LLM to produce a grandmaster-style comment on the
position. There is no client-side JavaScript framework: every screen is a
server-rendered HTML fragment swapped in by HTMX.

For a "clone and run" quickstart, see the [root README](../README.md). These
pages cover the parts that don't fit there.

| Page | What it covers |
| --- | --- |
| [Architecture](architecture.md) | The five cooperating processes, the analysis flow end to end, and the layering rules between modules |
| [Data model](data-model.md) | `User`, `Game`, `CoachSuggestion` — fields, constraints, and the invariants the code relies on |
| [Configuration](configuration.md) | Every environment variable, what Docker overrides, Stockfish and LLM setup |
| [Development](development.md) | Running locally, the four processes you need, URL map, management commands, code conventions |
| [Deployment](deployment.md) | Docker Compose, the container entrypoint, CI/CD, running behind a reverse proxy |
| [Testing](testing.md) | Running the suite, how `conftest.py` makes it dependency-free, and the mocking seams |

## Reading order

If you're new to the project, read [Architecture](architecture.md) first — it
explains why there are four processes instead of one, which is the single most
surprising thing about the codebase. Then [Data model](data-model.md), because
the `CoachSuggestion` row doubles as a lock and that decision shows up
everywhere else.
