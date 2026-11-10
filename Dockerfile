# syntax=docker/dockerfile:1.4

# Base images by tag and digest: the tag says what it is, the digest makes
# it exactly that image on every build (a tag can be moved to another
# image). The digests are the multi-arch indexes the registries served for
# these tags on 2026-11-10; to move to a newer patch release, look up the
# tag's current digest and replace both together. The python digest is
# younger than the two-week rule allows, under its security exception: it
# removes apt CVE-2026-109518 (HIGH), which the 2026-10-21 digest carried
# (Trivy 0.76.1, scan of both on 2026-11-10).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./

# OpenTelemetry packages are the optional extra "telemetry": installed only
# with --build-arg INSTALL_TELEMETRY=true (tracing itself stays off until
# OTEL_* variables are set at run time).
ARG INSTALL_TELEMETRY=false

RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$INSTALL_TELEMETRY" = "true" ]; then extra="--extra telemetry"; else extra=""; fi && \
    uv sync --frozen --no-dev --no-install-project $extra

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Debian security updates published after the base image was built: the
# official image is rebuilt on its own schedule, and until then it ships the
# old packages (zlib1g CVE-2026-76844 and CVE-2026-85091 on 2026-11-10, fixed
# in Debian as 1:1.3.dfsg+really1.3.1-1+deb13u1). Only the runtime stage ships.
RUN apt-get update && \
    apt-get upgrade --yes --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

# The base image ships pip (with its own vendored copies of certifi, urllib3,
# msgpack, ...) in the system site-packages. Nothing at run time installs
# packages: the app runs from /app/.venv, built in the builder stage. Without
# pip the image carries none of pip's vendored libraries, so their advisories
# do not apply to it. python:3.13-slim installs no setuptools or wheel.
RUN python -m pip uninstall --yes pip

RUN adduser --disabled-password --gecos '' --no-create-home appuser && \
    mkdir -p /app/logs && \
    chown appuser:appuser /app/logs

COPY --from=builder /app/.venv /app/.venv

# Only what runs: the code, the migrations Alembic applies and pyproject.toml,
# which app/__init__.py reads for the version. Tests, docs, compose files,
# .env.example and uv.lock stay out of the image.
COPY pyproject.toml alembic.ini main.py ./
COPY app ./app
COPY migrations ./migrations

USER appuser

# The port the app listens on (PORT in app/core/config.py, 8000 by default).
# Documentation only: publishing it is up to `docker run -p` or compose.
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" || exit 1

# OCI image annotations (image-spec annotations.md). version, revision and
# created come from the build (CI passes them); a plain `docker build` leaves
# them "unknown". Declared last so a changing value invalidates only metadata.
ARG VERSION=unknown
ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.title="fastapi-ecommerce-backend" \
      org.opencontainers.image.description="FastAPI e-commerce backend: catalog, carts, orders, Stripe payments, Celery workers" \
      org.opencontainers.image.source="https://github.com/nadeko0/fastapi_e-commerce_backend" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

CMD ["python", "main.py"]