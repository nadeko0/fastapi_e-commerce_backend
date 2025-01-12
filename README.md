# E-commerce Backend API

A robust and scalable e-commerce backend API built with FastAPI, providing comprehensive features for managing products, orders, users, and more.

## Features

- **User Management**
  - Registration and authentication with JWT
  - Role-based access control (Admin/Client)
  - Email verification
  - Password reset functionality
  - GDPR compliance with consent management
  - User profile management

- **Product Management**
  - Product CRUD operations
  - Category management with hierarchical structure
  - Product search and filtering
  - Stock management
  - Image handling

- **Order Management**
  - Shopping cart functionality
  - Order processing
  - Order status tracking
  - Order history
  - Email notifications

- **Security**
  - JWT authentication
  - Token blacklisting
  - Password hashing
  - CORS protection
  - Rate limiting

- **Data Management**
  - Redis caching
  - Database migrations
  - GDPR data export
  - Data retention policies

## Technical Stack

- **Framework**: FastAPI
- **Database**: PostgreSQL
- **ORM**: SQLAlchemy
- **Caching**: Redis
- **Task Queue**: Celery (for background tasks)
- **Authentication**: JWT with refresh tokens
- **Email**: SMTP integration
- **Documentation**: OpenAPI (Swagger)

## Project Structure

```
app/
├── api/
│   ├── v1/
│   │   ├── admin.py       # Admin endpoints
│   │   ├── cart.py        # Shopping cart endpoints
│   │   ├── orders.py      # Order management
│   │   ├── products.py    # Product catalog
│   │   └── users.py       # User management
│   └── deps.py            # Dependencies and utilities
├── core/
│   ├── config.py          # Configuration settings
│   ├── database.py        # Database setup
│   ├── security.py        # Security utilities
│   └── logging_config.py  # Logging configuration
├── models/
│   ├── user.py            # User model
│   ├── product.py         # Product model
│   ├── order.py           # Order model
│   ├── category.py        # Category model
│   └── address.py         # Address model
├── schemas/
│   ├── user.py            # User schemas
│   ├── product.py         # Product schemas
│   ├── order.py           # Order schemas
│   └── common.py          # Common schemas
├── services/
│   ├── email.py           # Email service
│   ├── redis.py           # Redis service
│   └── auth.py            # Authentication service
└── main.py                # Application entry point
```

## Setup

### Prerequisites

- Python 3.8+
- PostgreSQL
- Redis
- SMTP server for emails

### Environment Variables

Create a `.env` file in the root directory:

```env
# Application Settings
PROJECT_NAME=E-commerce Backend
VERSION=1.0.0
ENVIRONMENT=development
DEBUG=True

# Server Configuration
HOST=0.0.0.0
PORT=8000
WORKERS=4

# Database Configuration
POSTGRES_SERVER=localhost
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password
POSTGRES_DB=ecommerce

# Redis Configuration
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=
REDIS_DB=0

# JWT Configuration
JWT_SECRET_KEY=your_secret_key
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=7

# Email Settings
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your_email@gmail.com
SMTP_PASSWORD=your_app_password
EMAILS_FROM_EMAIL=noreply@example.com
EMAILS_FROM_NAME=E-commerce Support

# CORS Configuration
BACKEND_CORS_ORIGINS=["http://localhost:3000"]
```

### Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd backend
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

4. Run database migrations:
```bash
alembic upgrade head
```

5. Start the application:
```bash
uvicorn app.main:app --reload
```

## API Documentation

Once the application is running, access the API documentation at:
- Swagger UI: `http://localhost:8000/api/v1/docs`
- ReDoc: `http://localhost:8000/api/v1/redoc`


### Database Migrations

To create a new migration:
```bash
alembic revision --autogenerate -m "description"
```

To apply migrations:
```bash
alembic upgrade head
```

## Deployment

### Docker Deployment

1. Build the Docker image:
```bash
docker build -t ecommerce-backend .
```

2. Run with Docker Compose:
```bash
docker-compose up -d
```

### Production Considerations

- Set `ENVIRONMENT=production` in .env
- Configure proper logging
- Set up monitoring
- Configure proper CORS origins
- Use secure SMTP settings
- Set up proper database backups
- Configure rate limiting
- Set up proper caching strategies

## Security Features

- Password hashing using bcrypt
- JWT token authentication
- Token blacklisting for logout
- Rate limiting on sensitive endpoints
- CORS protection
- Security headers middleware
- SQL injection protection through SQLAlchemy
- XSS protection
- CSRF protection

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request
