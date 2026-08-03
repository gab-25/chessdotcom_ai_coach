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
| `LLM_BASE_URL` | OpenAI-compatible LLM endpoint | `http://llm:8080/v1` | `http://localhost:8080/v1` |
| `LLM_MODEL` | Model name sent with each request | `llama-3.2-3b-instruct` | same |
| `REDIS_URL` | Celery broker **and** result backend | `redis://redis:6379/0` | `redis://localhost:6379/0` |
| `STOCKFISH_PATH` | Path to the engine binary | `stockfish` (resolved on `PATH`) | `./stockfish` |

Note that the **defaults are the Docker values**, not the local ones —
`llm:8080` and `redis:6379` are Compose service names. A local run with an
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
`web` and `worker` services via `env_file`, then overrides four keys on both so
the containers reach each other by service name:

```yaml
environment:
  - POSTGRES_HOST=postgres
  - LLM_BASE_URL=http://llm:8080/v1
  - REDIS_URL=redis://redis:6379/0
  - STOCKFISH_PATH=stockfish
```

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

The `llm` service runs `ghcr.io/ggml-org/llama.cpp:server`, which exposes an
OpenAI-compatible API — the app talks to it with the standard `openai` async
client and a dummy API key.

```yaml
command: >
  -hf bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M
  --host 0.0.0.0 --port 8080 -c 4096 --jinja
```

- `-hf` auto-downloads the GGUF (~2GB) into the `llm-models` volume on first
  start — no manual pull. **While the download runs, the coach falls back to
  Stockfish-only text**, which is expected and self-corrects.
- `-c 4096` bounds the KV cache, and therefore RAM.
- `--jinja` uses the model's embedded chat template.

A 3B model is deliberate: ~2GB loaded, against ~5–6GB for the 8B build, which
keeps the whole stack inside an 8GB node.

Health check:

```bash
curl http://localhost:8080/health
```

For a fully offline or reproducible setup, mount a local `.gguf` into the service
and swap `-hf ...` for `-m /models/<file>.gguf`.

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
