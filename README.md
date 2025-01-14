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

- **Framework**: FastAPI 0.115.6
- **Database**: PostgreSQL with SQLAlchemy 2.0.37
- **Caching**: Redis 5.2.1
- **Task Queue**: Celery 5.4.0
- **Authentication**: JWT with refresh tokens
- **Email**: SMTP integration
- **Documentation**: OpenAPI (Swagger)

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

## Docker Setup

### Prerequisites
- Docker
- Docker Compose

### Docker Configuration Files

1. Create `Dockerfile`:
```dockerfile
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

2. Create `docker-compose.yml`:
```yaml
version: '3.8'

services:
  api:
    build: .
    ports:
      - "8000:8000"
    env_file:
      - .env
    depends_on:
      - db
      - redis

  db:
    image: postgres:15
    volumes:
      - postgres_data:/var/lib/postgresql/data
    env_file:
      - .env
    ports:
      - "5432:5432"

  redis:
    image: redis:7
    ports:
      - "6379:6379"
    command: redis-server --requirepass ${REDIS_PASSWORD}
    volumes:
      - redis_data:/data

volumes:
  postgres_data:
  redis_data:
```

### Running with Docker

1. Build and start services:
```bash
docker-compose up --build
```

2. Run migrations:
```bash
docker-compose exec api alembic upgrade head
```

3. Create initial admin user:
```bash
docker-compose exec api python -m scripts.create_admin
```

## Local Development Setup

1. Clone the repository:
```bash
git clone <repository-url>
cd fastapi-ecommerce
```

2. Create and activate virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Set up environment variables:
```bash
cp .env.example .env
# Edit .env with your configurations
```

5. Run database migrations:
```bash
alembic upgrade head
```

6. Start the application:
```bash
uvicorn app.main:app --reload
```

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