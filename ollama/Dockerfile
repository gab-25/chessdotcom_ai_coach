FROM ollama/ollama:latest

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Override the base image ENTRYPOINT (/bin/ollama serve)
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
