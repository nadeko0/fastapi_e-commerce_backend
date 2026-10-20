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
