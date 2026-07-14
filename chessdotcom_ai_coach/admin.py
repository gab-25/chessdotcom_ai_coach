from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


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
