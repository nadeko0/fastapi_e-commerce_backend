from sqlalchemy import update

import app.api.v1.users as users_mod
import app.core.security as sec
from app.core.security import get_password_hash, invalidate_tokens_issued_before_now
from app.models.user import User

PW = "Zx9!qwerty-unique"


def _register(client, email):
    client.post("/api/v1/users/register", json={
        "email": email, "password": PW, "gdpr_consent": True, "privacy_policy_accepted": True})


def test_2c02_login_in_flight_during_password_reset_survives_it(client, db_session, monkeypatch):
    _register(client, "r@example.com")
    uid = db_session.query(User).filter_by(email="r@example.com").one().id
    real_verify = users_mod.verify_password_async

    async def verify_then_reset_happens(plain, hashed):
        ok = await real_verify(plain, hashed)
        # The victim completes POST /password/reset/confirm while this
        # attacker login (old password) is inside bcrypt:
        await invalidate_tokens_issued_before_now(uid)
        db_session.execute(update(User).where(User.id == uid).values(
            hashed_password=get_password_hash("N3w-Pass-phrase!")))
        db_session.commit()
        return ok

    with monkeypatch.context() as m:
        m.setattr(users_mod, "verify_password_async", verify_then_reset_happens)
        tokens = client.post("/api/v1/users/login", data={"username": "r@example.com", "password": PW}).json()
    me = client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.status_code == 200  # attacker's token minted from the OLD password survives the reset
    ref = client.post("/api/v1/users/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert ref.status_code == 200  # and so does the refresh chain


def test_2c03_rotation_in_flight_during_password_reset_survives_it(client, db_session, monkeypatch):
    _register(client, "s@example.com")
    uid = db_session.query(User).filter_by(email="s@example.com").one().id
    tokens = client.post("/api/v1/users/login", data={"username": "s@example.com", "password": PW}).json()
    real = sec.is_token_superseded

    async def check_then_reset(user_id, iat):
        result = await real(user_id, iat)
        await invalidate_tokens_issued_before_now(uid)  # reset lands right after the check
        return result

    with monkeypatch.context() as m:
        m.setattr(sec, "is_token_superseded", check_then_reset)
        rotated = client.post("/api/v1/users/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200
    again = client.post("/api/v1/users/refresh", json={"refresh_token": rotated.json()["refresh_token"]})
    assert again.status_code == 200  # chain survives the reset indefinitely


def test_2c09_concurrent_double_refresh_logs_out_winner(client, db_session, monkeypatch):
    _register(client, "t@example.com")
    tokens = client.post("/api/v1/users/login", data={"username": "t@example.com", "password": PW}).json()
    first = client.post("/api/v1/users/refresh", json={"refresh_token": tokens["refresh_token"]})
    # second tab replays the same (now spent) token
    second = client.post("/api/v1/users/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert first.status_code == 200 and second.status_code == 401
    third = client.post("/api/v1/users/refresh", json={"refresh_token": first.json()["refresh_token"]})
    assert third.status_code == 401  # winner's fresh token was revoked too (by design)


def test_2c04_deletion_password_oracle_not_counted(client, db_session):
    _register(client, "u@example.com")
    tok = client.post("/api/v1/users/login", data={"username": "u@example.com", "password": PW}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    for i in range(30):
        r = client.post("/api/v1/legal/data-request", headers=h,
                        json={"request_type": "deletion", "password": f"guess-{i}"})
        assert r.status_code == 401
