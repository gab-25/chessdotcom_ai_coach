#!/bin/sh
set -e

MODEL="${OLLAMA_MODEL:-llama3:8b}"

# 1. Start the Ollama server in the background
ollama serve &
pid=$!

# 2. Wait for the server to become responsive
until ollama list >/dev/null 2>&1; do
  echo "Waiting for Ollama to be ready..."
  sleep 1
done

# 3. Pull the model only if it is not already in the volume
if ! ollama list | grep -q "$MODEL"; then
  echo "Pulling model $MODEL..."
  ollama pull "$MODEL"
else
  echo "Model $MODEL already present, skipping pull."
fi

# 4. Keep the server in the foreground (also handles SIGTERM)
wait "$pid"
