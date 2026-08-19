# Setup Instructions

This document provides detailed instructions for setting up the E-commerce Backend project in both local and Docker environments.

## Prerequisites

### Local Development
- Python 3.13 or higher
- PostgreSQL 15+ (the Docker setup pins `postgres:15-alpine`; local dev has
  been tested against PostgreSQL 18 too)
- Redis 7+
- Git

### Docker Development
- Docker Engine 24.0+
- Docker Compose v2.20+
- Git

## Local Setup

1. **Clone the Repository**
```bash
git clone https://github.com/nadeko0/fastapi_e-commerce_backend.git
cd fastapi_e-commerce_backend
```

2. **Install [uv](https://docs.astral.sh/uv/getting-started/installation/)**

uv manages the virtual environment for you - there's no separate `venv`
creation step.

3. **Install Dependencies**
```bash
uv sync
```
This creates `.venv` and installs the exact versions pinned in `uv.lock`.
Prefix subsequent commands with `uv run` (e.g. `uv run alembic upgrade head`),
or activate the environment directly with `.venv\Scripts\activate` (Windows)
/ `source .venv/bin/activate` (Linux/Mac).

4. **Set Up Environment Variables**
```bash
cp .env.example .env
# Edit .env with your configurations
```

5. **Set Up PostgreSQL**
```bash
# Create database
createdb ecommerce

# Or using psql
psql -U postgres
CREATE DATABASE ecommerce;
```

6. **Run Migrations**
```bash
uv run alembic upgrade head
```
The initial migration (`migrations/versions/`, tracked in git) has been
generated and applied against a real local PostgreSQL 18 instance, verified
with `alembic check` (zero drift between the migration and the current
models) and `scripts/pg_smoke_test.py` (see below). If you change a model,
regenerate with `uv run alembic revision --autogenerate -m "description"`
and re-run `alembic check` before committing the new revision.

7. **(Optional) Seed demo data**

To get a working catalog to develop against without hand-creating
categories/products, run the demo seed script after migrating:
```bash
uv run python scripts/seed_demo_data.py
```
This creates a small illustrative category tree (a generic "general
store" catalog - replace it with your own domain), a dozen or so
products (a couple with `ProductVariant`s), and one admin user plus one
regular user. Credentials are generated fresh each run and printed to
stdout - they are not stored anywhere, so save them from the terminal
output. Safe to re-run: existing categories/products/users (matched by
name/email) are skipped rather than duplicated.

8. **(Optional) Run the Postgres-specific smoke test**

The main test suite (`uv run pytest`) runs against an in-memory SQLite
database for speed - it does not have Postgres available and cannot
validate Postgres-only behavior (ARRAY columns, JSONB path filtering,
partial unique indexes). After `alembic upgrade head` against a real,
disposable Postgres database, run:
```bash
uv run python scripts/pg_smoke_test.py
```
This TRUNCATEs the tables it touches - point it at a throwaway database,
never a real one.

9. **Start Redis Server**
```bash
# Windows (if using WSL)
wsl sudo service redis-server start

# Linux/Mac
sudo service redis-server start
```

10. **Run the Application**
```bash
uv run python main.py
# or: uv run uvicorn app.main:app --reload
```

## Docker Setup

1. **Clone the Repository**
```bash
git clone https://github.com/nadeko0/fastapi_e-commerce_backend.git
cd fastapi_e-commerce_backend
```

2. **Set Up Environment Variables**
```bash
cp .env.example .env
# Edit .env with your configurations
# Make sure to use 'db' as POSTGRES_SERVER and 'redis' as REDIS_HOST
```

3. **Build and Start Services**
```bash
# Build and start all services
docker compose up --build -d

# View logs
docker compose logs -f

# Check service status
docker compose ps
```

4. **Run Migrations**
```bash
docker compose exec api alembic upgrade head
```

5. **(Optional) Seed Demo Data (creates an admin user, among other things)**
```bash
docker compose exec api python scripts/seed_demo_data.py
```

## Development Workflow

1. **Code Style**
- Follow PEP 8 guidelines
- Use type hints
- Write docstrings for functions and classes
- Keep functions small and focused

2. **Git Workflow**
```bash
# Create feature branch
git checkout -b feature/your-feature

# Make changes and commit
git add .
git commit -m "feat: your feature description"

# Push changes
git push origin feature/your-feature
```

3. **Running Tests**
```bash
# Local
uv run pytest --cov=app

# Docker
docker compose exec api uv run pytest --cov=app
```

4. **API Documentation**
- Swagger UI: http://localhost:8000/api/v1/docs
- ReDoc: http://localhost:8000/api/v1/redoc

## Common Issues and Solutions

### Database Connection Issues
1. Check PostgreSQL service is running
2. Verify database credentials in .env
3. Ensure database exists
4. Check network connectivity

### Redis Connection Issues
1. Verify Redis service is running
2. Check Redis password in .env
3. Test Redis connection:
```bash
redis-cli ping
```

### Docker Issues
1. **Services won't start**
   - Check docker logs
   - Verify port availability
   - Ensure no conflicts with local services

2. **Database migrations fail**
   - Wait for database to be ready
   - Check database connection settings
   - Verify migration files exist

## Production Deployment

1. **Update Environment Variables**
```bash
# Set production values
ENVIRONMENT=production
DEBUG=False
```

2. **Security Checklist**
- [ ] Set strong passwords
- [ ] Configure CORS properly
- [ ] Enable HTTPS
- [ ] Set up proper logging
- [ ] Configure backups
- [ ] Set up monitoring

3. **Performance Tuning**
- Adjust worker count based on CPU cores
- Configure database pool size
- Set appropriate cache TTLs
- Enable compression

4. **Backup Setup**
```bash
# Database backup
docker compose exec db pg_dump -U postgres ecommerce > backup.sql

# Restore if needed
docker compose exec db psql -U postgres ecommerce < backup.sql
```

## Additional Resources

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [SQLAlchemy Documentation](https://docs.sqlalchemy.org/)
- [Alembic Documentation](https://alembic.sqlalchemy.org/)
- [Docker Documentation](https://docs.docker.com/)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [Redis Documentation](https://redis.io/documentation)

## Support

For technical issues:
- Open an issue in the repository
- Check the logs: `docker compose logs -f service_name`