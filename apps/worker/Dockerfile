# syntax=docker/dockerfile:1.9
# Build context is the REPO ROOT (uv workspace), not apps/worker.
#   docker build -f apps/worker/Dockerfile .
#
# A separate image from the API on purpose: different dependency closure,
# different resource limits, different scaling signal (queue depth, not RPS).

FROM python:3.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/
COPY packages/sdk/pyproject.toml packages/sdk/
COPY apps/api/pyproject.toml apps/api/
COPY apps/worker/pyproject.toml apps/worker/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package lo-worker --no-install-workspace

COPY packages/core packages/core
COPY apps/worker apps/worker

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package lo-worker


FROM python:3.13-slim-bookworm AS runtime

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

USER app

# arq traps SIGTERM and finishes in-flight jobs before exiting, so a rolling
# deploy does not abandon a half-finished eval run.
CMD ["arq", "lo_worker.main.WorkerSettings"]
