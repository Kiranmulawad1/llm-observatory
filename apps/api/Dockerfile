# syntax=docker/dockerfile:1.9
# Build context is the REPO ROOT (uv workspace), not apps/api.
#   docker build -f apps/api/Dockerfile .

FROM python:3.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Manifests first, source second. Dependency resolution is the slow layer and
# only the manifests invalidate it, so day-to-day source edits reuse the cache.
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/
COPY packages/sdk/pyproject.toml packages/sdk/
COPY apps/api/pyproject.toml apps/api/
COPY apps/worker/pyproject.toml apps/worker/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package lo-api --no-install-workspace

COPY packages/core packages/core
COPY apps/api apps/api

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package lo-api


FROM python:3.13-slim-bookworm AS runtime

# Non-root by default. The Kubernetes SecurityContext also enforces
# runAsNonRoot, but an image that only works as root makes that impossible.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

USER app
EXPOSE 8000

# No --reload and no shell form: PID 1 must be uvicorn itself so SIGTERM reaches
# it and Kubernetes gets a graceful drain instead of a 30s kill timeout.
CMD ["uvicorn", "lo_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
