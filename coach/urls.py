from django.contrib.auth.views import LoginView
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("login", LoginView.as_view(template_name="coach/login.html"), name="login"),
    path("logout", views.logout_view, name="logout"),
    path("game/<str:id>", views.game_detail, name="game_detail"),
    path("game/<str:id>/coach", views.coach_suggestion, name="coach_suggestion"),
]
