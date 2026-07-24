from django.contrib import admin
from django.contrib.auth.views import LoginView
from django.urls import path

from . import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.home, name="home"),
    path("games", views.game_list, name="game_list"),
    path(
        "login",
        LoginView.as_view(template_name="login.html"),
        name="login",
    ),
    path("logout", views.logout_view, name="logout"),
    path("game/<str:id>", views.game_detail, name="game_detail"),
    path("game/<str:id>/view", views.game_position, name="game_position"),
    path("game/<str:id>/live", views.game_live, name="game_live"),
    path("game/<str:id>/analyze", views.analyze_position, name="analyze_position"),
]
