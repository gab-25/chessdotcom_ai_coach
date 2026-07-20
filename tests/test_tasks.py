from unittest.mock import patch

import pytest

from chessdotcom_ai_coach.tasks import auto_analyze_active_games


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.tasks.auto_analyze.run_once")
def test_auto_analyze_task_runs_once_with_default_setting(mock_run_once, settings):
    settings.AUTO_ANALYZE_ENABLED = True
    settings.AUTO_ANALYZE_MAX_PER_TICK = 3
    mock_run_once.return_value = 2

    started = auto_analyze_active_games()

    assert started == 2
    mock_run_once.assert_called_once_with(max_per_tick=3)


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.tasks.auto_analyze.run_once")
def test_auto_analyze_task_skips_when_disabled(mock_run_once, settings):
    settings.AUTO_ANALYZE_ENABLED = False

    started = auto_analyze_active_games()

    assert started == 0
    mock_run_once.assert_not_called()
