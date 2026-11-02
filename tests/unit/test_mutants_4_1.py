from starlette.requests import Request

from app.core.errors import ProblemType, _unsupported_media_type
from app.core.rate_limit import user_id_from_token
from app.core.security import create_access_token


def test_user_id_from_token_multiple_spaces():
    """Kills:
    app.core.rate_limit.x_user_id_from_token__mutmut_3
    """
    token = create_access_token(42)
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"authorization", f"Bearer  {token}".encode("latin-1"))],
    })
    assert user_id_from_token(request) == "42"


def test_unsupported_media_type_detail_message():
    """Kills:
    app.core.errors.x__unsupported_media_type__mutmut_19
    app.core.errors.x__unsupported_media_type__mutmut_20
    app.core.errors.x__unsupported_media_type__mutmut_21
    """
    request = Request({"type": "http", "method": "POST", "headers": []})
    prob = _unsupported_media_type(request)
    assert prob.status_code == 415
    assert prob.problem_type == ProblemType.UNSUPPORTED_MEDIA_TYPE
    expected_detail = "The request body must be JSON: send it with Content-Type: application/json"
    assert prob.detail == expected_detail
