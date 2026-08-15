# syntax=docker/dockerfile:1
#
# ⚠ NOT YET VERIFIED — never built. See docs/PHASE-01-COMPLETION.md.
#
# Python is pinned to 3.11 to match the version Phase 1 was developed and tested
# against, so the container and a local checkout cannot disagree about runtime
# behaviour.

FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# uv is pinned rather than :latest — an unpinned build tool means the image is
# not reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.11.23 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies resolve in their own layer, before any source is copied, so
# editing application code does not invalidate the dependency cache.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

COPY alembic.ini ./
COPY migrations ./migrations
COPY infrastructure/docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# Non-root: the API has no reason to run with root privileges, and BUILD_SPEC
# §17 requires safe action boundaries.
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser
EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
