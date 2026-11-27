# The official Redis image with Alpine's security updates applied at build
# time, for the same reason as docker/postgres/Dockerfile: a package Alpine
# has fixed is not waited for until Docker Library rebuilds the image.
#
# Redis 8.8: the newest release redis-py 8.1 lists as supported (its README,
# "Supported Redis Versions": 7.2 to 8.8). The base is pinned by tag and
# digest; .github/workflows/ci.yml tests against this exact base.
FROM redis:8.8-alpine@sha256:1273af45b299d2b8b60d0e400d90aa6c2c5b1794f01d6a7ca60ff3ba6e65efb5

RUN apk upgrade --no-cache
