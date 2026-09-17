"""Environment defaults applied before Django settings are imported.

Loaded through ``-p pytest_bootstrap`` in pyproject's ``addopts``, which pytest
imports during ``_preparse``. ``conftest.py`` is too late for anything settings.py
reads: pytest-django forces the settings import from its
``pytest_load_initial_conftests`` hook, and pytest's own conftest loader is
registered ``trylast`` on that same hook — so settings.py runs first.
"""

import os

# settings.py refuses to start without it. The suite mocks the OpenAI client
# outright, so this value never reaches the network.
os.environ.setdefault("OPENROUTER_API_KEY", "test-openrouter-key")
