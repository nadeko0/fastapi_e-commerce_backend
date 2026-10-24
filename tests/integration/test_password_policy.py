"""Password policy (NIST SP 800-63B-4 3.1.1.2): no composition rules, any
Unicode, NFC before hashing and verifying, a blocklist and context words at
registration and reset, never at login."""

import unicodedata

import bcrypt
import pytest
from conftest import TestingSessionLocal

from app.core.passwords import BLOCKED_MESSAGE, is_blocked_password
from app.core.security import generate_password_reset_token, verify_password
from app.models.user import User

USERS = "/api/v1/users"
EMAIL = "policy.user@example.com"


def _payload(password, email=EMAIL):
    return {
        "email": email, "password": password, "full_name": "Policy User",
        "gdpr_consent": True, "privacy_policy_accepted": True,
    }


def _login(client, password, email=EMAIL):
    return client.post(f"{USERS}/login", data={"username": email, "password": password})


@pytest.mark.parametrize("password", [
    "correct horse battery staple",
    "Passw0rd.2024",
    "Correct-Horse-Battery9",
    "Pässwörd12345",
    "onlylettersandspaces here",
    "12345678901234",
    "Str0ngPass1",
])
def test_any_composition_is_accepted(client, password):
    assert client.post(f"{USERS}/register", json=_payload(password)).status_code == 200
    assert _login(client, password).status_code == 200


@pytest.mark.parametrize("password", ["password", "Password", "PASSWORD", "12345678", "qwertyuiop", "iloveyou"])
def test_blocklisted_password_is_refused_at_registration(client, password):
    response = client.post(f"{USERS}/register", json=_payload(password))

    assert response.status_code == 422
    assert BLOCKED_MESSAGE in str(response.json())


@pytest.mark.parametrize("password", ["policy.user", "POLICY.USER-2024!", "my-policy.user-pass"])
def test_email_local_part_is_refused(client, password):
    response = client.post(f"{USERS}/register", json=_payload(password))

    assert response.status_code == 422
    assert BLOCKED_MESSAGE in str(response.json())


def test_service_name_is_refused(client):
    assert client.post(f"{USERS}/register", json=_payload("E-commerce Backend!")).status_code == 422


def test_blocklist_is_whole_password_not_substring():
    assert is_blocked_password("password") is True
    assert is_blocked_password("a purple horse said password to me") is False


def test_length_rules_are_unchanged(client):
    assert client.post(f"{USERS}/register", json=_payload("sevenXX")).status_code == 422
    too_long = client.post(f"{USERS}/register", json=_payload("x7" * 40))
    assert too_long.status_code == 422
    assert "72 bytes" in str(too_long.json())


def test_composed_and_decomposed_forms_are_the_same_password(client):
    composed = "café au lait 42"
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed

    assert client.post(f"{USERS}/register", json=_payload(composed)).status_code == 200

    assert _login(client, composed).status_code == 200
    assert _login(client, decomposed).status_code == 200


def test_decomposed_registration_logs_in_with_composed(client):
    composed = "café au lait 42"
    decomposed = unicodedata.normalize("NFD", composed)

    assert client.post(f"{USERS}/register", json=_payload(decomposed)).status_code == 200

    assert _login(client, composed).status_code == 200


def test_hash_made_before_normalisation_still_verifies():
    # Existing users: hashes made from the input as typed (not NFC).
    decomposed = unicodedata.normalize("NFD", "café au lait 42")
    old_hash = bcrypt.hashpw(decomposed.encode(), bcrypt.gensalt(rounds=4)).decode()
    ascii_hash = bcrypt.hashpw(b"Str0ngPass1", bcrypt.gensalt(rounds=4)).decode()

    assert verify_password(decomposed, old_hash) is True
    assert verify_password("Str0ngPass1", ascii_hash) is True
    assert verify_password("Str0ngPass2", ascii_hash) is False
    assert verify_password(unicodedata.normalize("NFC", decomposed), old_hash) is False


def test_login_never_applies_the_policy(client):
    # An account whose stored password is blocklisted (made before the rule)
    # must still log in.
    from app.core.security import get_password_hash

    client.post(f"{USERS}/register", json=_payload("Str0ngPass1"))
    db = TestingSessionLocal()
    try:
        user = db.query(User).filter(User.email == EMAIL).one()
        user.hashed_password = get_password_hash("password1")
        db.commit()
    finally:
        db.close()

    assert _login(client, "password1").status_code == 200


def test_reset_applies_the_policy_and_accepts_passphrases(client):
    client.post(f"{USERS}/register", json=_payload("Str0ngPass1"))
    db = TestingSessionLocal()
    try:
        user = db.query(User).filter(User.email == EMAIL).one()
        token = generate_password_reset_token(user.email, user.hashed_password)
    finally:
        db.close()
    url = f"{USERS}/password/reset/confirm"

    for bad in ("password", "policy.user-2024"):
        refused = client.post(url, json={"token": token, "current_password": "x", "new_password": bad})
        assert refused.status_code == 422, bad

    ok = client.post(url, json={"token": token, "current_password": "x", "new_password": "correct horse battery staple"})
    assert ok.status_code == 200
    assert _login(client, "correct horse battery staple").status_code == 200
