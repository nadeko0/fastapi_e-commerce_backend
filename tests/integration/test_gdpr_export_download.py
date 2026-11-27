"""
A personal data export (GDPR Art. 15/20) from the customer's side:
POST /legal/data-request records the request and publishes the build
(recorded by the export_builds fixture, the task body run here as a worker
would), and GET /legal/data-export/{request_id} serves the archive to the
account that asked, until it expires.
"""
import io
import zipfile
from datetime import timedelta

import pytest
from payment_support import Shop

from app import tasks
from app.core.clock import utc_now
from app.models.user import User
from app.services.gdpr_export import export_path

LEGAL = "/api/v1/legal"
ARCHIVE_FILES = {"account.json", "addresses.json", "orders.json", "payments.json", "refunds.json"}


@pytest.fixture
def shop(client, db_session, tasks_db):
    return Shop(client, db_session)


@pytest.fixture
def mailbox(monkeypatch):
    """Every mail sent, as (recipient, subject, body)."""
    sent = []

    class Capture:
        def send(self, to_email, subject, html_content):
            sent.append((to_email, subject, html_content))
            return True

    monkeypatch.setattr("app.services.email.get_email_provider", Capture)
    return sent


def _request_export(shop, buyer):
    response = shop.client.post(f"{LEGAL}/data-request", json={"request_type": "export"},
                                headers=buyer.headers)
    assert response.status_code == 200, response.text
    return response.json()["request_id"]


def _download(shop, buyer, request_id):
    return shop.client.get(f"{LEGAL}/data-export/{request_id}", headers=buyer.headers)


def _set_entry(shop, user_id, request_id, **changes):
    user = shop.db.get(User, user_id)
    user.data_export_requests = [
        {**e, **changes} if e.get("request_id") == request_id else e
        for e in user.data_export_requests
    ]
    shop.db.commit()


def _entries(shop, user_id):
    shop.db.expire_all()
    return shop.db.get(User, user_id).data_export_requests


def test_an_export_is_built_then_downloaded_by_its_owner(shop, export_builds):
    buyer = shop.buyer()
    request_id = _request_export(shop, buyer)
    assert export_builds == [(buyer.user_id, request_id)]

    building = _download(shop, buyer, request_id)
    assert building.status_code == 409
    assert building.headers["retry-after"] == "60"

    assert tasks.build_gdpr_export.run(buyer.user_id, request_id) == "ready"
    archive = _download(shop, buyer, request_id)

    assert archive.status_code == 200
    assert archive.headers["content-type"] == "application/zip"
    assert archive.headers["cache-control"] == "no-store"
    assert f'filename="{request_id}.zip"' in archive.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
        assert set(zf.namelist()) == ARCHIVE_FILES
        assert buyer.email in zf.read("account.json").decode()
        assert "1 Main Street" in zf.read("addresses.json").decode()


def test_a_second_request_answers_the_export_in_progress_or_ready(shop, export_builds):
    buyer = shop.buyer()
    first = _request_export(shop, buyer)

    assert _request_export(shop, buyer) == first
    tasks.build_gdpr_export.run(buyer.user_id, first)
    assert _request_export(shop, buyer) == first

    assert export_builds == [(buyer.user_id, first)]
    assert len(_entries(shop, buyer.user_id)) == 1


@pytest.mark.parametrize("ended", [
    {"status": "ready", "expires_at": (utc_now() - timedelta(minutes=1)).isoformat()},
    {"status": "failed"},
    # A build that never finished (worker killed): taken as lost after an hour.
    {"status": "building", "requested_at": (utc_now() - timedelta(hours=1, minutes=1)).isoformat()},
])
def test_an_export_that_ended_is_not_served_and_a_new_request_starts_over(shop, export_builds, ended):
    buyer = shop.buyer()
    first = _request_export(shop, buyer)
    tasks.build_gdpr_export.run(buyer.user_id, first)
    _set_entry(shop, buyer.user_id, first, **ended)

    assert _download(shop, buyer, first).status_code == 404
    second = _request_export(shop, buyer)

    assert second != first
    assert export_builds == [(buyer.user_id, first), (buyer.user_id, second)]


def test_an_entry_from_before_archives_does_not_block_a_new_export(shop, export_builds):
    buyer = shop.buyer()
    user = shop.db.get(User, buyer.user_id)
    user.data_export_requests = [{"request_id": "export_1_1700000000.0",
                                  "requested_at": utc_now().isoformat()}]
    shop.db.commit()

    request_id = _request_export(shop, buyer)

    assert export_builds == [(buyer.user_id, request_id)]
    assert _entries(shop, buyer.user_id)[0] == {"request_id": "export_1_1700000000.0",
                                                "requested_at": user.data_export_requests[0]["requested_at"]}


def test_another_account_cannot_download_it(shop):
    owner, other = shop.buyer(), shop.buyer()
    request_id = _request_export(shop, owner)
    tasks.build_gdpr_export.run(owner.user_id, request_id)

    assert _download(shop, other, request_id).status_code == 404
    assert shop.client.get(f"{LEGAL}/data-export/{request_id}").status_code == 401


def test_a_ready_export_whose_file_is_gone_is_a_404(shop):
    buyer = shop.buyer()
    request_id = _request_export(shop, buyer)
    tasks.build_gdpr_export.run(buyer.user_id, request_id)
    export_path(request_id).unlink()

    assert _download(shop, buyer, request_id).status_code == 404


def _request_erasure(shop, buyer):
    response = shop.client.post(f"{LEGAL}/data-request", headers=buyer.headers,
                                json={"request_type": "deletion", "password": "Str0ngPass1"})
    assert response.status_code == 200, response.text


def test_an_erasure_request_deletes_the_ready_archive_at_once(shop):
    buyer = shop.buyer()
    request_id = _request_export(shop, buyer)
    tasks.build_gdpr_export.run(buyer.user_id, request_id)
    assert export_path(request_id).is_file()

    _request_erasure(shop, buyer)

    assert not export_path(request_id).exists()
    assert _download(shop, buyer, request_id).status_code == 403


def test_a_build_for_an_account_erased_meanwhile_keeps_nothing(shop, mailbox):
    buyer = shop.buyer()
    request_id = _request_export(shop, buyer)
    _request_erasure(shop, buyer)

    assert tasks.build_gdpr_export.run(buyer.user_id, request_id) == "gone"

    assert not export_path(request_id).exists()
    assert not [m for m in mailbox if "export is ready" in m[1]]


@pytest.mark.parametrize("request_id", ["export_1_123", "../etc/passwd", "export_" + "g" * 32])
def test_a_request_id_of_another_shape_is_refused(shop, request_id):
    buyer = shop.buyer()

    response = _download(shop, buyer, request_id)

    assert response.status_code in (404, 422)
    if response.status_code == 422:
        assert "request_id" in response.text


def test_an_export_the_broker_cannot_take_is_a_503_and_can_be_requested_again(
    shop, monkeypatch, export_builds
):
    buyer = shop.buyer()

    def _broker_down(*args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(tasks.build_gdpr_export, "apply_async", _broker_down)
    response = shop.client.post(f"{LEGAL}/data-request", json={"request_type": "export"},
                                headers=buyer.headers)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    [entry] = _entries(shop, buyer.user_id)
    assert entry["status"] == "failed"

    monkeypatch.setattr(tasks.build_gdpr_export, "apply_async",
                        lambda args=None, **options: export_builds.append(tuple(args)))
    request_id = _request_export(shop, buyer)
    assert request_id != entry["request_id"]
    assert export_builds == [(buyer.user_id, request_id)]
