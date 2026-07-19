# Stage 1: Fetch a precompiled Stockfish binary.
# The official Stockfish releases ship dynamically linked Linux x86-64 binaries
# with the NNUE network embedded, so there is nothing to compile and no weights
# file to download.
FROM debian:bookworm-slim AS engine

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Extract into a dedicated dir: the tarball unpacks a "stockfish/" folder, which
# would collide with the /stockfish target if we worked from the root.
WORKDIR /build

# avx2 is a good default for modern x86-64 CPUs. On older CPUs/VMs that raise
# "Illegal instruction", rebuild with SF_VARIANT=stockfish-ubuntu-x86-64-sse41-popcnt.
ARG SF_VARIANT=stockfish-ubuntu-x86-64-avx2
# Extract into a temp dir: the archive unpacks a "stockfish/" folder, which would
# otherwise collide with the "/stockfish" destination (cwd is "/" in this stage).
RUN curl -fL "https://github.com/official-stockfish/Stockfish/releases/download/sf_18/${SF_VARIANT}.tar" -o /tmp/sf.tar \
    && mkdir -p /tmp/sf \
    && tar -xf /tmp/sf.tar -C /tmp/sf \
    && mv "/tmp/sf/stockfish/${SF_VARIANT}" /stockfish \
    && chmod +x /stockfish \
    && rm -rf /tmp/sf /tmp/sf.tar

# Stage 2: The Django application image.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# libstdc++6 is the runtime library the dynamically linked Stockfish binary
# needs; it is not guaranteed to be present in the slim base image.
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# The chess engine now runs as a local subprocess inside this container instead
# of a separate service; STOCKFISH_PATH tells the coach where to find it.
COPY --from=engine /stockfish /usr/local/bin/stockfish
ENV STOCKFISH_PATH=/usr/local/bin/stockfish

COPY pyproject.toml .

RUN pip install --no-cache-dir .

COPY manage.py entrypoint.sh ./
COPY chessdotcom_ai_coach ./chessdotcom_ai_coach
COPY theme ./theme

RUN chmod +x entrypoint.sh

EXPOSE 8000

CMD ["./entrypoint.sh"]
