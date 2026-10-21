"""docker-compose.yml, read as text.

No YAML parser is a dependency of this project and Docker is not available
to the test suite, so these checks look at the lines of one service's block
(everything indented under "  <service>:"). Crude, and enough for what they
pin: settings whose absence was a finding of the security review.
"""

import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(name):
    with open(os.path.join(_ROOT, name), encoding="utf-8") as f:
        return f.read()


def service_block(name):
    lines = _read("docker-compose.yml").splitlines()
    start = lines.index(f"  {name}:")
    block = []
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith("   ") and not line.startswith("  #"):
            break
        block.append(line)
    return "\n".join(block)


def _without_comments(text):
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_redis_memory_is_capped_and_never_evicted():
    # Without maxmemory a flood of guest carts grows Redis until the kernel
    # kills the container, taking the Celery queue, the token blacklist and
    # every cart with it. With an eviction policy Redis would instead drop
    # keys: allkeys-* can drop the Celery queue, and volatile-* the token
    # blacklist and password-change watermarks (they carry a TTL), which
    # would make revoked tokens valid again. noeviction drops nothing and
    # refuses writes when full.
    redis = _without_comments(service_block("redis"))
    assert re.search(r"--maxmemory\s*\n\s*- \$\{REDIS_MAXMEMORY:-\d+[kmg]b\}", redis), redis
    assert re.search(r"--maxmemory-policy\s*\n\s*- noeviction\b", redis), redis


def test_redis_refuses_to_start_without_a_password():
    # ${REDIS_PASSWORD} empty or unset turned into "--requirepass" with no
    # argument (or an empty one): Redis exited with a config error, stayed
    # unhealthy, and nothing depending on it started. ${VAR:?message} makes
    # `docker compose` itself stop with that message instead.
    redis = _without_comments(service_block("redis"))
    assert re.search(r"--requirepass\s*\n\s*- \$\{REDIS_PASSWORD:\?[^}]+\}", redis), redis
    assert "${REDIS_PASSWORD}" not in redis


def test_redis_healthcheck_authenticates_without_the_password_in_its_arguments():
    # An unauthenticated `redis-cli ping` gets NOAUTH from a Redis with a
    # password and the container is never healthy. redis-cli reads
    # REDISCLI_AUTH from its environment, which keeps the password out of
    # the healthcheck's command line (`-a` would also print a warning).
    redis = _without_comments(service_block("redis"))
    assert re.search(r"REDISCLI_AUTH: \$\{REDIS_PASSWORD:\?[^}]+\}", redis), redis
    healthcheck = redis[redis.index("healthcheck:"):]
    assert "redis-cli ping" in healthcheck
    # Exit status alone is not trusted: the reply has to be PONG.
    assert "grep -q PONG" in healthcheck
    assert " -a " not in healthcheck and "REDIS_PASSWORD" not in healthcheck
