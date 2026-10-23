# syntax=docker/dockerfile:1.4

# Base images by tag and digest: the tag says what it is, the digest makes
# it exactly that image on every build (a tag can be moved to another
# image). The digests are the multi-arch indexes the registries served for
# these tags on 2026-10-21; to move to a newer patch release, look up the
# tag's current digest and replace both together.
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN adduser --disabled-password --gecos '' --no-create-home appuser && \
    mkdir -p /app/logs && \
    chown appuser:appuser /app/logs

COPY --from=builder /app/.venv /app/.venv

COPY . .

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" || exit 1

CMD ["python", "main.py"]