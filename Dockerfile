# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------- builder stage
FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY requirements.txt .

# Build wheels for every dependency so the runtime stage can install
# without needing compilers or network access.
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ---------------------------------------------------------------- runtime stage
FROM python:3.13-slim AS runtime

LABEL org.opencontainers.image.title="image-organizer" \
      org.opencontainers.image.description="AI-powered image categorizer with web UI, CLI, and MCP server" \
      org.opencontainers.image.source="https://github.com/testllm008-commits/image-organizer" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    IMGORG_DOCKER=1

# Create an unprivileged user — the container should not run as root.
RUN groupadd --system --gid 1000 imgorg \
 && useradd  --system --uid 1000 --gid 1000 --create-home imgorg

WORKDIR /app

# Install pre-built wheels from the builder stage.
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
 && rm -rf /wheels

# Project source. .dockerignore keeps tests, .venv, .git, etc. out.
COPY organizer ./organizer
COPY assets    ./assets
COPY README.md ./
COPY docker/entrypoint.sh /usr/local/bin/imgorg-entrypoint
RUN chmod +x /usr/local/bin/imgorg-entrypoint

# Default volume locations — mount your real folders here.
RUN mkdir -p /data/source /data/output \
 && chown -R imgorg:imgorg /app /data
VOLUME ["/data/source", "/data/output"]

USER imgorg

# Web UI listens on this port inside the container.
EXPOSE 8765

ENTRYPOINT ["imgorg-entrypoint"]
CMD ["web"]
