import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "chessdotcom_ai_coach.settings")

application = get_wsgi_application()
