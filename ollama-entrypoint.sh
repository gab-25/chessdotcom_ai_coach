#!/bin/sh
# Entrypoint for the `ollama` service.
#
# The image has no auto-pull (unlike llama-server's -hf), so we pull the coach
# model here: the blob lands in the ollama-data volume, which makes every later
# start a no-op. The server runs in the background only while we pull, then we
# hand the container's lifetime back to it.
set -e

ollama serve &
server_pid=$!

# `ollama list` only succeeds once the HTTP API is accepting connections.
until ollama list >/dev/null 2>&1; do
  sleep 1
done

ollama pull "${OLLAMA_MODEL:-llama3.2:3b}"

wait "$server_pid"
