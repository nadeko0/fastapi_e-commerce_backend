from datetime import UTC, datetime

import pytest

from app.api.v1.cart import guest_cart_cookie_name
from app.api.v1.legal import get_cookie_policy, get_privacy_policy, get_terms_of_service
from app.core.config import settings
from app.services.legal_documents import load_legal_document

BUILTIN_DOCUMENTS = ("privacy-policy", "terms-of-service", "cookie-policy")


@pytest.mark.parametrize("name", BUILTIN_DOCUMENTS)
def test_builtin_documents_load_with_expected_version_and_date(name):
    doc = load_legal_document(name)

    assert doc.version == "1.1"
    assert doc.effective_date == datetime(2026, 11, 28, tzinfo=UTC)
    assert "{last_updated}" in doc.text


def test_unknown_placeholder_raises_value_error_naming_it(tmp_path, monkeypatch):
    (tmp_path / "privacy-policy.md").write_text(
        '---\nversion: "2.0"\neffective: 2026-12-01\n---\n\nHello {unexpected_token}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "LEGAL_DOCS_DIR", str(tmp_path))

    with pytest.raises(ValueError, match="unexpected_token"):
        get_privacy_policy()


def test_legal_docs_dir_overrides_builtin_text_and_caches_per_dir_and_name(tmp_path, monkeypatch):
    override = tmp_path / "terms-of-service.md"
    override.write_text(
        '---\nversion: "2.3"\neffective: 2026-12-15\n---\n\nCustom terms for {company_name} on {last_updated}.\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "LEGAL_DOCS_DIR", str(tmp_path))

    rendered = get_terms_of_service()
    assert rendered.version == "2.3"
    assert rendered.last_updated == datetime(2026, 12, 15, tzinfo=UTC)
    assert rendered.content == f"Custom terms for {settings.PROJECT_NAME} on 2026-12-15."

    # Cached per (dir, name): changing the file in place keeps the cached version until dir changes.
    override.write_text(
        '---\nversion: "9.9"\neffective: 2027-01-01\n---\n\nChanged on disk\n',
        encoding="utf-8",
    )
    assert get_terms_of_service().version == "2.3"

    # Switching back to built-in resolves a different (dir, name) key.
    monkeypatch.setattr(settings, "LEGAL_DOCS_DIR", None)
    assert get_terms_of_service().version == "1.1"


def test_rendered_terms_and_privacy_reflect_legal_corrections():
    terms = get_terms_of_service()
    lowered_terms = terms.content.lower()

    assert "online dispute resolution" not in lowered_terms
    assert "odr" not in lowered_terms
    assert "alternative dispute resolution" in lowered_terms
    assert "14 days of being informed" in terms.content
    assert "at least 16 years old" in terms.content
    assert "gdpr requirement" not in lowered_terms

    privacy = get_privacy_policy()
    assert "Stripe" in privacy.content
    assert "Article 18" in privacy.content
    assert "GDPR Chapter V" in privacy.content


def test_rendered_cookie_policy_names_only_guest_cart_cookie():
    policy = get_cookie_policy()
    lowered = policy.content.lower()

    assert guest_cart_cookie_name() in policy.content
    assert str(settings.REDIS_GUEST_CART_TTL_DAYS) in policy.content
    assert "csrf" not in lowered
    assert "session" not in lowered
