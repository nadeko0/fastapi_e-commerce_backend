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


def test_redis_appends_every_write_to_disk_on_the_kept_volume():
    # The token blacklist, password-change watermarks, refresh-token state
    # and the Celery queues live in this Redis. With RDB snapshots alone a
    # crash (OOM kill, host loss) loses everything since the last snapshot,
    # and revoked tokens become valid again. AOF with fsync every second
    # bounds the loss to about one second (Redis persistence docs). The AOF
    # directory lives under /data, which is the redis_data volume.
    redis = _without_comments(service_block("redis"))
    assert re.search(r'--appendonly\s*\n\s*- "yes"', redis), redis
    assert re.search(r"--appendfsync\s*\n\s*- everysec\b", redis), redis
    assert re.search(r"^\s*- redis_data:/data\s*$", redis, re.M), redis


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


_INIT_SCRIPT = "docker/postgres/initdb/10-app-role.sh"


def test_postgres_and_redis_are_published_on_loopback_only():
    # A port published without an address listens on every interface, and
    # Docker's own iptables rules put it in front of ufw/firewalld. The
    # services reach both over the compose network; the published ports are
    # for a developer's own machine.
    for service, variable, port in (("db", "DOCKER_POSTGRES_PORT", 5432), ("redis", "DOCKER_REDIS_PORT", 6379)):
        block = _without_comments(service_block(service))
        published = re.findall(r'^\s*- "([^"]+)"\s*$', block[block.index("ports:"):], re.M)[0]
        assert published == f"127.0.0.1:${{{variable}:-{port}}}:{port}", (service, published)


def test_the_application_connects_as_its_own_role_not_the_superuser():
    # POSTGRES_USER is the superuser the image creates and the owner of the
    # schema (migrations); the api and Celery services override it with the
    # unprivileged role the init script creates.
    for service in ("api", "celery-worker", "celery-beat"):
        block = _without_comments(service_block(service))
        assert re.search(r"POSTGRES_USER: \$\{APP_DB_USER:\?[^}]+\}", block), service
        assert re.search(r"POSTGRES_PASSWORD: \$\{APP_DB_PASSWORD:\?[^}]+\}", block), service


def test_the_database_creates_that_role_on_first_start():
    db = _without_comments(service_block("db"))
    assert "./docker/postgres/initdb:/docker-entrypoint-initdb.d:ro" in db
    assert re.search(r"APP_DB_USER=\$\{APP_DB_USER:\?[^}]+\}", db)
    assert re.search(r"APP_DB_PASSWORD=\$\{APP_DB_PASSWORD:\?[^}]+\}", db)


def test_migrations_have_a_service_that_runs_them_as_the_owner():
    migrate = _without_comments(service_block("migrate"))
    assert "alembic" in migrate and "upgrade" in migrate and "head" in migrate
    assert "APP_DB_USER" not in migrate
    assert re.search(r"profiles:\s*\n\s*- migrate", migrate)


def test_the_init_script_grants_data_access_only():
    script = _read(_INIT_SCRIPT)
    assert "NOSUPERUSER" in script and "NOCREATEDB" in script and "NOCREATEROLE" in script
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public" in script
    assert "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public" in script
    # Tables the migrations create later, as the owner, are granted too.
    assert 'ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public' in script
    assert "ALL PRIVILEGES" not in script and "GRANT CREATE" not in script
    # The password reaches psql through its environment (\getenv), not its
    # command line, and the role must not be the superuser itself.
    assert r"\getenv app_password APP_DB_PASSWORD" in script
    assert '"$APP_DB_USER" = "$POSTGRES_USER"' in script


def test_the_init_script_keeps_unix_line_endings():
    # Run by sh in the container: a checkout with CRLF breaks it there.
    with open(os.path.join(_ROOT, _INIT_SCRIPT), "rb") as f:
        assert b"\r\n" not in f.read()
    assert re.search(r"^\*\.sh\s+text\s+eol=lf\s*$", _read(".gitattributes"), re.M)


_PINNED = r"@sha256:[0-9a-f]{64}$"


def test_base_images_are_pinned_by_digest_on_their_current_majors():
    # A tag alone can point at a different image on the next pull: builds
    # stop being reproducible and a replaced image is taken unnoticed. The
    # tag stays in front of the digest for the reader (Docker uses the
    # digest). Majors as they were: python 3.13, postgres 15, redis 7,
    # uv 0.13.5.
    dockerfile = _read("Dockerfile")
    references = re.findall(r"^FROM (\S+)", dockerfile, re.M)
    stages = set(re.findall(r"^FROM \S+ AS (\S+)", dockerfile, re.M))
    references += [
        ref for ref in re.findall(r"^COPY --from=(\S+)", dockerfile, re.M) if ref not in stages
    ]
    references += re.findall(r"^\s*image: (\S+)", _read("docker-compose.yml"), re.M)
    expected_tags = [
        "python:3.13-slim", "python:3.13-slim", "ghcr.io/astral-sh/uv:0.13.5",
        "postgres:15-alpine", "redis:7-alpine",
    ]
    assert sorted(ref.split("@")[0] for ref in references) == sorted(expected_tags), references
    for ref in references:
        assert re.search(_PINNED, ref), ref


def test_ci_tests_against_the_postgres_image_production_runs():
    # The test-postgres job's service container must be the db service's image,
    # tag and digest: a Postgres-only behaviour is only worth testing on the
    # server that is deployed. Bumping one without the other fails here.
    compose_image = re.search(r"^\s*image: (postgres:\S+)", service_block("db"), re.M).group(1)
    ci = _read(".github/workflows/ci.yml")
    job = ci[ci.index("\n  test-postgres:"):]
    job = job[: job.index("\n  docker-build:")]
    assert re.search(rf"^\s*image: {re.escape(compose_image)}\s*$", job, re.M)
    assert "test-postgres" in re.search(r"docker-build:.*?needs: \[([^\]]*)\]", ci, re.S).group(1)


def test_celery_worker_gets_time_to_finish_its_task_on_stop():
    # `docker stop` sends SIGTERM (Celery: warm shutdown, running tasks
    # finish) and SIGKILL after stop_grace_period, 10 s by default. A task
    # killed mid-run is, with acks_late and the Redis broker, redelivered
    # only after the visibility timeout (1 hour). The grace period has to
    # outlast the longest task: the hard time limit.
    from app import tasks

    worker = _without_comments(service_block("celery-worker"))
    grace = re.search(r"^\s*stop_grace_period: (\d+)s\s*$", worker, re.M)
    assert grace, worker
    assert int(grace.group(1)) > tasks.celery.conf.task_time_limit
    assert "stop_signal" not in worker  # the default, SIGTERM, is the warm shutdown
