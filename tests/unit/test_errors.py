from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from app.core.errors import (
    ProblemType,
    _supported_methods,
    _unsupported_media_type,
    http_exception_handler,
    register_error_handlers,
    request_validation_error_handler,
)
from app.main import app


def test_register_error_handlers_registers_http_exception():
    test_app = FastAPI()
    register_error_handlers(test_app)
    assert test_app.exception_handlers.get(StarletteHTTPException) == http_exception_handler


def test_register_error_handlers_registers_validation_exception():
    test_app = FastAPI()
    register_error_handlers(test_app)
    assert test_app.exception_handlers.get(RequestValidationError) == request_validation_error_handler


def test_supported_methods_with_root_path():
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/mounted/api/v1/products",
        "root_path": "/mounted",
        "app": app,
        "headers": [],
    })
    methods = _supported_methods(request)
    assert "GET" in methods


def test_unsupported_media_type_patch():
    req = Request({"type": "http", "method": "PATCH", "headers": []})
    prob = _unsupported_media_type(req)
    assert prob.status_code == 415
    assert prob.problem_type == ProblemType.UNSUPPORTED_MEDIA_TYPE
    assert prob.headers["Accept"] == "application/merge-patch+json, application/json"


def test_unsupported_media_type_other_method():
    req = Request({"type": "http", "method": "POST", "headers": []})
    prob = _unsupported_media_type(req)
    assert prob.status_code == 415
    assert prob.problem_type == ProblemType.UNSUPPORTED_MEDIA_TYPE
    assert prob.headers["Accept"] == "application/json"
