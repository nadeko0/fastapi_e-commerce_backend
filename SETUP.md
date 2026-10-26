# Setup and Local Development

Getting a transactional e-commerce backend running locally should be predictable: clone the repository, synchronize pinned dependencies, configure environment variables, and run migrations.

## Prerequisites

- **Python 3.13** or higher
- **uv 0.13+** for dependency management ([installation guide](https://docs.astral.sh/uv/getting-started/installation/))
- **Docker Engine 24.0+** and **Docker Compose v2.20+**
- **Git**
- Optional: **Stripe CLI** for local webhook forwarding

## Local Development Setup

### 1. Synchronize Dependencies

This project manages virtual environments and dependencies with `uv`:

```bash
uv sync
```

This creates `.venv` and installs the exact dependency versions locked in `uv.lock`. Execute subsequent commands using `uv run <command>`, or activate the environment directly via `.venv\Scripts\activate` (Windows) or `source .venv/bin/activate` (Linux/macOS).

### 2. Configure Environment Variables

Copy the template configuration to create a local `.env`:

```bash
cp .env.example .env
```

The application uses Pydantic Settings (`app/core/config.py`). The following settings have no defaults and **must** be defined in `.env`:

| Setting Name | Purpose | Example Value for Local Development |
|---|---|---|
| `ENVIRONMENT` | Runtime mode: `development`, `test`, `staging`, or `production` | `development` |
| `JWT_SECRET_KEY` | HMAC secret for signing access and refresh tokens | Random 64-character hex string (`openssl rand -hex 32`) |
| `POSTGRES_SERVER` | PostgreSQL server hostname or IP | `localhost` (or `db` inside Docker Compose) |
| `POSTGRES_USER` | PostgreSQL superuser and schema owner | `postgres` |
| `POSTGRES_PASSWORD` | PostgreSQL superuser password | Local database password |
| `POSTGRES_DB` | PostgreSQL database name | `ecommerce` |
| `REDIS_HOST` | Redis server hostname or IP | `localhost` (or `redis` inside Docker Compose) |
| `SMTP_HOST` | Mail transfer agent hostname | `localhost` |
| `SMTP_USER` | Mail authentication username | Local username or placeholder |
| `SMTP_PASSWORD` | Mail authentication password | Local password or placeholder |
| `EMAILS_FROM_EMAIL` | Sender address on transactional emails | `noreply@example.com` |
| `EMAILS_FROM_NAME` | Sender display name | `Store Support` |

#### Environment Modes (`ENVIRONMENT`)

The `ENVIRONMENT` variable dictates security constraints and runtime behavior:
- `development`: Automatically forces `DEBUG=True`, `RELOAD=True`, and `WORKERS=1`. Connection pool sizes (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`) are halved. Interactive OpenAPI documentation is enabled at `/api/v1/docs` and `/redoc`. Deployment secret strength checks are skipped.
- `test`: Used by pytest. Allows `LOG_LEVEL=DEBUG`, skips production secret checks, and maintains interactive documentation.
- `staging` and `production`: Enforces strict production deployment gates in `Settings.check_deployment_settings`. Forbids `LOG_LEVEL=DEBUG`, requires `BCRYPT_ROUNDS >= 12`, enforces `PAYMENT_PROVIDER=stripe`, mandates non-placeholder secrets meeting minimum length requirements, and requires `FRONTEND_URL` to start with `https://`. In `production`, interactive documentation (`/api/v1/docs`, `/redoc`, and `/api/v1/openapi.json`) is disabled (returns 404).

### 3. Running Services Locally

Start local PostgreSQL and Redis servers, then run database migrations:

```bash
uv run alembic upgrade head
```

Run the application components in separate terminal windows:

```bash
# 1. FastAPI application (uvicorn)
uv run python main.py
# (On Windows, main.py explicitly selects SelectorEventLoop for psycopg async compatibility)

# 2. Celery background worker
uv run celery -A app.tasks worker --loglevel=info

# 3. Celery beat periodic scheduler
uv run celery -A app.tasks beat --loglevel=info
```

The API will be available at `http://localhost:8000` (docs at `http://localhost:8000/api/v1/docs`).

## Docker Compose Setup

Docker Compose provides a complete, isolated environment matching production topology.

### 1. Compose Configuration and Roles

Inside `docker-compose.yml`, services connect using internal container hostnames:
- `POSTGRES_SERVER=db`
- `REDIS_HOST=redis`
- `REDIS_PASSWORD` is strictly required (`:?` expansion prevents starting unauthenticated)

PostgreSQL implements role separation via `docker/postgres/initdb/10-app-role.sh`:
- `POSTGRES_USER`: Superuser and schema owner. Owns the tables and executes schema migrations.
- `APP_DB_USER`: Unprivileged runtime role used by the `api`, `celery-worker`, and `celery-beat` services. It possesses DML rights (SELECT, INSERT, UPDATE, DELETE) and sequence usage, but zero DDL rights (cannot CREATE, ALTER, or DROP tables).

### 2. Build and Start the Stack

```bash
# Start background infrastructure (db, redis) and runtime services (api, celery-worker, celery-beat)
docker compose up --build -d

# Check service health
docker compose ps

# View logs
docker compose logs -f api
```

### 3. Run Database Migrations

Because the `api` container runs under the unprivileged `APP_DB_USER` role, it cannot modify database schemas. Migrations must be executed via the dedicated `migrate` profile as the schema owner:

```bash
docker compose --profile migrate run --rm migrate
```

> [!WARNING]
> Never attempt to run `docker compose exec api alembic upgrade head`. The runtime application role has no DDL permissions, and Alembic will fail with a PostgreSQL permission denied error.

Published host ports:
- API: `http://localhost:8000` (controlled by `DOCKER_API_PORT`, container binds to internal 8000)
- PostgreSQL: `127.0.0.1:5432` (loopback only, controlled by `DOCKER_POSTGRES_PORT`)
- Redis: `127.0.0.1:6379` (loopback only, controlled by `DOCKER_REDIS_PORT`)

## Creating the First Administrator

The system does not provide a command-line account creation utility. To create an administrator:

1. Register a standard user account through the API (`POST /api/v1/users/register`) or interactive documentation:
   ```bash
   curl -X POST http://localhost:8000/api/v1/users/register \
     -H "Content-Type: application/json" \
     -d '{"email": "admin@example.com", "password": "SuperSecretPassword123!", "gdpr_consent": true, "privacy_policy_accepted": true}'
   ```
2. Promote the user to the `admin` role (and mark the e-mail verified) as the database owner. The variables are expanded inside the `db` container, which has them from `.env`:
   ```bash
   docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
   UPDATE users SET role = 'admin', is_email_verified = true
   WHERE lower(email) = lower('admin@example.com');
   SQL
   ```

For a local non-Docker PostgreSQL database, execute the same query via `psql -U postgres -d ecommerce`.

## Payment Configuration

### Mock Provider

For offline testing without a Stripe account, keep the default setting:

```env
PAYMENT_PROVIDER=mock
```

The mock provider simulates successful transactions, test-card declines, and refund lifecycles entirely in-memory without making external network calls.

### Real Stripe Development

To integrate with Stripe test mode:

1. In `.env`, set `PAYMENT_PROVIDER=stripe` and provide your test keys:
   ```env
   PAYMENT_PROVIDER=stripe
   STRIPE_SECRET_KEY=<your-stripe-secret-key>
   ```
2. Forward incoming Stripe webhooks to your local API instance using the Stripe CLI:
   ```bash
   stripe listen --forward-to http://localhost:8000/api/v1/orders/webhooks/stripe
   ```
3. Copy the webhook signing secret printed by the Stripe CLI into `.env`:
   ```env
   STRIPE_WEBHOOK_SECRET=<your-webhook-signing-secret>
   ```
4. Restart the API and Celery worker so they read the updated secrets.

Standard Stripe test payment method tokens:
- `pm_card_visa`: Generates an immediately successful charge (`PaymentStatus.PAID`).
- `pm_card_visa_chargeDeclined`: Simulates a declined card (`card_declined / generic_decline`).
- `pm_card_createDispute`: Triggers a chargeback dispute webhook (`charge.dispute.created`), placing the associated order on hold for administrative review.

## Running Tests

The test suite includes over 5,200 automated tests covering unit logic, PostgreSQL concurrency, property-based invariants, and Stripe test-mode workflows.

To run the full suite locally:

```bash
uv run pytest
```

See [docs/TESTING.md](docs/TESTING.md) for details on testing against real PostgreSQL engines, running property fuzzing with Hypothesis, and executing mutation tests.

See also: [docs/OPERATIONS.md](docs/OPERATIONS.md), [docs/TESTING.md](docs/TESTING.md), [docs/LIMITATIONS.md](docs/LIMITATIONS.md).
