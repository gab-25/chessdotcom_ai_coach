FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml .

RUN pip install --no-cache-dir .

COPY ./chessdotcom_ai_coach ./chessdotcom_ai_coach

CMD ["fastapi", "run"]
