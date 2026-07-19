"""
Global pytest configuration.

Keeps the test suite self-contained: it must run without a live PostgreSQL,
Ollama or LC0 engine. We (1) provide safe defaults for the environment
variables that modules read at import time, and (2) swap the database for a
file-backed SQLite so no PostgreSQL server is needed.
"""

import os
import tempfile

# Defaults for env vars read at import time. In particular
# chessdotcom_ai_coach/services/coach.py does int(os.getenv("CHESS_ENGINE_PORT"))
# at module level, so a missing value would blow up on import.
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("CHESS_ENGINE_HOST", "127.0.0.1")
os.environ.setdefault("CHESS_ENGINE_PORT", "9999")
os.environ.setdefault("OLLAMA_HOST", "localhost")
os.environ.setdefault("OLLAMA_PORT", "11434")

import pytest


@pytest.fixture(scope="session")
def django_db_modify_db_settings():
    """Swap the DB for a file-backed SQLite before pytest-django builds it.

    Overriding this hook (rather than ``django_db_setup``) mutates the DB
    config *before* the default setup runs, so migrations still create the
    tables — just in SQLite instead of the PostgreSQL configured in
    settings.py. DB-backed tests then run anywhere with no external services.

    A temp file (not ``:memory:``) is used because the async views are driven
    through the ASGI test client on worker threads; a shared file DB with a
    busy timeout is reachable from every connection, whereas each connection
    to ``:memory:`` would see its own empty database.
    """
    from django.conf import settings
    from django.db import connections

    db_path = os.path.join(tempfile.gettempdir(), "chessdotcom_ai_coach_test.sqlite3")
    settings.DATABASES["default"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": db_path,
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "OPTIONS": {"timeout": 30},
        "TIME_ZONE": None,
        "USER": "",
        "PASSWORD": "",
        "HOST": "",
        "PORT": "",
        "TEST": {
            "CHARSET": None,
            "COLLATION": None,
            "MIGRATE": True,
            "MIRROR": None,
            "NAME": db_path,
        },
    }
    # Rebuild the connection handler so its cached settings point at the new
    # (SQLite) config instead of the PostgreSQL one loaded from settings.py.
    connections.__init__()
