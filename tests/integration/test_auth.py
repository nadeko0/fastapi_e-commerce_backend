import asyncio
import threading
import time
from datetime import timedelta

import jwt
import pytest
from conftest import TestingSessionLocal
from freezegun import freeze_time
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import settings
from app.core.security import (
    blacklist_token,
    create_access_token,
    generate_email_verification_token,
    generate_password_reset_token,
    validate_token,
)

API_PREFIX = "/api/v1/users"


def _reset_token_for(email):
    """A reset token as the reset email would carry it: issued against the
    account's current password hash."""
    from app.models.user import User

    db = TestingSessionLocal()
    try:
        user = db.query(User).filter(User.email == email).one()
        return generate_password_reset_token(user.email, user.hashed_password)
    finally:
        db.close()


def _register_and_login(client, payload):
    client.post(f"{API_PREFIX}/register", json=payload)
    login_response = client.post(
        f"{API_PREFIX}/login",
        data={"username": payload["email"], "password": payload["password"]},
    )
    return login_response.json()["data"]["access_token"]



def _tamper_signature(token: str) -> str:
    # Change the FIRST character of the signature, not the last. A JWT
    # signature is 32 bytes in 43 base64url characters, so the final character
    # carries only 4 significant bits: swapping it for another character can
    # decode to the very same bytes, leaving a perfectly valid token and a
    # test that fails now and then for no visible reason.
    head, signature = token.rsplit(".", 1)
    return f"{head}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"

def _registration_mail(monkeypatch):
    """What registration queues: welcome mails and "you already have an
    account" mails, by recipient."""
    sent = {"welcome": [], "exists": []}
    monkeypatch.setattr(
        "app.api.v1.users.send_welcome_email",
        lambda to_email, *args: sent["welcome"].append(to_email),
    )
    monkeypatch.setattr(
        "app.api.v1.users.send_account_exists_email",
        lambda to_email, *args: sent["exists"].append(to_email),
        raising=False,
    )
    return sent


def test_register_creates_the_account(client, valid_registration_payload, db_session):
    from app.models.user import User as UserModel

    response = client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    assert response.status_code == 200
    assert "hashed_password" not in response.text
    user = db_session.query(UserModel).filter(
        UserModel.email == valid_registration_payload["email"]
    ).one()
    assert user.full_name == valid_registration_payload["full_name"]


def test_registering_an_existing_email_answers_exactly_like_a_new_one(
    client, monkeypatch, valid_registration_payload
):
    # A 409 "Email already registered" told anybody which addresses have an
    # account here, while login and password reset take care not to. Now
    # the answer is the same, and only the mailbox's owner learns the
    # difference: from the email they get.
    sent = _registration_mail(monkeypatch)
    email = valid_registration_payload["email"]

    new = client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    again = client.post(
        f"{API_PREFIX}/register",
        json={**valid_registration_payload, "password": "Different1", "full_name": "Someone Else"},
    )

    assert new.status_code == again.status_code == 200
    assert new.json()["data"] == again.json()["data"]
    assert email not in new.text
    assert sent == {"welcome": [email], "exists": [email]}
    # The existing account is untouched: its password is still the first one.
    assert _can_log_in(client, email, valid_registration_payload["password"])
    assert not _can_log_in(client, email, "Different1")


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


def test_login_runs_bcrypt_even_for_nonexistent_email(client, monkeypatch):
    # Regression test: the login route used to be written as
    # `if user_id is None or not await verify_password_async(...)`. Python's
    # `or` short-circuits, so for a nonexistent email (user_id is None) the
    # right-hand side was never evaluated at all - bcrypt never ran. That
    # made a "no such account" login return near-instantly while a "wrong
    # password for a real account" login took the full bcrypt cost (~300ms
    # in production), a timing side-channel that reveals whether an email is
    # registered - the exact same information the identical-response-message
    # test above exists to hide. Fixed by always computing the password
    # check first (against a dummy hash when the user doesn't exist) and
    # only then combining it with the existence check.
    calls = []
    import app.core.security as security_module
    original = security_module.verify_password_async

    async def spy(*args, **kwargs):
        calls.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr("app.api.v1.users.verify_password_async", spy)

    response = client.post(
        f"{API_PREFIX}/login",
        data={"username": "does-not-exist@example.com", "password": "whatever-Pass1"},
    )

    assert response.status_code == 401
    assert len(calls) == 1, "verify_password_async must run even when the account doesn't exist"


def test_login_refuses_a_deactivated_account_like_a_wrong_password(
    client, monkeypatch, valid_registration_payload, db_session
):
    # A deactivated account (erasure requested, or switched off by an
    # admin) must not get tokens, and the refusal must not tell a caller
    # with the right password that the account exists but is switched off.
    from app.models.user import User as UserModel

    email = valid_registration_payload["email"]
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    user = db_session.query(UserModel).filter(UserModel.email == email).one()
    user.is_active = False
    db_session.commit()

    calls = []
    import app.core.security as security_module
    original = security_module.verify_password_async

    async def spy(*args, **kwargs):
        calls.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr("app.api.v1.users.verify_password_async", spy)

    right_password = client.post(
        f"{API_PREFIX}/login",
        data={"username": email, "password": valid_registration_payload["password"]},
    )
    wrong_password = client.post(
        f"{API_PREFIX}/login", data={"username": email, "password": "wrong-Pass1"}
    )

    assert right_password.status_code == 401
    assert right_password.json() == wrong_password.json() == {
        "detail": "Incorrect email or password"
    }
    # bcrypt ran both times: the answer takes as long as any other refusal.
    assert len(calls) == 2


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
    tampered = _tamper_signature(access_token)

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
    reset_token = _reset_token_for(valid_registration_payload["email"])

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
    reset_token = _reset_token_for(valid_registration_payload["email"])

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


def _emailed_reset_token(client, monkeypatch, email):
    """The token the reset email would carry, taken from the real request
    path (POST /password/reset) rather than minted by the test."""
    sent = []
    monkeypatch.setattr(
        "app.api.v1.users.send_password_reset_email",
        lambda to_email, token: sent.append(token),
    )
    response = client.post(f"{API_PREFIX}/password/reset", json={"email": email})
    assert response.status_code == 200
    assert len(sent) == 1, "no reset email was queued"
    return sent[0]


def _reset(client, token, new_password):
    return client.post(
        f"{API_PREFIX}/password/reset/{token}",
        json={"current_password": "unused", "new_password": new_password},
    )


def _can_log_in(client, email, password):
    response = client.post(
        f"{API_PREFIX}/login", data={"username": email, "password": password}
    )
    return response.status_code == 200


def test_a_used_password_reset_token_cannot_be_used_again(
    client, monkeypatch, valid_registration_payload
):
    # The link lives for a day, travels in a URL (proxy logs, browser
    # history, Referer) and used to keep working after it had done its job:
    # whoever saw it could set the password again, as often as they liked.
    email = valid_registration_payload["email"]
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    token = _emailed_reset_token(client, monkeypatch, email)

    assert _reset(client, token, "NewStr0ngPass").status_code == 200

    assert client.get(f"{API_PREFIX}/password/reset/{token}").status_code == 400
    replay = _reset(client, token, "Hijacked123")
    assert replay.status_code == 400
    assert replay.json()["detail"] == "Invalid or expired reset token"
    assert _can_log_in(client, email, "NewStr0ngPass")
    assert not _can_log_in(client, email, "Hijacked123")


def test_a_reset_token_issued_before_the_last_password_change_is_refused(
    client, monkeypatch, valid_registration_payload
):
    # Two reset emails, the second one used: the first must be dead too, or
    # an old link lying in a mailbox still opens the account.
    email = valid_registration_payload["email"]
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    older = _emailed_reset_token(client, monkeypatch, email)
    newer = _emailed_reset_token(client, monkeypatch, email)

    assert _reset(client, newer, "NewStr0ngPass").status_code == 200

    assert client.get(f"{API_PREFIX}/password/reset/{older}").status_code == 400
    assert _reset(client, older, "Hijacked123").status_code == 400
    assert _can_log_in(client, email, "NewStr0ngPass")


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
    # A scripted brute force against one account, with the default
    # settings. What stops it is the failed-login count of that email at
    # that address (RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS, 10 in
    # 15 minutes), which is lower than
    # what the address may try on the login route (RATE_LIMIT_AUTH,
    # 20/minute). Details of both: tests/unit/test_rate_limit.py and
    # tests/integration/test_rate_limit_api.py.
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    def attempt(password="wrong-password"):
        return client.post(
            f"{API_PREFIX}/login",
            data={"username": valid_registration_payload["email"], "password": password},
        )

    # The clock is frozen and only advanced on purpose: every attempt of
    # the burst lands in the same instant, which is what a scripted brute
    # force looks like. (The first limiter stored one entry per whole
    # second, so a burst like this counted as a single request.)
    with freeze_time("2026-01-01 00:00:00") as clock:
        for _ in range(10):
            assert attempt().status_code == 401

        blocked_response = attempt()
        assert blocked_response.status_code == 429
        detail = blocked_response.json()["detail"]
        assert detail["error"] == "Too many failed login attempts"
        assert detail["retry_after"] == 15 * 60
        assert blocked_response.headers["Retry-After"] == "900"

        # The right password does not get through during the block either.
        assert attempt(valid_registration_payload["password"]).status_code == 429

        # When the window is over the account can be logged into again.
        clock.tick(delta=15 * 60 + 1)
        assert attempt(valid_registration_payload["password"]).status_code == 200


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

        reset_token = _reset_token_for(valid_registration_payload["email"])
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


def _login_tokens(client, payload):
    client.post(f"{API_PREFIX}/register", json=payload)
    login_response = client.post(
        f"{API_PREFIX}/login",
        data={"username": payload["email"], "password": payload["password"]},
    )
    return login_response.json()["data"]


def test_refresh_returns_new_access_and_refresh_tokens_and_rotates_old_one(
    client, valid_registration_payload
):
    tokens = _login_tokens(client, valid_registration_payload)
    old_refresh_token = tokens["refresh_token"]

    response = client.post(
        f"{API_PREFIX}/refresh", json={"refresh_token": old_refresh_token}
    )

    assert response.status_code == 200
    new_tokens = response.json()["data"]
    assert new_tokens["access_token"]
    assert new_tokens["refresh_token"]
    assert new_tokens["refresh_token"] != old_refresh_token
    # Note: the new access token can be byte-identical to the old one if
    # both were minted within the same second - access tokens carry no jti,
    # only second-granularity iat/exp, so that's expected and not a bug.

    # New access token works.
    me_response = client.get(
        f"{API_PREFIX}/me",
        headers={"Authorization": f"Bearer {new_tokens['access_token']}"},
    )
    assert me_response.status_code == 200

    # Old refresh token is now dead (rotation makes it single-use).
    replay_response = client.post(
        f"{API_PREFIX}/refresh", json={"refresh_token": old_refresh_token}
    )
    assert replay_response.status_code == 401


def test_refresh_does_not_require_a_valid_access_token(client, valid_registration_payload):
    # Refreshing is the whole mechanism by which a client recovers from an
    # expired access token, so an expired (or entirely absent) access token
    # must not block it.
    with freeze_time("2026-01-01 12:00:00") as frozen:
        tokens = _login_tokens(client, valid_registration_payload)
        refresh_token = tokens["refresh_token"]

        past_expiry = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES + 1)
        frozen.tick(delta=past_expiry)

        expired_access_check = client.get(
            f"{API_PREFIX}/me",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert expired_access_check.status_code == 401

        response = client.post(
            f"{API_PREFIX}/refresh", json={"refresh_token": refresh_token}
        )

    assert response.status_code == 200
    assert response.json()["data"]["access_token"]


def test_refresh_rejects_malformed_token(client):
    response = client.post(
        f"{API_PREFIX}/refresh", json={"refresh_token": "not-a-real-token"}
    )
    assert response.status_code == 401


def test_refresh_rejects_token_with_tampered_signature(client, valid_registration_payload):
    tokens = _login_tokens(client, valid_registration_payload)
    refresh_token = tokens["refresh_token"]
    tampered = _tamper_signature(refresh_token)

    response = client.post(f"{API_PREFIX}/refresh", json={"refresh_token": tampered})
    assert response.status_code == 401


@pytest.mark.parametrize("subject", ["abc", "1.0", "9" * 5000])
def test_refresh_with_a_signed_token_whose_subject_is_not_a_user_id_is_a_401(client, subject):
    # Registered like any refresh token, so that only the subject is wrong.
    # The user lookup did int(sub): a ValueError, a 500.
    from app.core.security import issue_refresh_token

    response = client.post(
        f"{API_PREFIX}/refresh", json={"refresh_token": issue_refresh_token(subject)}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate refresh token"


def test_refresh_rejects_missing_body_field(client):
    response = client.post(f"{API_PREFIX}/refresh", json={})
    assert response.status_code == 422


def test_refresh_rejects_access_token_presented_as_refresh_token(
    client, valid_registration_payload
):
    tokens = _login_tokens(client, valid_registration_payload)

    response = client.post(
        f"{API_PREFIX}/refresh", json={"refresh_token": tokens["access_token"]}
    )
    assert response.status_code == 401


def test_refresh_rejects_expired_refresh_token(client, valid_registration_payload):
    with freeze_time("2026-01-01 12:00:00") as frozen:
        tokens = _login_tokens(client, valid_registration_payload)
        refresh_token = tokens["refresh_token"]

        past_expiry = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS + 1)
        frozen.tick(delta=past_expiry)

        response = client.post(
            f"{API_PREFIX}/refresh", json={"refresh_token": refresh_token}
        )

    assert response.status_code == 401


def test_refresh_rejects_reused_token_and_revokes_the_rest_of_its_family(
    client, valid_registration_payload
):
    tokens = _login_tokens(client, valid_registration_payload)
    original_refresh_token = tokens["refresh_token"]

    first_use = client.post(
        f"{API_PREFIX}/refresh", json={"refresh_token": original_refresh_token}
    )
    assert first_use.status_code == 200
    rotated_refresh_token = first_use.json()["data"]["refresh_token"]

    # Replaying the already-rotated token is a stolen-token signal.
    replay = client.post(
        f"{API_PREFIX}/refresh", json={"refresh_token": original_refresh_token}
    )
    assert replay.status_code == 401

    # The rest of the family (the token the replay attempt "raced" against)
    # must be revoked too, not just the reused one.
    downstream = client.post(
        f"{API_PREFIX}/refresh", json={"refresh_token": rotated_refresh_token}
    )
    assert downstream.status_code == 401


def test_logout_invalidates_refresh_token(client, valid_registration_payload):
    tokens = _login_tokens(client, valid_registration_payload)

    logout_response = client.post(
        f"{API_PREFIX}/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert logout_response.status_code == 200

    refresh_response = client.post(
        f"{API_PREFIX}/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refresh_response.status_code == 401


def _login_again(client, payload):
    response = client.post(
        f"{API_PREFIX}/login",
        data={"username": payload["email"], "password": payload["password"]},
    )
    return response.json()["data"]


def _refresh(client, refresh_token):
    return client.post(f"{API_PREFIX}/refresh", json={"refresh_token": refresh_token})


def test_logout_without_a_refresh_token_signs_out_everywhere(
    client, valid_registration_payload
):
    # The refresh token was optional, and leaving it out revoked nothing
    # but the access token: the refresh token went on minting new access
    # tokens for REFRESH_TOKEN_EXPIRE_DAYS. Without one, logout cannot tell
    # which session is meant, so it ends all of them.
    phone = _login_tokens(client, valid_registration_payload)
    laptop = _login_again(client, valid_registration_payload)
    rotated = _refresh(client, laptop["refresh_token"]).json()["data"]["refresh_token"]

    response = client.post(
        f"{API_PREFIX}/logout", headers={"Authorization": f"Bearer {phone['access_token']}"}
    )

    assert response.status_code == 200
    assert _refresh(client, phone["refresh_token"]).status_code == 401
    assert _refresh(client, rotated).status_code == 401


def test_logout_with_a_refresh_token_ends_only_that_session(
    client, valid_registration_payload
):
    phone = _login_tokens(client, valid_registration_payload)
    laptop = _login_again(client, valid_registration_payload)

    client.post(
        f"{API_PREFIX}/logout",
        json={"refresh_token": phone["refresh_token"]},
        headers={"Authorization": f"Bearer {phone['access_token']}"},
    )

    assert _refresh(client, phone["refresh_token"]).status_code == 401
    assert _refresh(client, laptop["refresh_token"]).status_code == 200


def test_signing_out_everywhere_leaves_other_users_signed_in(
    client, valid_registration_payload
):
    mine = _login_tokens(client, valid_registration_payload)
    theirs = _login_tokens(
        client, {**valid_registration_payload, "email": "someone.else@example.com"}
    )

    client.post(
        f"{API_PREFIX}/logout", headers={"Authorization": f"Bearer {mine['access_token']}"}
    )

    assert _refresh(client, theirs["refresh_token"]).status_code == 200


def test_logout_blacklists_the_access_token_too(client, valid_registration_payload):
    tokens = _login_tokens(client, valid_registration_payload)

    client.post(
        f"{API_PREFIX}/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )

    me_response = client.get(
        f"{API_PREFIX}/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert me_response.status_code == 401


def _pad_signature(token: str) -> str:
    # PyJWT accepts the signature segment with its base64url padding: a
    # different string for the very same token. (It rejects every other
    # re-spelling, e.g. a changed spare bit in the last character.)
    return f"{token}="


def test_logout_revokes_the_access_token_with_padded_signature_too(
    client, valid_registration_payload
):
    # A blacklist keyed by the raw token string missed this: after logout a
    # stolen token kept working with "=" appended.
    access_token = _register_and_login(client, valid_registration_payload)
    variant = _pad_signature(access_token)
    # The variant really is accepted as the same token, or this test proves nothing.
    assert client.get(
        f"{API_PREFIX}/me", headers={"Authorization": f"Bearer {variant}"}
    ).status_code == 200

    logout_response = client.post(
        f"{API_PREFIX}/logout", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert logout_response.status_code == 200

    plain_after = client.get(
        f"{API_PREFIX}/me", headers={"Authorization": f"Bearer {access_token}"}
    )
    padded_after = client.get(
        f"{API_PREFIX}/me", headers={"Authorization": f"Bearer {variant}"}
    )
    assert (plain_after.status_code, padded_after.status_code) == (401, 401)


def test_logout_revokes_an_access_token_without_jti_in_every_spelling(
    client, valid_registration_payload
):
    # Access tokens issued before tokens carried a jti are still valid for
    # up to ACCESS_TOKEN_EXPIRE_MINUTES after a deploy, and logout must
    # revoke those, in every spelling, too.
    access_token = _register_and_login(client, valid_registration_payload)
    user_id = client.get(
        f"{API_PREFIX}/me", headers={"Authorization": f"Bearer {access_token}"}
    ).json()["data"]["id"]
    now = int(time.time())
    legacy = jwt.encode(
        {
            "exp": now + 600,
            "iat": now,
            "sub": str(user_id),
            "type": "access",
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    assert client.get(
        f"{API_PREFIX}/me", headers={"Authorization": f"Bearer {legacy}"}
    ).status_code == 200

    # Logging out with one spelling revokes the others as well.
    logout_response = client.post(
        f"{API_PREFIX}/logout",
        headers={"Authorization": f"Bearer {_pad_signature(legacy)}"},
    )
    assert logout_response.status_code == 200

    plain_after = client.get(
        f"{API_PREFIX}/me", headers={"Authorization": f"Bearer {legacy}"}
    )
    padded_after = client.get(
        f"{API_PREFIX}/me",
        headers={"Authorization": f"Bearer {_pad_signature(legacy)}"},
    )
    assert (plain_after.status_code, padded_after.status_code) == (401, 401)


def test_validate_token_rejects_a_blacklisted_token_in_every_spelling():
    token = create_access_token(7)
    assert validate_token(_pad_signature(token)) is not None

    blacklist_token(token, expires_in=600)

    assert validate_token(token) is None
    assert validate_token(_pad_signature(token)) is None


# --- Revocation when Redis fails: closed, every time ---------------------
#
# Logout, a password reset and the checks that honour them all live in
# Redis. When it cannot be asked, the answer must be "revoked" (reading) or
# an error the client sees (writing) - never a silent success that leaves
# the old tokens working.


def _redis_command_fails(monkeypatch, fake_redis, *commands):
    """Make these commands of the shared fake Redis raise, for the sync
    client and the async one alike."""
    import fakeredis

    import app.services.redis_async as redis_async_module

    def broken(*args, **kwargs):
        raise RedisConnectionError("Redis is down")

    for command in commands:
        monkeypatch.setattr(fake_redis, command, broken)

    async def broken_async(*args, **kwargs):
        raise RedisConnectionError("Redis is down")

    class _AsyncCommandsFail(fakeredis.FakeAsyncRedis):
        pass

    for command in commands:
        setattr(_AsyncCommandsFail, command, broken_async)
    server = fakeredis.FakeServer()
    monkeypatch.setattr(
        redis_async_module,
        "_create_client",
        lambda: _AsyncCommandsFail(server=server, decode_responses=True),
    )
    monkeypatch.setattr(redis_async_module, "_clients", {})


def test_refresh_is_refused_when_the_password_change_watermark_cannot_be_read(
    client, monkeypatch, fake_redis, valid_registration_payload
):
    tokens = _login_tokens(client, valid_registration_payload)
    _redis_command_fails(monkeypatch, fake_redis, "get")

    response = client.post(
        f"{API_PREFIX}/refresh", json={"refresh_token": tokens["refresh_token"]}
    )

    assert response.status_code == 401


def test_password_reset_that_cannot_revoke_old_tokens_changes_nothing(
    client, monkeypatch, fake_redis, valid_registration_payload
):
    # "Password has been reset successfully" while every token issued before
    # it kept working was the worst of both: the user believed the account
    # was safe again. Now the reset is refused before the password changes,
    # and the very same link works once Redis is back.
    email = valid_registration_payload["email"]
    old_access = _login_tokens(client, valid_registration_payload)["access_token"]
    token = _reset_token_for(email)
    with monkeypatch.context() as outage:
        _redis_command_fails(outage, fake_redis, "setex")
        response = _reset(client, token, "NewStr0ngPass")

    assert response.status_code == 503
    assert response.json()["detail"] == "Password reset is temporarily unavailable, please retry"
    assert _can_log_in(client, email, valid_registration_payload["password"])
    assert client.get(
        f"{API_PREFIX}/me", headers={"Authorization": f"Bearer {old_access}"}
    ).status_code == 200
    # Nothing was used up: the link still resets the password afterwards.
    assert _reset(client, token, "NewStr0ngPass").status_code == 200


def test_logout_that_cannot_blacklist_the_token_says_so(
    client, monkeypatch, fake_redis, valid_registration_payload
):
    tokens = _login_tokens(client, valid_registration_payload)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    with monkeypatch.context() as outage:
        _redis_command_fails(outage, fake_redis, "setex")
        response = client.post(
            f"{API_PREFIX}/logout", json={"refresh_token": tokens["refresh_token"]}, headers=headers
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Logout is temporarily unavailable, please retry"


def test_logout_that_cannot_revoke_the_refresh_token_says_so(
    client, monkeypatch, fake_redis, valid_registration_payload
):
    tokens = _login_tokens(client, valid_registration_payload)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    with monkeypatch.context() as outage:
        _redis_command_fails(outage, fake_redis, "smembers")
        response = client.post(
            f"{API_PREFIX}/logout", json={"refresh_token": tokens["refresh_token"]}, headers=headers
        )

    assert response.status_code == 503


def test_signing_out_everywhere_that_cannot_revoke_says_so(
    client, monkeypatch, fake_redis, valid_registration_payload
):
    tokens = _login_tokens(client, valid_registration_payload)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    with monkeypatch.context() as outage:
        _redis_command_fails(outage, fake_redis, "smembers")
        response = client.post(f"{API_PREFIX}/logout", headers=headers)

    assert response.status_code == 503


def test_refresh_rejects_token_for_deactivated_user(
    client, valid_registration_payload, db_session
):
    from app.models.user import User as UserModel

    tokens = _login_tokens(client, valid_registration_payload)

    user = (
        db_session.query(UserModel)
        .filter(UserModel.email == valid_registration_payload["email"])
        .first()
    )
    user.is_active = False
    db_session.commit()

    response = client.post(
        f"{API_PREFIX}/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 401


def test_concurrent_refresh_with_same_token_only_one_succeeds(
    client, valid_registration_payload
):
    # Reuse detection (RedisService.consume_refresh_token) does an atomic
    # GET+DELETE inside a Redis transaction, so of N threads racing to
    # redeem the same refresh token, exactly one should see it as
    # "unconsumed" and get new tokens back - the rest must fail cleanly
    # (401), never crash or double-issue tokens.
    tokens = _login_tokens(client, valid_registration_payload)
    refresh_token = tokens["refresh_token"]

    n = 8
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        response = client.post(
            f"{API_PREFIX}/refresh", json={"refresh_token": refresh_token}
        )
        results[i] = response.status_code

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1
    assert results.count(401) == n - 1


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


def test_verify_password_async_offloads_to_threadpool_without_blocking_event_loop(
    monkeypatch,
):
    # verify_password_async is what login/delete-account actually call. If it
    # ran bcrypt.checkpw directly on the event loop thread instead of
    # dispatching to run_in_threadpool's worker threads, N concurrent calls
    # would serialize and take N * per-call-delay. Simulating each bcrypt
    # call as a fixed-cost sleep and running several concurrently via
    # asyncio.gather proves they overlap: total wall time is close to a
    # single call's delay, not the sum of all of them.
    import bcrypt as bcrypt_module

    from app.core.security import verify_password_async

    delay = 0.2
    call_count = 0

    def slow_checkpw(password, hashed):
        nonlocal call_count
        call_count += 1
        time.sleep(delay)
        return password == hashed

    monkeypatch.setattr(bcrypt_module, "checkpw", slow_checkpw)

    async def run_concurrently():
        n = 5
        started = time.monotonic()
        results = await asyncio.gather(
            *[verify_password_async("pw", "pw") for _ in range(n)]
        )
        elapsed = time.monotonic() - started
        return results, elapsed

    results, elapsed = asyncio.run(run_concurrently())

    assert results == [True] * 5
    assert call_count == 5
    # Fully serial would take ~1.0s (5 * 0.2s); concurrent execution across
    # threadpool workers should land close to a single call's delay. Use a
    # generous cutoff (well under the serial time) to avoid flakiness.
    assert elapsed < delay * 3, (
        f"expected concurrent bcrypt calls to overlap, took {elapsed:.2f}s "
        f"for 5 calls of {delay}s each (serial would be ~{delay * 5:.2f}s)"
    )


def test_get_password_hash_async_offloads_to_threadpool_without_blocking_event_loop(
    monkeypatch,
):
    import bcrypt as bcrypt_module

    from app.core.security import get_password_hash_async

    delay = 0.2
    original_hashpw = bcrypt_module.hashpw

    def slow_hashpw(password, salt):
        time.sleep(delay)
        return original_hashpw(password, salt)

    monkeypatch.setattr(bcrypt_module, "hashpw", slow_hashpw)

    async def run_concurrently():
        n = 5
        started = time.monotonic()
        results = await asyncio.gather(
            *[get_password_hash_async("Str0ngPass") for _ in range(n)]
        )
        elapsed = time.monotonic() - started
        return results, elapsed

    results, elapsed = asyncio.run(run_concurrently())

    assert len(results) == 5
    assert all(h.startswith("$2b$") for h in results)
    assert elapsed < delay * 3, (
        f"expected concurrent bcrypt calls to overlap, took {elapsed:.2f}s "
        f"for 5 calls of {delay}s each (serial would be ~{delay * 5:.2f}s)"
    )


def test_async_password_wrappers_match_sync_semantics():
    # The async wrappers must be pure offloading shims - same hash format,
    # same verify result, wrong password still rejected - not a different
    # code path with different behavior.
    import asyncio

    from app.core.security import (
        get_password_hash,
        get_password_hash_async,
        verify_password,
        verify_password_async,
    )

    async def run():
        hashed = await get_password_hash_async("Str0ngPass")
        correct = await verify_password_async("Str0ngPass", hashed)
        wrong = await verify_password_async("wrong-password", hashed)
        return hashed, correct, wrong

    hashed, correct, wrong = asyncio.run(run())

    assert hashed.startswith("$2b$")
    assert correct is True
    assert wrong is False
    # Cross-check against the sync functions directly: a hash produced by
    # the async wrapper verifies correctly through the sync path, and
    # vice versa - confirming they're the same bcrypt calls, not divergent
    # implementations.
    assert verify_password("Str0ngPass", hashed) is True
    sync_hashed = get_password_hash("Str0ngPass")
    assert asyncio.run(verify_password_async("Str0ngPass", sync_hashed)) is True


def test_sync_password_helpers_still_work_outside_an_event_loop():
    # Celery tasks (app/tasks.py's GDPR purge) and one-off scripts
    # (scripts/seed_demo_data.py, scripts/pg_smoke_test.py) call
    # get_password_hash/verify_password directly with no event loop running.
    # These must keep working as plain sync functions - they must NOT
    # require asyncio.run or an active event loop to succeed.
    import asyncio

    from app.core.security import get_password_hash, verify_password

    with pytest.raises(RuntimeError):
        # Sanity check this test genuinely has no running event loop.
        asyncio.get_running_loop()

    hashed = get_password_hash("Str0ngPass")
    assert hashed.startswith("$2b$")
    assert verify_password("Str0ngPass", hashed) is True
    assert verify_password("wrong-password", hashed) is False
