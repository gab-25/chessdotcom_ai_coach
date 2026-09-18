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
| `OPENROUTER_API_KEY` | OpenRouter API key | empty — without it the coach falls back to Stockfish-only prose | same |
| `LLM_MODEL` | Model slug sent with each request | `anthropic/claude-haiku-4.5` | same |
| `REDIS_URL` | Celery broker **and** result backend | `redis://redis:6379/0` | `redis://localhost:6379/0` |
| `SYNC_COOLDOWN_SECONDS` | How long a user's archive-sync claim holds | `300` | same |
| `STOCKFISH_PATH` | Path to the engine binary | `stockfish` (resolved on `PATH`) | `./stockfish` |

Note that the **defaults are the Docker values**, not the local ones —
`redis:6379` is a Compose service name. A local run with an incomplete `.env`
therefore fails by trying to reach a hostname that only exists inside the Compose
network; if analysis silently never completes locally, check `REDIS_URL` first.

`LLM_MODEL` and `OPENROUTER_API_KEY` are read directly by
[`services/coach.py`](../chessdotcom_ai_coach/services/coach.py) at import time
(`os.getenv`), not through Django settings — which keeps that module importable
without a configured Django. Nothing in
[`settings.py`](../chessdotcom_ai_coach/settings.py) knows about the LLM at all.

## Celery settings

Not environment variables, but worth knowing why they are what they are — they
are set in [`settings.py`](../chessdotcom_ai_coach/settings.py) and they change
how the worker behaves under restart:

| Setting | Value | Why |
| --- | --- | --- |
| `CELERY_TASK_ACKS_LATE` | `True` | Acknowledge a task after it ran, not when it was delivered, so an in-flight analysis isn't lost with its worker. A graceful stop hands the message straight back; after a hard kill it waits on kombu's visibility timeout (an hour), which is why the 10-minute `sync.requeue_stale_analyses` — run when the detail page is loaded — is the guarantee that actually holds. |
| `CELERY_TASK_REJECT_ON_WORKER_LOST` | `True` | Makes the above cover a worker killed outright (an OOM kill), not just a clean shutdown. |
| `CELERY_WORKER_PREFETCH_MULTIPLIER` | `1` | Reserve one task at a time. An analysis takes seconds to minutes, so prefetching a batch would hide those tasks from an idle worker and, with `acks_late`, return the whole batch to the queue when one worker dies. This bounds what a worker *reserves*, not what it *runs*: concurrency is a separate knob, left at Celery's default of one process per core, since the only local ceiling is CPU-bound Stockfish — OpenRouter serves the LLM calls in parallel. |

Because a task can therefore run more than once, `attempts` is capped: one that
kills its worker would otherwise be retried for ever. See
[data-model.md](data-model.md#the-lock-needs-an-expiry).

## What Docker Compose overrides

[`docker-compose.yaml`](../docker-compose.yaml) passes `.env` through to the
`web` and `worker` services via `env_file`, then overrides three keys on both so
the containers reach each other by service name:

```yaml
environment:
  - POSTGRES_HOST=postgres
  - REDIS_URL=redis://redis:6379/0
  - STOCKFISH_PATH=stockfish
```

Everything else — `OPENROUTER_API_KEY` and `LLM_MODEL` included — comes straight
from `.env`. The key is deliberately **not** named in the compose file: it is a
secret, and the file is committed.

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

The coach prose comes from [OpenRouter](https://openrouter.ai), which fronts many
providers behind one OpenAI-compatible API. The app talks to it with the standard
`openai` async client and a pinned `https://openrouter.ai/api/v1` endpoint — there
is no local model, no container and nothing to download.

There is also no second provider, and no local one. Without a key the coach still
works — it just stops coaching, and every analysis completes on Stockfish-only
prose.

### The API key

`OPENROUTER_API_KEY` is optional. Create one at
[openrouter.ai/keys](https://openrouter.ai/keys) and put it in `.env`:

```bash
OPENROUTER_API_KEY=sk-or-v1-...
```

Leave it empty and the app starts and runs perfectly normally, but there is no
LLM to ask: the coach card shows the Stockfish-only fallback text on every move.
Nothing else breaks, and nothing is sent anywhere.

That degradation is quiet by design — it is the same path a `401` or a `429`
takes — so the worker log is where you see it. A missing key is named explicitly
rather than left to surface as an opaque `401`:

```
LLM Error: OPENROUTER_API_KEY is not set; skipping the LLM and using Stockfish-only prose.
```

**If you are getting fallback prose everywhere, check this first.**

Two consequences of *setting* a key, worth stating plainly:

- **It costs money.** Every analysed move is one API request, billed per token.
  Analysing a whole game is dozens of them. Watch the spend on your OpenRouter
  dashboard, and treat the key as the secret it is — it is passed via `.env` and
  never written into [`docker-compose.yaml`](../docker-compose.yaml).
- **Positions leave the machine.** The FEN and the PGN of the analysed game are
  sent to OpenRouter and on to whichever provider serves the model. With no key
  set, nothing leaves the machine at all.

### Choosing a model

`LLM_MODEL` takes an OpenRouter model slug; browse them at
[openrouter.ai/models](https://openrouter.ai/models). The default is:

```bash
LLM_MODEL=anthropic/claude-haiku-4.5
```

It is a compromise between prose quality and the fact that the app calls the
model once per analysed move: a frontier model writes a better comment and costs
several times as much per game. Changing it is one line and a restart of `web`
and `worker` — there is nothing to pull and no tag to keep in sync.

```bash
docker compose up -d web worker
```

An unknown slug is not validated anywhere: the request simply fails and that
analysis falls back to Stockfish-only text, with `LLM Error` in the worker log.

### Timeout and fallback

The request uses a **60-second timeout** and `temperature=0.7`. If it fails for
any reason — no key, a `401` on a revoked key, a `429` from a rate limit, a
timeout, a network error — `get_best_move` returns the Stockfish-only fallback
prose and the analysis still completes. There is no retry: a rate-limited move is
left to the fallback, and **Re-analyse this game** is the way to ask again.

So there is exactly one failure mode, and it is never fatal: the analysis always
finishes, with or without the coaching prose.

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
