# Documentation

Chessdotcom AI Coach mirrors your whole Chess.com archive locally — **every
finished game, live and daily** — and replays any of them move by move. Ask it to
analyse a game and, for each move you played, it runs Stockfish and asks an LLM
through OpenRouter for a grandmaster-style comment on the position you faced.

It is a review tool, not a live assistant: a game you are still playing does not
appear until it ends, a move you have not played is never analysed, and nothing
is analysed until you ask. There is no client-side JavaScript framework: every
screen is a server-rendered HTML fragment swapped in by HTMX.

For a "clone and run" quickstart, see the [root README](../README.md). These
pages cover the parts that don't fit there.

| Page | What it covers |
| --- | --- |
| [Architecture](architecture.md) | The four cooperating processes, how games arrive and how they get analysed, and the layering rules between modules |
| [Data model](data-model.md) | `User`, `Game`, `CoachSuggestion`, `ArchiveImport` — fields, constraints, and the invariants the code relies on |
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
