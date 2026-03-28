FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .

RUN pip install --no-cache-dir .

COPY ./libs ./libs
COPY ./alembic ./alembic
COPY ./alembic.ini .
COPY ./entrypoint.sh .
COPY ./chessdotcom_ai_coach ./chessdotcom_ai_coach

RUN chmod +x ./entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]

CMD ["fastapi", "run"]
