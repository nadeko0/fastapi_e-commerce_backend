# The official Redis image with Alpine's security updates applied at build
# time, for the same reason as docker/postgres/Dockerfile: a package Alpine
# has fixed is not waited for until Docker Library rebuilds the image.
#
# Redis 8.8: the newest release redis-py 8.1 lists as supported (its README,
# "Supported Redis Versions": 7.2 to 8.8). The base is pinned by tag and
# digest; .github/workflows/ci.yml tests against this exact base.
FROM redis:8.8-alpine@sha256:c027ae0f677dcf73d74038256f5b88c9ba7b11ccbfae97e96a9f091208d36125

RUN apk upgrade --no-cache
