# Configuration

All configuration is environment variables, loaded from `.env` by
`python-dotenv` at the top of [`settings.py`](../chessdotcom_ai_coach/settings.py).
Copy [`.env.example`](../.env.example) to `.env` and edit.

## Environment variables

| Key | Purpose | Default | Local value |
| --- | --- | --- | --- |
| `SECRET_KEY` | Django secret key | `a-very-secret-key` | generate your own |
| `DEBUG` | `true`/`false` (accepts `1`/`yes`) | `true` | `true` |
| `ALLOWED_HOSTS` | Comma-separated hosts | `*` | `*` |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated origins **with scheme**, for CSRF behind a TLS-terminating proxy | empty | empty |
| `POSTGRES_DB` | Database name | `postgres` | `postgres` |
| `POSTGRES_USER` | Database user | `postgres` | `postgres` |
| `POSTGRES_PASSWORD` | Database password | `password` | `password` |
| `POSTGRES_HOST` | Database host | `localhost` | `localhost` |
| `POSTGRES_PORT` | Database port | `5432` | `5432` |
| `LLM_BASE_URL` | OpenAI-compatible LLM endpoint | `http://ollama:11434/v1` | `http://localhost:11434/v1` |
| `LLM_MODEL` | Model tag sent with each request | `llama3.2:3b` | same |
| `REDIS_URL` | Celery broker **and** result backend | `redis://redis:6379/0` | `redis://localhost:6379/0` |
| `STOCKFISH_PATH` | Path to the engine binary | `stockfish` (resolved on `PATH`) | `./stockfish` |

Note that the **defaults are the Docker values**, not the local ones —
`ollama:11434` and `redis:6379` are Compose service names. A local run with an
incomplete `.env` therefore fails by trying to reach hostnames that only exist
inside the Compose network, rather than by complaining about a missing setting.
If analysis silently never completes locally, check `LLM_BASE_URL` and
`REDIS_URL` first.

`LLM_BASE_URL` and `LLM_MODEL` are read directly by
[`services/coach.py`](../chessdotcom_ai_coach/services/coach.py) at import time
(`os.getenv`), not through Django settings — `settings.LLM_BASE_URL` exists but
is informational.

## What Docker Compose overrides

[`docker-compose.yaml`](../docker-compose.yaml) passes `.env` through to the
`web` and `worker` services via `env_file`, then overrides five keys on both so
the containers reach each other by service name:

```yaml
environment:
  - POSTGRES_HOST=postgres
  - LLM_BASE_URL=http://ollama:11434/v1
  - LLM_MODEL=llama3.2:3b
  - REDIS_URL=redis://redis:6379/0
  - STOCKFISH_PATH=stockfish
```

`LLM_MODEL` is pinned here (rather than left to `.env`) so the tag the app asks
for always matches the one the `ollama` service pulls at startup.

So you can keep local values in `.env` and still `docker compose up` without
editing anything.

> **Note.** `STOCKFISH_PATH=stockfish` in Compose overrides the
> `ENV STOCKFISH_PATH=/usr/local/bin/stockfish` baked into the
> [`Dockerfile`](../Dockerfile). Both resolve to the same binary — the Compose
> value relies on `/usr/local/bin` being on `PATH`, which it is in the
> `python:3.13-slim` base image — but the image's own value is not the one in
> effect under Compose.

## Stockfish

Move evaluation requires a Stockfish binary. In Docker it's fetched in a builder
stage of the [`Dockerfile`](../Dockerfile) and copied into the app image — nothing
to do.

For local runs, download the same official `sf_18` build the container uses, into
the repo root, so behaviour matches:

```bash
# Run from the repo root. avx2 works on any x86-64 CPU since ~2013; if you hit
# "Illegal instruction" (older CPU / VM), swap avx2 for sse41-popcnt.
curl -fL https://github.com/official-stockfish/Stockfish/releases/download/sf_18/stockfish-ubuntu-x86-64-avx2.tar \
  | tar -x --strip-components=1 -C . stockfish/stockfish-ubuntu-x86-64-avx2
mv stockfish-ubuntu-x86-64-avx2 stockfish
chmod +x stockfish
./stockfish --version   # -> Stockfish ... sf_18
```

Then point `STOCKFISH_PATH` at it (the leading `./` makes it a path rather than a
`PATH` lookup):

```
STOCKFISH_PATH=./stockfish
```

The binary is git-ignored, so it's never committed. The releases ship dynamically
linked binaries with the NNUE network **embedded** — there is nothing to compile
and no weights file to fetch separately. The engine runs as a short-lived local
subprocess per analysis (`chess.engine.popen_uci`), with a 2-second limit, and is
always terminated in a `finally` block.

## LLM

The `ollama` service runs `ollama/ollama:latest`. Ollama exposes an
OpenAI-compatible API under `/v1`, so the app talks to it with the standard
`openai` async client and a dummy API key — nothing in
[`services/coach.py`](../chessdotcom_ai_coach/services/coach.py) is
Ollama-specific.

A 3B model is deliberate: ~2GB loaded, against ~5–6GB for the 8B build, which
keeps the whole stack inside an 8GB node.

### Keep alive — why Ollama and not llama-server

```yaml
environment:
  - OLLAMA_KEEP_ALIVE=30s
```

This is the reason the project uses Ollama. `llama-server` keeps the weights
resident for the process's entire lifetime; Ollama **unloads the model 30
seconds after the last request**, so between analyses the ~2GB goes back to the
node. On an 8GB box shared with Postgres, Redis, `web` and `worker`, that
headroom is what makes the stack fit.

The trade-off: a request that arrives after an idle gap also pays for reloading
the model. That is bounded (a few seconds for a 3B Q4) and covered by the
150-second client timeout. Raise the value if you analyse in long bursts and have
the RAM; lower it to `0` to unload immediately.

Check what is loaded right now:

```bash
docker compose exec ollama ollama ps    # empty once the keep-alive window closes
```

### Model pull

Ollama has no `-hf`-style auto-download, so
[`ollama-entrypoint.sh`](../ollama-entrypoint.sh) — bind-mounted into the
container as its entrypoint — starts the server, waits for the API, pulls
`$OLLAMA_MODEL` (default `llama3.2:3b`) and then stays in foreground on the
server process. The blob lands in the `ollama-data` volume, so later starts are a
no-op. **While the first pull runs (~2GB), the coach falls back to Stockfish-only
text**, which is expected and self-corrects.

To use a different model, pull it and point `LLM_MODEL` at the same tag:

```bash
docker compose exec ollama ollama pull qwen2.5:3b
```

Then set `LLM_MODEL=qwen2.5:3b` (and `OLLAMA_MODEL` on the `ollama` service, so
it survives a fresh volume). The two must match — the app's request names the
tag, and Ollama returns an error for a tag it hasn't pulled.

Health check:

```bash
curl http://localhost:11434/api/tags    # lists the pulled models
```

The request itself uses a **150-second timeout** (CPU inference for this model
runs 20–30s, and worst cases are much slower) and `temperature=0.7`. If it fails
for any reason, `get_best_move` returns the Stockfish-only fallback prose — a
missing LLM degrades the analysis, it never breaks it.

## Behind a reverse proxy

Two settings matter when TLS is terminated upstream (Traefik, nginx, Caddy):

- `CSRF_TRUSTED_ORIGINS` must list the public origin **with scheme**, e.g.
  `https://coach.example.com`. Without it, every POST fails CSRF validation.
- `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` is set
  unconditionally in [`settings.py`](../chessdotcom_ai_coach/settings.py), so
  Django trusts the proxy's forwarded-proto header. Make sure the proxy actually
  sets it.

## Static files

WhiteNoise serves static assets straight from Gunicorn (Django's dev server only
serves them under `runserver`), with its middleware placed immediately after
`SecurityMiddleware` as required.

The storage backend is `CompressedStaticFilesStorage` — **not** the manifest
variant. This is intentional: the bundled Font Awesome `all.min.css` references
webfont files that aren't shipped, and the manifest backend parses those `url()`
references and would fail `collectstatic` on the missing files.

## Version badge

`APP_VERSION` is read once at startup from `pyproject.toml` via `tomllib`
([`settings.py`](../chessdotcom_ai_coach/settings.py)) and injected into every
template by the `app_version` context processor
([`context_processors.py`](../chessdotcom_ai_coach/context_processors.py)), which
renders as the badge in the page header. Bumping the version means editing
`pyproject.toml` and nothing else.
