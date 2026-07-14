"""Unit tests for the custom User model."""

import pytest

from chessdotcom_ai_coach.models import User


@pytest.mark.django_db
class TestChessUsername:
    def test_uses_chessdotcom_username_when_set(self):
        user = User(username="login_name", chessdotcom_username="chess_name")
        assert user.chess_username == "chess_name"

    def test_falls_back_to_login_username_when_unset(self):
        user = User(username="login_name", chessdotcom_username=None)
        assert user.chess_username == "login_name"

    def test_falls_back_to_login_username_when_blank(self):
        user = User(username="login_name", chessdotcom_username="")
        assert user.chess_username == "login_name"
