FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml .

RUN pip install --no-cache-dir .

COPY manage.py entrypoint.sh ./
COPY chessdotcom_ai_coach ./chessdotcom_ai_coach
COPY coach ./coach
COPY static ./static

RUN chmod +x entrypoint.sh

EXPOSE 8000

CMD ["./entrypoint.sh"]
