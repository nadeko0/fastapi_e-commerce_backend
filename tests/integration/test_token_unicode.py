"""A string that is not valid Unicode is an invalid token (4.0.1).

PyJWT encodes the token it is given to UTF-8 before it reads it, and a lone
surrogate (U+D800..U+DFFF) cannot be encoded: UnicodeEncodeError, a 500 on
POST /users/refresh. Every decode in app/core/security.py now goes through
decode_token, which turns that into the jwt.DecodeError each caller already
treats as "invalid token". (The request-body rule of app/core/text_input.py
refuses such a string earlier, as a 422; this is the second line, for a token
that arrives some other way.)
"""
import asyncio

import jwt
import pytest
from fastapi import HTTPException

from app.core import security

API = "/api/v1"
UNUSABLE_TOKENS = ["a\ud800b", "\udfff", "x.\ud800.y", "aéb.c.d"]


def test_authorization_header_that_is_not_ascii_is_401(client):
    # A header arrives as latin-1 text; the token is never a JWT.
    response = client.get(
        f"{API}/users/me", headers={"Authorization": "Bearer café".encode("latin-1")},
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'


@pytest.mark.parametrize("token", UNUSABLE_TOKENS)
def test_decoding_a_token_that_is_not_valid_unicode_is_an_invalid_token(token):
    assert security.verify_email_token(token) is None
    assert security.verify_password_reset_token(token) is None
    with pytest.raises(jwt.InvalidTokenError):
        security.decode_token(token, options={"require": ["exp"]})


@pytest.mark.parametrize("token", UNUSABLE_TOKENS)
def test_the_functions_that_read_a_token_treat_it_as_invalid(token):
    # None of these gets as far as the database (db=None): the decode fails first.
    with pytest.raises(HTTPException) as access:
        asyncio.run(security.get_current_user(None, token))
    assert access.value.status_code == 401
    assert access.value.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
    with pytest.raises(security.RefreshTokenError, match="Could not validate refresh token"):
        asyncio.run(security.rotate_refresh_token(token, None))
    # Logout and the blacklist treat a token that does not verify as nothing to revoke.
    assert asyncio.run(security.revoke_refresh_token(token)) is True
    assert asyncio.run(security.blacklist_token(token, 60)) is False
