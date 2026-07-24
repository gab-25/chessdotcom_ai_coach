from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import CoachSuggestion, Game, User


@admin.register(User)
class CoachUserAdmin(UserAdmin):
    """Admin for the custom user, exposing the Chess.com link field."""

    fieldsets = UserAdmin.fieldsets + (
        ("Chess.com", {"fields": ("chessdotcom_username",)}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("Chess.com", {"fields": ("chessdotcom_username",)}),
    )
    list_display = UserAdmin.list_display + ("chessdotcom_username",)


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    """Read-only view over the persisted game snapshots."""

    list_display = (
        "game_id",
        "user",
        "white_name",
        "black_name",
        "is_active",
        "result",
        "updated_at",
    )
    list_filter = ("is_active", "result", "time_class")
    search_fields = ("game_id", "white_name", "black_name")


@admin.register(CoachSuggestion)
class CoachSuggestionAdmin(admin.ModelAdmin):
    """Read-only view over the coach analyses (one per position)."""

    list_display = ("game_id", "user", "move_no", "best_move_san", "eval_text", "updated_at")
    list_filter = ("user",)
    search_fields = ("game_id", "best_move_san")
