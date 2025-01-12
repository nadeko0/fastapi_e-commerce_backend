from typing import Callable
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.core.config import settings

# Rate limiter
limiter = Limiter(key_func=get_remote_address)

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)
        self.security_headers = {
            'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
            'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; img-src 'self' data: https:;",
            'Permissions-Policy': 'geolocation=(), microphone=()',
            'X-XSS-Protection': '1; mode=block',
            'Referrer-Policy': 'strict-origin-when-cross-origin',
        }

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        for header_name, header_value in self.security_headers.items():
            response.headers[header_name] = header_value
        return response

class CSRFMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)
        self.safe_methods = {'GET', 'HEAD', 'OPTIONS'}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.method not in self.safe_methods:
            csrf_cookie = request.cookies.get('csrf_token')
            csrf_header = request.headers.get('X-CSRF-Token')
            
            if not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
                return Response(
                    status_code=403,
                    content={"detail": "CSRF token missing or invalid"}
                )

        response = await call_next(request)

        if request.method == 'GET' and 'csrf_token' not in request.cookies:
            response.set_cookie(
                key='csrf_token',
                value=request.state.csrf_token,
                httponly=True,
                secure=True,
                samesite='Strict'
            )

        return response

def setup_middleware(app: FastAPI) -> None:
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CSRFMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin) for origin in settings.BACKEND_CORS_ORIGINS],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        max_age=3600,
    )

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.ALLOWED_HOSTS
    )

    app.state.limiter = limiter
    app.add_middleware(
        BaseHTTPMiddleware,
        dispatch=limiter.middleware
    )

    @app.middleware("http")
    async def add_secure_cookie_settings(request: Request, call_next):
        response = await call_next(request)
        if 'Set-Cookie' in response.headers:
            response.headers['Set-Cookie'] += '; Secure; SameSite=Strict'
        return response