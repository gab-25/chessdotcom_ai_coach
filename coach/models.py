from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """
    Application user. Extends Django's built-in user (username, password hashing,
    sessions, admin) with the linked Chess.com account name.
    """

    chessdotcom_username = models.CharField(max_length=255, blank=True, null=True)

    @property
    def chess_username(self) -> str:
        """The Chess.com username to query, falling back to the app username."""
        return self.chessdotcom_username or self.username
