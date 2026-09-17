"""The mandatory-``OPENROUTER_API_KEY`` guard in settings.py.

OpenRouter is the only LLM provider, so a missing key is a misconfiguration and
settings.py refuses to start. These tests run settings.py for real rather than
poking at a helper, because what is being asserted is that the guard is wired at
module scope — where every entry point is bound to hit it.
"""

import importlib.util
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

import chessdotcom_ai_coach.settings as settings_module

SETTINGS_PATH = Path(settings_module.__file__)


def _exec_settings():
    """Run settings.py top to bottom in a throwaway module.

    Deliberately not ``importlib.reload``: reload re-executes into the *live*
    module's namespace, so a run that raises would leave a half-updated
    ``chessdotcom_ai_coach.settings`` behind in ``sys.modules``. This module is
    never registered there at all. ``django.conf.settings`` is unaffected either
    way — it holds a snapshot taken when pytest started, not a live view of the
    module — so no later test can observe anything done here.

    This works because settings.py has no relative imports; give it one and it
    has to be executed under its real package name instead.
    """
    spec = importlib.util.spec_from_file_location("_settings_under_test", SETTINGS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _neutralize_dotenv(monkeypatch):
    """Stop settings.py's ``load_dotenv()`` re-reading a developer's own .env.

    Patch it on ``dotenv``, not on the settings module: ``from dotenv import
    load_dotenv`` re-binds the name from the source package on every execution.
    """
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)


@pytest.mark.parametrize("missing", ["absent", "empty"])
def test_import_without_api_key_raises(monkeypatch, missing):
    if missing == "absent":
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENROUTER_API_KEY", "")

    with pytest.raises(ImproperlyConfigured, match="OPENROUTER_API_KEY"):
        _exec_settings()


def test_import_with_api_key_succeeds(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert _exec_settings().APP_VERSION
