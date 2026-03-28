#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

echo "--- Starting Entrypoint Script ---"

# 1. Run Alembic Migrations
echo "Checking and running database migrations..."
alembic upgrade head
echo "Migrations completed successfully."

# 2. Check and Pull Ollama Model
LLM_MODEL=${LLM_MODEL:-"llama3:8b"}
OLLAMA_URL=${OLLAMA_HOST:-"http://localhost:11434"}

echo "Checking for Ollama model: $LLM_MODEL via $OLLAMA_URL..."

# Check if model already exists using the /api/show endpoint
RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" "$OLLAMA_URL/api/show" -d "{\"name\": \"$LLM_MODEL\"}")

if [ "$RESPONSE" != "200" ]; then
    echo "Model $LLM_MODEL not found. Pulling (this may take a while)..."
    # Call the pull endpoint (outputs a JSON stream of progress)
    curl -X POST "$OLLAMA_URL/api/pull" -d "{\"name\": \"$LLM_MODEL\"}"
    echo -e "\nModel $LLM_MODEL pull command finished."
else
    echo "Model $LLM_MODEL is already available."
fi

echo "--- Bootstrap finished. Starting FastAPI server ---"

# Execute the CMD passed from Dockerfile (usually "fastapi run")
exec "$@"
