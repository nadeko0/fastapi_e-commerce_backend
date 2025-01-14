# Setup Instructions

This document provides detailed instructions for setting up the E-commerce Backend project in both local and Docker environments.

## Prerequisites

### Local Development
- Python 3.11 or higher
- PostgreSQL 15
- Redis 7
- Git

### Docker Development
- Docker Engine 24.0+
- Docker Compose v2.20+
- Git

## Local Setup

1. **Clone the Repository**
```bash
git clone <repository-url>
cd fastapi-ecommerce
```

2. **Create Virtual Environment**
```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Linux/Mac
python -m venv venv
source venv/bin/activate
```

3. **Install Dependencies**
```bash
pip install -r requirements.txt
```

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
alembic upgrade head
```

7. **Start Redis Server**
```bash
# Windows (if using WSL)
wsl sudo service redis-server start

# Linux/Mac
sudo service redis-server start
```

8. **Run the Application**
```bash
uvicorn app.main:app --reload
```

## Docker Setup

1. **Clone the Repository**
```bash
git clone <repository-url>
cd fastapi-ecommerce
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
docker-compose up --build -d

# View logs
docker-compose logs -f

# Check service status
docker-compose ps
```

4. **Run Migrations**
```bash
docker-compose exec api alembic upgrade head
```

5. **Create Initial Admin User**
```bash
docker-compose exec api python -m scripts.create_admin
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
pytest

# Docker
docker-compose exec api pytest
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

## Monitoring

### Prometheus & Grafana (Optional)
1. Enable monitoring services:
```bash
docker-compose --profile monitoring up -d
```

2. Access monitoring:
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (admin/admin)

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
docker-compose exec db pg_dump -U postgres ecommerce > backup.sql

# Restore if needed
docker-compose exec db psql -U postgres ecommerce < backup.sql
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
- Create an issue in the repository
- Contact technical support: {TECHNICAL_CONTACT}
- Check the logs: `docker-compose logs -f service_name`