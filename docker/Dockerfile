# syntax=docker/dockerfile:1.7

# ---- builder: resolve and compile the virtualenv every later stage reuses ----
# Pinned deliberately: the floating python3.13-trixie-slim tag now carries uv
# 0.13.1, which cannot extract the legacy logging-0.4.9.6 sdist.
FROM ghcr.io/astral-sh/uv:0.12.5-python3.13-trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependencies resolve in their own layer so source-only edits do not re-resolve.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=README.md,target=README.md \
    uv sync --locked --no-install-project --no-dev

# pyproject declares readme = "README.md", and uv_build copies it into the wheel.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

# ---- development: full dev environment, source is bind-mounted by compose.dev.yml ----
FROM builder AS development

# The runtime stage sets these; the dev stage inherits only the builder's env.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# Re-syncs the project editable plus the dev group (pytest, watchfiles).
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --dev

CMD ["opentelemetry-instrument", "uvicorn", "vouch.api.server:app", \
     "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---- runtime: the shipped image, production dependencies only ----
FROM python:3.13-slim-trixie AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    VOUCH_LOG_DIR=/app/logs

RUN groupadd --system --gid 10001 vouch \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /app vouch

WORKDIR /app

# Interpreter path matches the builder exactly, so the copied venv stays valid.
COPY --from=builder --chown=vouch:vouch /app/.venv /app/.venv

# The one-shot migrate service runs alembic from here.
COPY --chown=vouch:vouch alembic.ini ./
COPY --chown=vouch:vouch alembic ./alembic

# router.py creates its downloads directory next to its own module at import time.
RUN set -eux; \
    SITE="$(/app/.venv/bin/python -c 'import pathlib, vouch; print(pathlib.Path(vouch.__file__).parent)')"; \
    install -d -o vouch -g vouch "$SITE/downloads" /app/logs

# The base image ships pip and ensurepip for build-time use; this app needs neither.
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip \
           /usr/local/lib/python3.13/site-packages/pip-* \
           /usr/local/lib/python3.13/ensurepip \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13

USER vouch

EXPOSE 8000

# /ping is a liveness probe only: it deliberately touches neither Postgres nor Redis.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=5 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ping')"]

CMD ["opentelemetry-instrument", "uvicorn", "vouch.api.server:app", \
     "--host", "0.0.0.0", "--port", "8000"]
