# The official PostgreSQL image with Alpine's security updates applied at build
# time. Docker Library rebuilds its images on its own schedule; between two
# rebuilds the image carries packages Alpine has already fixed (on 2026-11-10:
# krb5-libs 1.22.2-r1 and libxslt 1.1.43-r3), which the CI scan fails on.
# Upgrading here keeps the deployed database patched without waiting, the same
# way the app image does with apt (see the root Dockerfile).
#
# The base is pinned by tag and digest as everywhere else; .github/workflows/ci.yml
# tests against this exact base (test_docker_compose.py keeps them equal).
FROM postgres:18.6-alpine@sha256:123b17998ccaf47e53b502e8c6a1e79afd8f579c6f3eec83c563da026f957395

RUN apk upgrade --no-cache
