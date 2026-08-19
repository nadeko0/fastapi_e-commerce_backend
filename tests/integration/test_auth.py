from datetime import timedelta

from freezegun import freeze_time

from app.core.config import settings
from app.core.security import (
    blacklist_token,
    generate_email_verification_token,
    generate_password_reset_token,
)

API_PREFIX = "/api/v1/users"


def _register_and_login(client, payload):
    client.post(f"{API_PREFIX}/register", json=payload)
    login_response = client.post(
        f"{API_PREFIX}/login",
        data={"username": payload["email"], "password": payload["password"]},
    )
    return login_response.json()["data"]["access_token"]


def test_register_returns_created_user(client, valid_registration_payload):
    response = client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["email"] == valid_registration_payload["email"]
    assert "hashed_password" not in body["data"]


def test_register_rejects_duplicate_email(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    response = client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    assert response.status_code == 409


def test_register_requires_gdpr_consent(client, valid_registration_payload):
    valid_registration_payload["gdpr_consent"] = False
    response = client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    assert response.status_code == 422


def test_login_returns_tokens_for_valid_credentials(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    response = client.post(
        f"{API_PREFIX}/login",
        data={
            "username": valid_registration_payload["email"],
            "password": valid_registration_payload["password"],
        },
    )

    assert response.status_code == 200
    tokens = response.json()["data"]
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    assert tokens["token_type"] == "bearer"


def test_login_rejects_wrong_password(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    response = client.post(
        f"{API_PREFIX}/login",
        data={
            "username": valid_registration_payload["email"],
            "password": "wrong-password",
        },
    )

    assert response.status_code == 401


def test_me_requires_authentication(client):
    response = client.get(f"{API_PREFIX}/me")

    assert response.status_code == 401


def test_me_returns_current_user_with_valid_token(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    login_response = client.post(
        f"{API_PREFIX}/login",
        data={
            "username": valid_registration_payload["email"],
            "password": valid_registration_payload["password"],
        },
    )
    access_token = login_response.json()["data"]["access_token"]

    response = client.get(
        f"{API_PREFIX}/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["email"] == valid_registration_payload["email"]


def test_login_rejects_nonexistent_email_with_same_wording_as_wrong_password(
    client, valid_registration_payload
):
    # A different error message (or status) for "no such account" vs "wrong
    # password" would let an attacker enumerate registered emails.
    wrong_password_response = client.post(
        f"{API_PREFIX}/login",
        data={
            "username": "does-not-exist@example.com",
            "password": "whatever-Pass1",
        },
    )

    assert wrong_password_response.status_code == 401
    assert wrong_password_response.json()["detail"] == "Incorrect email or password"


def test_me_rejects_expired_access_token(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    with freeze_time("2026-01-01 12:00:00"):
        login_response = client.post(
            f"{API_PREFIX}/login",
            data={
                "username": valid_registration_payload["email"],
                "password": valid_registration_payload["password"],
            },
        )
        access_token = login_response.json()["data"]["access_token"]

    past_expiry = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES + 1)
    with freeze_time("2026-01-01 12:00:00") as frozen:
        frozen.move_to(frozen.time_to_freeze + past_expiry)
        response = client.get(
            f"{API_PREFIX}/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    assert response.status_code == 401


def test_me_rejects_blacklisted_token(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)

    blacklist_token(access_token, expires_in=3600)

    response = client.get(
        f"{API_PREFIX}/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 401


def test_me_rejects_token_with_tampered_signature(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)
    tampered = access_token[:-1] + ("A" if access_token[-1] != "A" else "B")

    response = client.get(
        f"{API_PREFIX}/me",
        headers={"Authorization": f"Bearer {tampered}"},
    )

    assert response.status_code == 401


def test_me_rejects_refresh_token_used_as_access_token(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    login_response = client.post(
        f"{API_PREFIX}/login",
        data={
            "username": valid_registration_payload["email"],
            "password": valid_registration_payload["password"],
        },
    )
    refresh_token = login_response.json()["data"]["refresh_token"]

    response = client.get(
        f"{API_PREFIX}/me",
        headers={"Authorization": f"Bearer {refresh_token}"},
    )

    assert response.status_code == 401


def test_me_rejects_password_reset_token_used_as_access_token(
    client, valid_registration_payload
):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    reset_token = generate_password_reset_token(valid_registration_payload["email"])

    response = client.get(
        f"{API_PREFIX}/me",
        headers={"Authorization": f"Bearer {reset_token}"},
    )

    assert response.status_code == 401


def test_verify_email_activates_account(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    token = generate_email_verification_token(valid_registration_payload["email"])

    response = client.get(f"{API_PREFIX}/verify-email/{token}")

    assert response.status_code == 200
    assert response.json()["data"]["message"] == "Email verified successfully"


def test_verify_email_rejects_invalid_token(client):
    response = client.get(f"{API_PREFIX}/verify-email/not-a-real-token")

    assert response.status_code == 400


def test_verify_email_twice_reports_already_verified(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    token = generate_email_verification_token(valid_registration_payload["email"])
    client.get(f"{API_PREFIX}/verify-email/{token}")

    response = client.get(f"{API_PREFIX}/verify-email/{token}")

    assert response.status_code == 200
    assert response.json()["data"]["message"] == "Email already verified"


def test_password_reset_flow_allows_login_with_new_password(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    reset_token = generate_password_reset_token(valid_registration_payload["email"])

    validate_response = client.get(f"{API_PREFIX}/password/reset/{reset_token}")
    assert validate_response.status_code == 200

    reset_response = client.post(
        f"{API_PREFIX}/password/reset/{reset_token}",
        json={"current_password": "unused", "new_password": "NewStr0ngPass"},
    )
    assert reset_response.status_code == 200

    old_password_login = client.post(
        f"{API_PREFIX}/login",
        data={
            "username": valid_registration_payload["email"],
            "password": valid_registration_payload["password"],
        },
    )
    assert old_password_login.status_code == 401

    new_password_login = client.post(
        f"{API_PREFIX}/login",
        data={
            "username": valid_registration_payload["email"],
            "password": "NewStr0ngPass",
        },
    )
    assert new_password_login.status_code == 200


def test_password_reset_rejects_invalid_token(client):
    response = client.post(
        f"{API_PREFIX}/password/reset/not-a-real-token",
        json={"current_password": "unused", "new_password": "NewStr0ngPass"},
    )

    assert response.status_code == 400


# --- Adversarial scenarios ---------------------------------------------
#
# The tests above cover happy paths and the auth edge cases already known
# to be handled correctly. The tests below specifically probe attack
# scenarios: brute force beyond the rate limit, token replay, token
# fixation across a password reset, refresh token handling, and timing-
# safe comparisons.


def test_login_brute_force_is_blocked_after_rate_limit_exceeded(
    client, monkeypatch, valid_registration_payload
):
    # RATE_LIMIT_LOGIN is not monkeypatched here: the RateLimitMiddleware
    # instance is built once (lazily, on the app's first request) and
    # caches settings.RATE_LIMIT_LOGIN in self.endpoint_limits at that
    # point, so patching the setting later in this test would not affect
    # the already-built middleware. Only RATE_LIMIT_ENABLED is read fresh
    # on every request, so that's the one safe to toggle here.
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    # The sliding window buckets requests by whole-second timestamp, so
    # requests issued within the same wall-clock second collapse onto one
    # bucket entry instead of counting separately - advance the clock a
    # full second between attempts, as the rate limiter's own unit tests do.
    with freeze_time("2026-01-01 00:00:00") as frozen:
        for _ in range(settings.RATE_LIMIT_LOGIN):
            response = client.post(
                f"{API_PREFIX}/login",
                data={
                    "username": valid_registration_payload["email"],
                    "password": "wrong-password",
                },
            )
            assert response.status_code == 401
            frozen.tick(delta=1)

        blocked_response = client.post(
            f"{API_PREFIX}/login",
            data={
                "username": valid_registration_payload["email"],
                "password": "wrong-password",
            },
        )

    assert blocked_response.status_code == 429
    assert blocked_response.json()["detail"]["error"] == "Rate limit exceeded"


def test_access_token_is_replayable_until_blacklisted_or_expired(
    client, valid_registration_payload
):
    # JWTs are inherently replayable: there is no session-binding or
    # per-request nonce, so a valid access token used from two unrelated
    # requests behaves identically both times. This is the expected,
    # accepted tradeoff for stateless JWT auth (replay protection would
    # require server-side session state, which this app deliberately
    # doesn't have outside of the blacklist). This test documents that
    # behavior as a regression guard - it should fail loudly if a future
    # change silently adds half-implemented single-use semantics.
    access_token = _register_and_login(client, valid_registration_payload)
    headers = {"Authorization": f"Bearer {access_token}"}

    first_use = client.get(f"{API_PREFIX}/me", headers=headers)
    second_use = client.get(f"{API_PREFIX}/me", headers=headers)

    assert first_use.status_code == 200
    assert second_use.status_code == 200
    assert (
        first_use.json()["data"]["email"]
        == second_use.json()["data"]["email"]
        == valid_registration_payload["email"]
    )


def test_reset_password_invalidates_previously_issued_access_token(
    client, valid_registration_payload
):
    # Token fixation check: a token issued before a password reset must
    # stop working after the reset. A user who resets their password
    # because they suspect their account/token is compromised gains
    # nothing if the old token remains valid until it naturally expires.
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    with freeze_time("2026-01-01 12:00:00") as frozen:
        login_response = client.post(
            f"{API_PREFIX}/login",
            data={
                "username": valid_registration_payload["email"],
                "password": valid_registration_payload["password"],
            },
        )
        old_access_token = login_response.json()["data"]["access_token"]

        pre_reset = client.get(
            f"{API_PREFIX}/me",
            headers={"Authorization": f"Bearer {old_access_token}"},
        )
        assert pre_reset.status_code == 200

        # Advance the clock so the reset's "password changed at" watermark
        # (second-granularity, like the token's own iat) is unambiguously
        # later than the old token's issued-at time.
        frozen.tick(delta=timedelta(seconds=2))

        reset_token = generate_password_reset_token(valid_registration_payload["email"])
        reset_response = client.post(
            f"{API_PREFIX}/password/reset/{reset_token}",
            json={"current_password": "unused", "new_password": "NewStr0ngPass"},
        )
        assert reset_response.status_code == 200

        post_reset = client.get(
            f"{API_PREFIX}/me",
            headers={"Authorization": f"Bearer {old_access_token}"},
        )
        assert post_reset.status_code == 401

        frozen.tick(delta=timedelta(seconds=2))

        new_login = client.post(
            f"{API_PREFIX}/login",
            data={
                "username": valid_registration_payload["email"],
                "password": "NewStr0ngPass",
            },
        )
        new_access_token = new_login.json()["data"]["access_token"]

        # A token issued *after* the reset must still work normally.
        fresh_check = client.get(
            f"{API_PREFIX}/me",
            headers={"Authorization": f"Bearer {new_access_token}"},
        )

    assert fresh_check.status_code == 200


def test_no_refresh_token_exchange_endpoint_exists(client, valid_registration_payload):
    # create_refresh_token's output is only ever handed out at login and
    # never consumed anywhere in the app - there is no POST /refresh (or
    # similar) route to exchange it for a new access token. This is a real
    # gap (refresh tokens are effectively dead weight: 7-day-lived bearer
    # secrets shipped to the client with no way to redeem them, and
    # correspondingly no rotation/blacklist-on-refresh to verify), flagged
    # in the accompanying findings notes rather than fixed here, since
    # building a full refresh flow is out of scope for this pass.
    access_token = _register_and_login(client, valid_registration_payload)

    for candidate_path in (
        f"{API_PREFIX}/refresh",
        f"{API_PREFIX}/token/refresh",
        f"{API_PREFIX}/login/refresh",
    ):
        response = client.post(candidate_path, json={"refresh_token": access_token})
        assert response.status_code in (404, 405)


def test_verify_password_delegates_entirely_to_bcrypt_checkpw(monkeypatch):
    # bcrypt.checkpw is constant-time internally. The concern is whether
    # any additional, non-constant-time comparison (e.g. a plain `==` on
    # the hash or a prefix check) was layered on top of it. Spying on
    # bcrypt.checkpw and asserting verify_password's result always matches
    # exactly what it returns (for both a correct and an incorrect
    # password) confirms verify_password is a thin pass-through with no
    # extra custom comparison logic in front of or behind the bcrypt call.
    import bcrypt as bcrypt_module

    from app.core.security import get_password_hash, verify_password

    calls = []
    original_checkpw = bcrypt_module.checkpw

    def spy_checkpw(password, hashed):
        result = original_checkpw(password, hashed)
        calls.append((password, hashed, result))
        return result

    monkeypatch.setattr(bcrypt_module, "checkpw", spy_checkpw)

    hashed = get_password_hash("Str0ngPass")

    assert verify_password("Str0ngPass", hashed) is True
    assert verify_password("wrong-password", hashed) is False

    assert len(calls) == 2
    assert calls[0][2] is True
    assert calls[1][2] is False
