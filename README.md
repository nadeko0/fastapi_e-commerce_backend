# FastAPI E-commerce Backend

A production-ready e-commerce backend API built with FastAPI, implementing SOLID principles and providing comprehensive features for managing products, orders, users, and more. The project follows best practices for scalability, maintainability, and security.

## SOLID Principles Implementation

### Single Responsibility Principle (SRP)
- Each service class has a single responsibility (e.g., `EmailService`, `GDPRService`)
- Clear separation between models, schemas, and services
- Dedicated modules for specific functionalities (auth, email, GDPR)

### Open/Closed Principle (OCP)
- Base models and schemas that can be extended
- Middleware system for adding new functionality
- Plugin-based architecture for easy feature additions

### Liskov Substitution Principle (LSP)
- Proper inheritance in models (Base model classes)
- Consistent interface implementations
- Type hints and proper abstract classes usage

### Interface Segregation Principle (ISP)
- Focused API endpoints for specific functionalities
- Granular Pydantic schemas
- Specific dependencies for different authentication levels

### Dependency Inversion Principle (DIP)
- Dependency injection throughout the application
- Abstract base classes for services
- Configuration through environment variables

## Features

### User Management
- Registration and authentication with JWT
- Role-based access control (Admin/Client)
- Email verification
- Password reset functionality
- GDPR compliance with consent management
- User profile management

### Product Management
- Product CRUD operations
- Category management with hierarchical structure
- Product search and filtering
- Stock management
- Image handling

### Order Management
- Shopping cart functionality
- Order processing
- Order status tracking
- Order history
- Email notifications

### Security
- JWT authentication with refresh tokens
- Token blacklisting
- Password hashing with bcrypt
- CORS protection
- Rate limiting
- Security headers
- SQL injection protection
- XSS protection
- CSRF protection

### Data Management
- Redis caching
- Database migrations
- GDPR data export
- Data retention policies

## CRUD Operations Overview

### Products
```python
# Create
POST /api/v1/products
{
    "name": "Product Name",
    "description": "Description",
    "price": 99.99,
    "stock": 100
}

# Read
GET /api/v1/products
GET /api/v1/products/{id}

# Update
PUT /api/v1/products/{id}
{
    "name": "Updated Name",
    "price": 89.99
}

# Delete
DELETE /api/v1/products/{id}
```

### Orders
```python
# Create
POST /api/v1/orders
{
    "items": [
        {"product_id": 1, "quantity": 2}
    ],
    "shipping_address_id": 1
}

# Read
GET /api/v1/orders
GET /api/v1/orders/{id}

# Update (Status)
PUT /api/v1/orders/{id}/status
{
    "status": "processing"
}

# Delete (Cancel)
DELETE /api/v1/orders/{id}
```

### Users
```python
# Create
POST /api/v1/users/register
{
    "email": "user@example.com",
    "password": "secure_password",
    "full_name": "John Doe"
}

# Read
GET /api/v1/users/me
GET /api/v1/users/{id} (admin only)

# Update
PUT /api/v1/users/me
{
    "full_name": "John Smith",
    "phone": "+1234567890"
}

# Delete
DELETE /api/v1/users/me
```

## Technical Stack

- **Runtime**: Python 3.13
- **Framework**: FastAPI 0.141.1 (Starlette 1.6.0)
- **Database**: PostgreSQL with SQLAlchemy 2.0.52
- **Caching**: Redis 8.1.0 (redis-py client)
- **Task Queue**: Celery 5.6.3
- **Authentication**: JWT (python-jose 3.5.0) with refresh tokens
- **Password hashing**: bcrypt 5.0.0, called directly (no passlib wrapper)
- **Email**: SMTP integration
- **Documentation**: OpenAPI (Swagger)
- **Packaging**: [uv](https://docs.astral.sh/uv/) (`pyproject.toml` + `uv.lock`)

All dependencies track latest stable. Two version decisions are worth
calling out because they weren't just "bump and go":
- `python-jose` 3.3.0 had CVE-2024-33663 (algorithm confusion); 3.5.0 fixes it.
- `fastapi` jumped from the 0.115.x line straight to 0.141.x specifically to
  pull in a Starlette release patched against CVE-2025-62727 (a DoS via a
  crafted `Range` header) - FastAPI 0.115.x pins an older Starlette that
  can't take the fix alone.
- `passlib` (last released 2020, unmaintained) breaks outright on bcrypt
  5.x - hashing raises `ValueError: password cannot be longer than 72
  bytes` for any password, because its bcrypt-version-detection shim relies
  on an attribute bcrypt 5.x removed. Rather than pin bcrypt back to 4.x,
  `app/core/security.py` now calls `bcrypt.hashpw`/`bcrypt.checkpw`
  directly and passlib was dropped entirely.

## Project Structure

```
app/
├── api/                    # API endpoints
│   ├── v1/                # API version 1
│   │   ├── admin.py       # Admin endpoints
│   │   ├── cart.py        # Shopping cart
│   │   ├── orders.py      # Order management
│   │   ├── products.py    # Product catalog
│   │   ├── users.py       # User management
│   │   └── legal.py       # Legal & GDPR
│   └── deps.py            # Dependencies
├── core/                  # Core functionality
│   ├── config.py          # Settings
│   ├── database.py        # DB setup
│   ├── security.py        # Security
│   └── logging_config.py  # Logging
├── models/                # Database models
├── schemas/               # Pydantic schemas
├── services/             # Business logic
└── main.py               # Entry point
```

## Quick Start (uv)

### Prerequisites
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- PostgreSQL 15 and Redis 7 (or Docker, see below)

```bash
git clone <repository-url>
cd fastapi-ecommerce
cp .env.example .env        # edit with your local DB/Redis/SMTP settings
uv sync                     # creates .venv and installs pinned deps from uv.lock
uv run alembic upgrade head
uv run python main.py       # or: uv run uvicorn app.main:app --reload
```

Run the test suite with `uv run pytest --cov=app`.

## Docker Setup

The actual [`Dockerfile`](Dockerfile) and [`docker-compose.yml`](docker-compose.yml)
in the repo root are the source of truth; they build the API image with `uv`
(dependencies resolved from `uv.lock`, not re-resolved at build time) and run
Postgres and Redis alongside it.

### Running with Docker

1. Build and start services:
```bash
docker-compose up --build
```

2. Run migrations:
```bash
docker-compose exec api alembic upgrade head
```

## GDPR / Data Protection

This is a portfolio project, not a legal audit — full regulatory compliance
depends on organizational measures outside a codebase (a real DPA, an actual
DPO, verified breach-response procedures) as well as the code. What's
implemented here targets the technical requirements of EU GDPR
(Regulation (EU) 2016/679) that apply to an e-commerce backend:

- **Lawful basis & consent (Art. 6, 7)** — registration requires explicit
  GDPR consent and privacy-policy acceptance; marketing consent is separate
  and optional. GDPR consent can't be "withdrawn" via the consent endpoint
  (only removed by requesting account deletion, per Art. 6(1)(b) — it's
  needed to fulfil the contract); marketing consent toggles freely either
  way, matching Art. 7(3)'s "as easy to withdraw as to give."
- **Consent audit trail (Art. 7(1))** — every consent-affecting action
  (registration, consent updates) appends a `{type, granted, timestamp,
  ip_address, user_agent}` record to `consent_history`, in one consistent
  shape across both places consent can be changed.
- **Right of access & portability (Art. 15, 20)** — `GET /users/data/export`
  returns personal data, consent history, addresses, and orders as
  structured JSON.
- **Right to erasure (Art. 17)** — `POST /users/data/delete` deactivates the
  account immediately; a scheduled task anonymizes the account (email,
  name, phone, addresses) after a 1-day grace period
  (`DATA_DELETION_GRACE_PERIOD_DAYS`). Order/invoice rows are **not**
  deleted — Art. 17(3)(b) exempts data still needed for a legal obligation,
  and most EU member states require invoices retained for tax purposes for
  around 10 years, so financial records survive with their personal
  identifiers scrubbed rather than being cascade-deleted with the account.
- **Data minimization (Art. 5(1)(c))** — no collection beyond what
  registration/checkout/delivery actually need.

**Known gaps, deliberately not built** (see `.agent-notes/gdpr.md` for the
full reasoning — that file is local/gitignored, not part of the repo):
automatic deletion purely on retention-period expiry (only explicit erasure
requests are acted on), Art. 18 restriction-of-processing as a distinct
state, a separate Art. 21 objection endpoint (covered by the marketing
toggle for the one unconditional objection right, Art. 21(2)), Art. 30
records of processing and Art. 33/34 breach notification (both
organizational/process documents, not app features), and field-level
envelope encryption for crypto-shredding PII (the anonymize-in-place
approach above reaches the same legal outcome without a KMS dependency).

## API Documentation

- Swagger UI: `http://localhost:8000/api/v1/docs`
- ReDoc: `http://localhost:8000/api/v1/redoc`

## Production Deployment Checklist

- [ ] Set `ENVIRONMENT=production` in .env
- [ ] Configure proper logging
- [ ] Set up monitoring (e.g., Prometheus + Grafana)
- [ ] Configure proper CORS origins
- [ ] Use secure SMTP settings
- [ ] Set up database backups
- [ ] Configure rate limiting
- [ ] Set up proper caching strategies
- [ ] Enable HTTPS
- [ ] Set up CI/CD pipeline
- [ ] Configure error tracking (e.g., Sentry)
- [ ] Set up load balancing
- [ ] Configure database connection pooling
- [ ] Set up automated backups
- [ ] Configure monitoring alerts

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.