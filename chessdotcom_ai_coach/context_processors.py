from django.conf import settings


def app_version(request):
    """Expose the application version to every template (replaces app.state.version)."""
    return {"version": settings.APP_VERSION}
