import torture_support  # noqa: F401, I001  (must precede payment_support & co.)

# Protocol & standards torture: NIST SP 800-63B / Unicode, RFC 7396 (JSON Merge
# Patch), RFC 5789 (PATCH), RFC 9110 (conditional requests), RFC 8259 (JSON),
# money and pagination boundaries, RFC 9457 (problem details).
#
# Every response a test here receives goes through `observe`, which enforces the
# RFC 9457 shape of every error and that nothing internal leaks. Tests named
# `test_bug_<name>` are minimal deterministic reproducers of a backend defect a
# property test found; see .cloud_torture/02b_protocol_torture.md.
#
# Runs on SQLite (default) and on PostgreSQL (TEST_DATABASE_URL=...).
#
# Phase 3 validation (.cloud_torture/03a_validation_protocol_boundary.md):
# every `test_bug_*` that reproduces a confirmed backend defect is a strict
# xfail ("BUG DISCOVERED: ..."), so the suite is green and turns red the day
# the defect is fixed (XPASS(strict)), as a reminder to drop the marker. The
# property tests stay green by leaving the known-bug input class out of their
# generators (each exclusion names its strict-xfail reproducer); set
# TORTURE_STRICT=1 to put those inputs back and re-reveal the bugs.
import json
import os
import re
import unicodedata
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import pytest
from conftest import IS_POSTGRES, scaled_examples
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st
from payment_support import Shop
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func, select

from app.core.passwords import is_blocked_password
from app.main import app
from app.models.product import ProductVariant
from app.models.user import User
from app.schemas.address import AddressUpdate
from app.schemas.product import ProductUpdate, ProductVariantUpdate
from app.schemas.user import UserUpdate, normalize_email

# TORTURE_STRICT=1: property tests also generate the inputs of the known
# (strict-xfailed) bugs below, so they fail again.
TORTURE_STRICT = os.environ.get("TORTURE_STRICT") == "1"


def bug(reason: str, condition: bool = True):
    """Strict xfail for a confirmed backend defect (an XPASS fails the run)."""
    return pytest.mark.xfail(condition, strict=True, reason=f"BUG DISCOVERED: {reason}")


def strict_only(decorator):
    """Apply a Hypothesis @example that hits a known bug only under TORTURE_STRICT=1."""
    return decorator if TORTURE_STRICT else (lambda test: test)


BUG_B1 = ("login looks the account up by normalize_email() only, registration stores email-validator's "
          "IDNA/UTS #46 form (app/api/v1/users.py:124 vs app/schemas/user.py:104-108)")
BUG_B2 = ("free-form JSON nested ~255 deep is stored, then every serialisation of it is a 500 "
          "(no depth bound in app/core/text_input.py:69 first_unusable_text; commit at app/api/v1/admin.py:212)")
BUG_B3 = ("NaN/Infinity/1e400 in free-form JSON accepted (app/core/text_input.py:58, RequestBody): "
          "500 from jsonb on PostgreSQL, silently stored as null on SQLite")
BUG_B4 = ("a '*' anywhere in If-Match/If-None-Match is read as the wildcard "
          "(app/core/etag.py:21 _TAG with finditer, :29-31 _parse)")
BUG_B5 = ("money accepts non-ASCII Unicode digits: str-pattern \\d at app/schemas/order.py:45/47 "
          "(the published OpenAPI pattern is ASCII)")
BUG_B7 = ("PATCH with application/json-patch+json (any application/*+json) is applied as a merge "
          "patch instead of 415 (app/api/merge_patch.py, app/core/errors.py:236)")
BUG_B9 = ("price filter with an exponent beyond PostgreSQL numeric is a 500: bare Decimal "
          "min_price/max_price at app/schemas/product.py:140-141")

API = "/api/v1"
PROBLEM = "application/problem+json"
MERGE_PATCH = "application/merge-patch+json"


def _settings(examples: int, **kwargs):
    return settings(
        max_examples=scaled_examples(examples),
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# RFC 9457 strictness + leak detection, applied to every response
# ---------------------------------------------------------------------------

# RFC 3986 URI-reference characters (type and instance are URI references).
_URI_REFERENCE = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*$")
_LEAK_ANYCASE = re.compile(
    r"traceback|sqlalchemy|psycopg|sqlite3|asyncpg|pydantic_core|aiosqlite|"
    r"integrityerror|dataerror|operationalerror|programmingerror|numericvalueoutofrange|"
    r"recursionerror|serializationerror|/home/|site-packages|\.py\b",
    re.IGNORECASE,
)
_LEAK_EXACT = re.compile(
    r'File "|line \d+, in |\bSELECT\b|\bINSERT INTO\b|\bDELETE FROM\b|\bUPDATE \w+ SET\b|'
    r"\bWHERE\b|\bFROM \w+\b|Error serializing|Circular reference"
)


def assert_problem(response) -> dict:
    """RFC 9457: application/problem+json, an object with a URI-reference `type`,
    a string `title`, an int `status` equal to the HTTP status, optional string
    `detail`/`instance`, and nothing internal in it."""
    assert response.headers.get("content-type", "").split(";")[0].strip() == PROBLEM, (
        response.status_code, response.headers.get("content-type"), response.text[:300])
    body = response.json()
    assert isinstance(body, dict), body
    assert isinstance(body.get("type"), str) and body["type"], body
    assert _URI_REFERENCE.fullmatch(body["type"]), body["type"]
    urlsplit(body["type"])  # parses
    assert body["type"] == "about:blank" or urlsplit(body["type"]).scheme in ("https", "http", "urn", "tag"), body
    assert isinstance(body.get("title"), str) and body["title"], body
    assert type(body.get("status")) is int and body["status"] == response.status_code, body
    for member in ("detail", "instance"):
        if member in body:
            assert isinstance(body[member], str), body
    if "instance" in body:
        assert _URI_REFERENCE.fullmatch(body["instance"]), body["instance"]
        assert "?" not in body["instance"], body["instance"]
    if "errors" in body:
        assert isinstance(body["errors"], list)
        for error in body["errors"]:
            assert "input" not in error, error
    assert not _LEAK_ANYCASE.search(response.text), response.text[:500]
    assert not _LEAK_EXACT.search(response.text), response.text[:500]
    return body


def observe(response, *, secrets=()):
    """Shape checks for any response: an error is a strict problem document,
    a 304 has no body, and no secret the request carried comes back."""
    if response.status_code >= 400:
        assert_problem(response)
    if response.status_code == 304:
        assert response.content == b"", response.content
        assert "etag" in response.headers
    for secret in secrets:
        if secret:
            assert secret not in response.text, "request secret echoed in the response"
    return response


def no_5xx(response, *, secrets=()):
    observe(response, secrets=secrets)
    assert response.status_code < 500, (response.status_code, response.text[:300])
    return response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api(monkeypatch, fake_redis):
    """The real app, but a server error comes back as the 500 response a
    client would see (so its problem document can be checked) instead of
    being re-raised into the test."""
    monkeypatch.setattr("app.api.v1.users.send_welcome_email", lambda *a, **k: True)
    monkeypatch.setattr("app.api.v1.users.send_account_exists_email", lambda *a, **k: True)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


class World:
    def __init__(self, api, db_session):
        self.api = api
        self.db = db_session
        self.shop = Shop(api, db_session)
        self.admin = self.shop.admin()
        self.product = self.shop.product()
        self.variant = self.shop.variant(self.product)
        self.buyer = self.shop.buyer()

    @property
    def product_url(self):
        return f"{API}/admin/products/{self.product.id}"

    @property
    def variant_url(self):
        return f"{API}/admin/products/{self.product.id}/variants/{self.variant.id}"

    @property
    def address_url(self):
        return f"{API}/users/addresses/{self.buyer.address_id}"

    def product_etag(self):
        response = observe(self.api.get(f"{API}/products/{self.product.id}"))
        assert response.status_code == 200, response.text
        return response.headers["etag"], response.json()

    def variant_etag(self):
        response = observe(self.api.get(self.variant_url, headers=self.admin))
        assert response.status_code == 200, response.text
        return response.headers["etag"], response.json()


@pytest.fixture
def world(api, db_session):
    return World(api, db_session)


# PATCH targets: (id, url of the world, auth of the world, update schema)
PATCH_TARGETS = [
    ("user", lambda w: f"{API}/users/me", lambda w: w.buyer.headers, UserUpdate),
    ("address", lambda w: w.address_url, lambda w: w.buyer.headers, AddressUpdate),
    ("product", lambda w: w.product_url, lambda w: w.admin, ProductUpdate),
    ("variant", lambda w: w.variant_url, lambda w: w.admin, ProductVariantUpdate),
]


def _patch(world, target, body, content_type=MERGE_PATCH, extra_headers=None):
    _, url, auth, _ = target
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    headers = {**auth(world), "content-type": content_type, **(extra_headers or {})}
    return world.api.patch(url(world), content=content, headers=headers)


# ---------------------------------------------------------------------------
# 1. NIST SP 800-63B, Unicode normalisation, bcrypt's 72 bytes, emails
# ---------------------------------------------------------------------------

FILLER = "Kx9!mV2@pQ7#zL4$wR8%"


def _filler(length: int) -> str:
    return (FILLER * (length // len(FILLER) + 1))[:length]


def _email(prefix="pw") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


def _register(api, email, password, *, raw: bytes | None = None):
    if raw is not None:
        return api.post(f"{API}/users/register", content=raw,
                        headers={"content-type": "application/json"})
    payload = {"email": email, "password": password, "gdpr_consent": True,
               "privacy_policy_accepted": True}
    return api.post(f"{API}/users/register", json=payload)


def _login(api, email, password):
    return api.post(f"{API}/users/login", data={"username": email, "password": password})


def _nfc(text):
    return unicodedata.normalize("NFC", text)


def _utf8_len(text):
    return len(text.encode("utf-8"))


# Characters that stress normalisation, invisibles and astral planes.
SPECIAL_CHARS = [
    "‍", "​", "﻿", "‮", "‭", "⁦", "́", "̈",
    "̧", "Å", "Å", "K", "ﬁ", "Ａ", "²", "Ⅳ",
    "각", "각", "\U0001f510", "\U0001f469‍\U0001f469‍\U0001f467",
    "\U0001d400", " ", "\t", "　", " ", "İ", "ß", "Σ",
]
_password_chars = st.one_of(
    st.characters(codec="utf-8", exclude_characters="\x00"),
    st.sampled_from(SPECIAL_CHARS),
)
passwords = st.lists(_password_chars, min_size=8, max_size=28).map("".join)


@_settings(60)
@given(password=passwords)
@example(password="Café-au-lait-Ångström")
@example(password="\U0001f510" * 18)
def test_password_round_trips_and_canonical_equivalents_log_in(api, password):
    """Registration then login with the same password always works, and so does
    any canonically equivalent spelling (NFC / NFD), the policy the project
    documents (app/core/passwords.py, NFC before hashing). The password is never
    echoed. Register answers 202 or 422, never 500."""
    email = _email()
    nfc = _nfc(password)
    assume(len(nfc) >= 8 and len(password) >= 8 and _utf8_len(nfc) <= 72)
    assume(not is_blocked_password(nfc, email))

    created = no_5xx(_register(api, email, password), secrets=[password])
    assert created.status_code == 202, created.text

    for spelling in {password, nfc, unicodedata.normalize("NFD", password)}:
        response = no_5xx(_login(api, email, spelling), secrets=[spelling])
        assert response.status_code == 200, (repr(spelling), response.text)


# (registered as, logged in with, same password under the documented NFC policy?)
NORMALISATION_CASES = [
    pytest.param("Ångström-Pass9", "Ångström-Pass9", True, id="angstrom-sign-vs-A-ring"),
    pytest.param("Ångström-Pass9", "Ångström-Pass9", True, id="precomposed-vs-decomposed"),
    pytest.param("Kelvin-Pass-77", "Kelvin-Pass-77", True, id="kelvin-sign-vs-K"),
    pytest.param("각각-pw-91", "각각-pw-91", True, id="hangul-jamo-vs-syllable"),
    pytest.param("café-ḉ-Pass", "café-ḉ-Pass", True, id="multi-combining-reordered"),
    # Compatibility (NFKC-only) equivalents: different passwords under NFC.
    pytest.param("ﬁligature-Pass1", "filigature-Pass1", False, id="fi-ligature-nfkc-only"),
    pytest.param("Ｐａｓｓ-Wide-77", "Pass-Wide-77", False, id="fullwidth-nfkc-only"),
    pytest.param("Square²-Pass-x", "Square2-Pass-x", False, id="superscript-nfkc-only"),
    pytest.param("RomanⅣ-Pass-xy", "RomanIV-Pass-xy", False, id="roman-numeral-nfkc-only"),
    pytest.param("nbsp Pass Word9", "nbsp Pass Word9", False, id="nbsp-vs-space-nfkc-only"),
    # Invisible characters are part of the password, never stripped.
    pytest.param("zw‍j-Pass-Word9", "zwj-Pass-Word9", False, id="zwj-not-stripped"),
    pytest.param("zw​sp-Pass-Word9", "zwsp-Pass-Word9", False, id="zwsp-not-stripped"),
    pytest.param("﻿bom-Pass-Word9", "bom-Pass-Word9", False, id="bom-not-stripped"),
    pytest.param("rtl‮9droW-ssaP", "rtl9droW-ssaP", False, id="rtl-override-not-stripped"),
]


@pytest.mark.parametrize("registered, typed, same", NORMALISATION_CASES)
def test_password_normalisation_policy(api, registered, typed, same):
    """Canonical equivalents (NFC) are one password; compatibility equivalents
    (NFKC only) and invisible characters are not - the project documents NFC
    (app/core/passwords.py:32). NIST SP 800-63B (rev. 3, 5.1.1.2) recommends
    NFKC/NFKD instead: recorded as a policy observation in the log."""
    email = _email()
    created = no_5xx(_register(api, email, registered), secrets=[registered])
    assert created.status_code == 202, created.text
    assert no_5xx(_login(api, email, registered)).status_code == 200
    response = no_5xx(_login(api, email, typed), secrets=[typed])
    assert response.status_code == (200 if same else 401), response.text


@pytest.mark.parametrize("password, refused", [
    pytest.param("password", True, id="ascii"),
    pytest.param("PASSWORD", True, id="case-folded"),
    pytest.param("pa\u0073\u0073word", True, id="escaped-ascii"),
    # Compatibility / invisible spellings: not refused under NFC + casefold.
    pytest.param("\uff50\uff41\uff53\uff53\uff57\uff4f\uff52\uff44", False, id="fullwidth"),
    pytest.param("pass\u200bword", False, id="zwsp-inside"),
])
def test_blocklist_compares_the_nfc_casefolded_password(api, password, refused):
    """Policy record (app/core/passwords.py:_fold): the blocklist is compared on
    NFC + casefold. NFKC spellings and invisible characters of a listed password
    pass; NIST SP 800-63B 5.1.1.2's NFKC/NFKD would catch the fullwidth one.
    Logged as an observation, not a defect."""
    # (no echo check: the literal "password" is also the field name in `loc`)
    response = no_5xx(_register(api, _email("blk"), password))
    assert response.status_code == (422 if refused else 202), response.text
    assert "input" not in response.text


@pytest.mark.parametrize("password, status", [
    pytest.param(_filler(7), 422, id="ascii-7"),
    pytest.param(_filler(8), 202, id="ascii-8"),
    pytest.param(_filler(63), 202, id="ascii-63"),
    pytest.param(_filler(64), 202, id="ascii-64"),
    pytest.param(_filler(65), 202, id="ascii-65"),
    pytest.param(_filler(72), 202, id="ascii-72-bytes"),
    pytest.param(_filler(73), 422, id="ascii-73-bytes"),
    pytest.param(_filler(128), 422, id="ascii-128"),
    pytest.param(_filler(129), 422, id="ascii-129"),
    pytest.param("\U0001f510" * 18, 202, id="astral-18x4-72-bytes"),
    pytest.param("\U0001f510" * 19, 422, id="astral-19x4-76-bytes"),
    pytest.param("€" * 24, 202, id="euro-24x3-72-bytes"),
    pytest.param("€" * 25, 422, id="euro-25x3-75-bytes"),
    pytest.param("é" * 36, 202, id="precomposed-36x2-72-bytes"),
    # 108 bytes as typed, 72 bytes once composed: the NFC form is what counts.
    pytest.param("é" * 36, 202, id="decomposed-36-is-72-bytes-nfc"),
    pytest.param("é" * 37, 422, id="decomposed-37-is-74-bytes-nfc"),
    # 8 code points as typed, 7 once composed.
    pytest.param("é" * 4 + "xyz", 422, id="decomposed-shrinks-below-8"),
    pytest.param("a" + "́" * 30 + "Zq9!xy", 202, id="combining-stack-30"),
    pytest.param("é" * 64, 422, id="nist-64-chars-128-bytes"),
])
def test_password_length_boundaries(api, password, status):
    """Length rules as documented (app/schemas/user.py: at least 8 characters
    after NFC, at most 72 bytes of UTF-8). A password the policy admits logs in
    with the same text, as typed or composed."""
    email = _email("len")
    assert not is_blocked_password(_nfc(password), email)
    response = no_5xx(_register(api, email, password), secrets=[password])
    assert response.status_code == status, response.text
    if status == 202:
        assert no_5xx(_login(api, email, password)).status_code == 200
        assert no_5xx(_login(api, email, _nfc(password))).status_code == 200


@_settings(40)
@given(
    base=st.text(alphabet=st.characters(codec="utf-8", exclude_characters="\x00"),
                 min_size=1, max_size=72),
    suffix=st.text(alphabet=st.characters(codec="utf-8", exclude_characters="\x00"),
                   min_size=1, max_size=40),
)
def test_passwords_differing_after_byte_72_never_collide(api, base, suffix):
    """bcrypt reads 72 bytes. A registered password P (at most 72 bytes) must not
    be matched by P + anything: the longer one is refused at registration (422)
    and is a wrong password at login (401), never a truncated match."""
    email = _email("b72")
    nfc = _nfc(base)
    # Pad the password to exactly 72 UTF-8 bytes where possible.
    padded = nfc + _filler(max(0, 72 - _utf8_len(nfc)))
    assume(len(padded) >= 8 and _utf8_len(padded) <= 72 and _nfc(padded) == padded)
    assume(not is_blocked_password(padded, email))
    assert no_5xx(_register(api, email, padded)).status_code == 202
    longer = padded + suffix
    assert no_5xx(_register(api, _email("b72x"), longer), secrets=[longer]).status_code == 422
    assert no_5xx(_login(api, email, longer), secrets=[longer]).status_code == 401
    assert no_5xx(_login(api, email, padded)).status_code == 200


@pytest.mark.parametrize("raw_password", [
    pytest.param(r"abc\ud800defgh1", id="lone-high-surrogate"),
    pytest.param(r"abc\udfffdefgh1", id="lone-low-surrogate"),
    pytest.param(r"\udc00\ud800reversed-pair1", id="reversed-pair"),
    pytest.param(r"abc\u0000defgh1", id="nul-escape"),
])
def test_password_that_is_not_unicode_text_is_422(api, raw_password):
    """A lone surrogate or NUL, sent as a JSON escape in raw bytes, is a 422
    validation problem at registration (app/core/text_input.py), not a 500 from
    the encoder or the database."""
    raw = ('{"email":"EMAIL","password":"PASSWORD","gdpr_consent":true,'
           '"privacy_policy_accepted":true}').replace("EMAIL", _email("sur"))
    raw = raw.replace("PASSWORD", raw_password).encode()
    response = no_5xx(_register(api, None, None, raw=raw))
    assert response.status_code == 422, response.text


def test_password_as_a_valid_surrogate_pair_escape_round_trips(api):
    email = _email("pair")
    raw = ('{"email":"EMAIL","password":"lock-\\ud83d\\udd10-Pass9","gdpr_consent":true,'
           '"privacy_policy_accepted":true}').replace("EMAIL", email).encode()
    assert no_5xx(_register(api, None, None, raw=raw)).status_code == 202
    assert no_5xx(_login(api, email, "lock-\U0001f510-Pass9")).status_code == 200


@pytest.mark.parametrize("password", ["x\x00yz-Pass-Word9", "\x00" * 9])
def test_login_with_nul_in_the_password_is_422_not_500(api, password):
    response = no_5xx(_login(api, _email("nul"), password))
    assert response.status_code == 422, response.text


def test_login_with_a_password_far_beyond_72_bytes_is_401(api):
    email = _email("huge")
    assert no_5xx(_register(api, email, _filler(20))).status_code == 202
    response = no_5xx(_login(api, email, _filler(20) + "\U0001f510" * 5000))
    assert response.status_code == 401, response.text


# Emails ---------------------------------------------------------------------

_EMAIL = TypeAdapter(EmailStr)


def _canonical_email(email: str) -> str | None:
    """What registration stores for `email` (normalize_email, then EmailStr),
    or None when it is refused."""
    try:
        return _EMAIL.validate_python(normalize_email(email))
    except ValidationError:
        return None


_LOCAL_ALPHABET = st.sampled_from(
    list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    + list("éÉöÖåÅÅKσΣжЖßİ")
    + ["é", "Å", "ｊ", "ı"]
)
_local_parts = st.lists(_LOCAL_ALPHABET, min_size=1, max_size=12).map("".join)
DOMAINS = [
    "example.com", "EXAMPLE.COM", "Example.Com", "münchen.de", "MÜNCHEN.DE",
    "xn--mnchen-3ya.de", "ｅｘａｍｐｌｅ.com", "example。com",
]
_SPELLINGS = {
    "as-is": lambda e: e,
    "upper": str.upper,
    "lower": str.lower,
    "nfd": lambda e: unicodedata.normalize("NFD", e),
    "nfc": lambda e: unicodedata.normalize("NFC", e),
    "padded": lambda e: f"  {e} ",
}


# Domains whose registered (IDNA/UTS #46) form differs from what login looks up:
# test_bug_login_does_not_normalise_email_like_registration (BUG_B1).
_B1_DOMAINS = {"xn--mnchen-3ya.de", "ｅｘａｍｐｌｅ.com", "example。com"}


@_settings(80)
@given(local=_local_parts, domain=st.sampled_from(DOMAINS))
@strict_only(example(local="jane", domain="xn--mnchen-3ya.de"))
def test_an_account_logs_in_with_the_exact_email_it_registered_with(api, local, domain):
    """Round trip: whatever spelling of an address registration accepts, login
    with that very same string finds the account. (Domains of BUG_B1 are left
    out unless TORTURE_STRICT=1.)"""
    assume(TORTURE_STRICT or domain not in _B1_DOMAINS)
    email = f"{local}{uuid.uuid4().hex[:6]}@{domain}"
    assume(_canonical_email(email) is not None)
    password = "Tr0ub4dor-&-Horse-" + uuid.uuid4().hex[:6]
    assert no_5xx(_register(api, email, password)).status_code == 202
    response = no_5xx(_login(api, email, password))
    assert response.status_code == 200, (email, _canonical_email(email), response.text)


@_settings(80)
@given(local=_local_parts, domain=st.sampled_from(DOMAINS[:5]),
       spelling=st.sampled_from(sorted(_SPELLINGS)))
def test_one_mailbox_one_account_and_no_false_merges(api, db_session, local, domain, spelling):
    """Two registrations create one account exactly when they name the same
    canonical address (case, NFC, IDNA): no duplicates for one mailbox, no
    merging of two different ones. Login with either spelling finds the account
    when they are one."""
    tag = uuid.uuid4().hex[:6]
    first = f"{local}{tag}@{domain}"
    second = _SPELLINGS[spelling](first)
    canon_first, canon_second = _canonical_email(first), _canonical_email(second)
    assume(canon_first is not None and canon_second is not None)

    def count():
        db_session.rollback()
        return db_session.execute(select(func.count(User.id))).scalar_one()

    # Not derived from `tag`: a password containing the email's local part is
    # refused by the context-word rule.
    password = "Corr3ct-H0rse-Battery!"
    assume(not is_blocked_password(password, canon_first))
    assert no_5xx(_register(api, first, password)).status_code == 202
    before = count()
    assert no_5xx(_register(api, second, password + "x")).status_code == 202
    after = count()
    if canon_first == canon_second:
        assert after == before, (first, second, canon_first)
        assert no_5xx(_login(api, second, password)).status_code == 200
    else:
        assert after == before + 1, (first, second, canon_first, canon_second)


@pytest.mark.parametrize("email", [
    pytest.param("jane@xn--mnchen-3ya.de", id="idna-ace-domain", marks=bug(BUG_B1)),
    pytest.param("jane@ｅｘａｍｐｌｅ.com", id="fullwidth-domain", marks=bug(BUG_B1)),
    pytest.param("jane@example。com", id="ideographic-full-stop", marks=bug(BUG_B1)),
])
def test_bug_login_does_not_normalise_email_like_registration(api, email):
    """Registration stores email-validator's normalised address (IDNA/UTS #46
    domain: 'jane@münchen.de', 'jane@example.com'), but login looks the account
    up by normalize_email() alone (NFC + lower()), so the same string the user
    registered with is 'Incorrect email or password'."""
    password = "Corr3ct-H0rse-" + uuid.uuid4().hex[:6]
    assert no_5xx(_register(api, email, password)).status_code == 202
    response = no_5xx(_login(api, email, password))
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# 2. JSON Merge Patch (RFC 7396), PATCH media types (RFC 5789), ETags (RFC 9110)
# ---------------------------------------------------------------------------


def _nullable(model, field: str) -> bool:
    """Whether the request schema (the OpenAPI contract) admits null."""
    prop = model.model_json_schema(mode="validation")["properties"][field]
    options = prop.get("anyOf", [prop])
    return any(option.get("type") == "null" for option in options)


NULL_CASES = [
    pytest.param(target, field, id=f"{target[0]}.{field}")
    for target in PATCH_TARGETS
    for field in target[3].model_fields
]


@pytest.mark.parametrize("target, field", NULL_CASES)
def test_explicit_null_on_every_patch_field_follows_the_schema(world, target, field):
    """RFC 7396: null removes (clears) a member. For every field of every PATCH
    schema: if the published schema admits null the field is cleared (200,
    null in the response); otherwise 422 naming the field - never a 500 from a
    NOT NULL column."""
    response = no_5xx(_patch(world, target, {field: None}))
    if _nullable(target[3], field):
        assert response.status_code == 200, response.text
        assert response.json()[field] is None
    else:
        assert response.status_code == 422, response.text
        assert any(error["loc"][-1] == field for error in response.json()["errors"])


@pytest.mark.parametrize("target", PATCH_TARGETS, ids=[t[0] for t in PATCH_TARGETS])
@pytest.mark.parametrize("body", [b"[]", b'"x"', b"1", b"1.5", b"null", b"true", b"[{}]",
                                  b'[{"op":"replace","path":"/x","value":1}]'])
def test_a_patch_document_that_is_not_an_object_is_refused(world, target, body):
    """A merge patch that is not an object would replace the whole resource
    (RFC 7396 section 2); these resources are objects, so it is a 4xx problem."""
    response = no_5xx(_patch(world, target, body))
    assert response.status_code in (400, 422), response.text


@pytest.mark.parametrize("target", PATCH_TARGETS, ids=[t[0] for t in PATCH_TARGETS])
@_settings(60)
@given(name=st.text(min_size=1, max_size=20), value=st.one_of(
    st.none(), st.booleans(), st.integers(), st.text(max_size=10),
    st.lists(st.integers(), max_size=3), st.dictionaries(st.text(max_size=3), st.integers(), max_size=2)))
def test_unknown_members_are_422_and_never_applied(world, target, name, value):
    assume(name not in target[3].model_fields)
    response = no_5xx(_patch(world, target, {name: value}))
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("target", PATCH_TARGETS, ids=[t[0] for t in PATCH_TARGETS])
@pytest.mark.parametrize("content_type, status", [
    ("application/json", 200),
    (MERGE_PATCH, 200),
    (MERGE_PATCH + "; charset=utf-8", 200),
    ("Application/Merge-Patch+JSON", 200),
    ("text/plain", 415),
    ("application/x-www-form-urlencoded", 415),
    ("application/xml", 415),
])
def test_patch_media_types(world, target, content_type, status):
    body = {
        "user": {"full_name": "Merge Patch"},
        "address": {"city": "Gotham"},
        "product": {"stock_quantity": 7},
        "variant": {"stock_quantity": 7},
    }[target[0]]
    response = no_5xx(_patch(world, target, body, content_type=content_type))
    assert response.status_code == status, response.text
    if status == 415:
        assert MERGE_PATCH in response.headers.get("accept", ""), response.headers


@bug(BUG_B7)
@pytest.mark.parametrize("target", PATCH_TARGETS, ids=[t[0] for t in PATCH_TARGETS])
def test_bug_json_patch_media_type_is_applied_as_a_merge_patch(world, target):
    """RFC 5789 section 2.2: a patch format the server does not support is a 415.
    `application/json-patch+json` (RFC 6902) is matched by FastAPI's
    application/*+json rule and its body is applied with merge-patch semantics."""
    body = {"user": {"full_name": "Json Patch"}, "address": {"city": "Gotham"},
            "product": {"stock_quantity": 7}, "variant": {"stock_quantity": 7}}[target[0]]
    response = no_5xx(_patch(world, target, body, content_type="application/json-patch+json"))
    assert response.status_code == 415, response.text


def rfc7396_merge(target, patch):
    """The reference algorithm of RFC 7396 section 2."""
    if isinstance(patch, dict):
        result = dict(target) if isinstance(target, dict) else {}
        for name, value in patch.items():
            if value is None:
                result.pop(name, None)
            else:
                result[name] = rfc7396_merge(result.get(name), value)
        return result
    return patch


_json_keys = st.text(alphabet=st.characters(codec="utf-8", exclude_characters="\x00"),
                     min_size=1, max_size=4)
_json_scalars = st.one_of(st.booleans(), st.integers(-10**6, 10**6),
                          st.text(alphabet=st.characters(codec="utf-8", exclude_characters="\x00"), max_size=6))
_json_values = st.recursive(
    st.one_of(st.none(), _json_scalars),
    lambda children: st.one_of(st.lists(children, max_size=3),
                               st.dictionaries(_json_keys, children, max_size=3)),
    max_leaves=8,
)


def _set_attributes(world, attributes):
    world.db.rollback()
    variant = world.db.get(ProductVariant, world.variant.id)
    variant.attributes = attributes
    world.db.commit()


@_settings(60)
@given(stored=st.dictionaries(_json_keys, _json_values, max_size=4),
       patch=st.dictionaries(_json_keys, _json_values, min_size=1, max_size=4))
@example(stored={"size": "M"}, patch={"color": "red"})
def test_merge_patch_replaces_a_free_form_object_wholesale(world, stored, patch):
    """POLICY (B6): the API documents "Members present replace the stored value"
    (docs/API.md "JSON Merge Patch", app/api/merge_patch.py PATCH_DESCRIPTION),
    so a free-form member such as the variant `attributes` is replaced as a
    whole - nulls inside it are kept - not merged recursively as RFC 7396
    section 2 would. A deliberate deviation, asserted here as documented."""
    _set_attributes(world, stored)
    response = no_5xx(_patch(world, PATCH_TARGETS[3], {"attributes": patch}))
    assert response.status_code == 200, response.text
    assert response.json()["attributes"] == patch


def test_merge_patch_of_nested_object_replaces_it_as_documented(world):
    """POLICY (B6): RFC 7396 Appendix A would give {"size": "M", "color": "red"}
    and then {"color": "red"}; the documented contract ("Members present replace
    the stored value") gives the replacement object, a null kept as stored."""
    _set_attributes(world, {"size": "M"})
    response = no_5xx(_patch(world, PATCH_TARGETS[3], {"attributes": {"color": "red"}}))
    assert response.status_code == 200, response.text
    assert response.json()["attributes"] == {"color": "red"}
    response = no_5xx(_patch(world, PATCH_TARGETS[3], {"attributes": {"size": None}}))
    assert response.status_code == 200, response.text
    assert response.json()["attributes"] == {"size": None}


def _nested(depth: int, kind: str):
    return ("[" * depth + "]" * depth) if kind == "list" else ('{"a":' * depth + "1" + "}" * depth)


@_settings(40)
@given(depth=st.integers(1, 900), kind=st.sampled_from(["list", "dict"]))
@strict_only(example(depth=300, kind="list"))
def test_deeply_nested_free_form_json_is_never_a_500(world, depth, kind):
    """A free-form member (variant `attributes`) nested arbitrarily deep is
    stored and served (200) or refused (400/422); it never makes the write - or
    any later read - a 500. (Depth >= 200 is BUG_B2, left out unless
    TORTURE_STRICT=1; test_bug_deeply_nested_characteristic_... reproduces it.)"""
    assume(TORTURE_STRICT or depth < 200)
    body = ('{"attributes":{"k":' + _nested(depth, kind) + "}}").encode()
    response = no_5xx(_patch(world, PATCH_TARGETS[3], body))
    assert response.status_code in (200, 400, 422), response.text
    no_5xx(world.api.get(world.variant_url, headers=world.admin))
    no_5xx(world.api.get(f"{world.variant_url.rsplit('/', 1)[0]}", headers=world.admin))


@bug(BUG_B2)
def test_bug_deeply_nested_characteristic_is_a_500_and_breaks_the_public_catalog(world):
    """An admin creating a product whose characteristic value is nested 300 deep
    gets a 500 (pydantic: 'Circular reference detected (depth exceeded)' when
    the response is serialised), yet the row is committed: from then on GET
    /products, /products/search and /products/{id} are a 500 for every visitor."""
    deep = _nested(300, "list")
    body = ('{"name":"Deep product","description":"A product nested deep","price":"1.00",'
            '"stock_quantity":1,"category_id":' + str(world.product.category_id) + ","
            '"images":["https://e.com/x.png"],"characteristics":{"c":{"name":"c","value":'
            + deep + "}}}")
    created = no_5xx(world.api.post(f"{API}/admin/products", content=body.encode(),
                                     headers={**world.admin, "content-type": "application/json"}))
    assert created.status_code in (201, 400, 422), created.text
    listing = no_5xx(world.api.get(f"{API}/products"))
    assert listing.status_code == 200, listing.text
    search = no_5xx(world.api.get(f"{API}/products/search", params={"query": "deep"}))
    assert search.status_code == 200, search.text


@bug(BUG_B3)
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"])
@pytest.mark.parametrize("where", ["variant-attributes", "product-characteristics"])
def test_bug_non_finite_numbers_in_free_form_json(world, literal, where):
    """RFC 8259 section 6: Infinity and NaN are not JSON numbers; 1e400 has no
    double. Python's json module accepts them; on PostgreSQL the JSONB column
    then refuses the value (500), on SQLite it is silently stored as null."""
    if where == "variant-attributes":
        target, body = PATCH_TARGETS[3], '{"attributes":{"w":' + literal + "}}"
    else:
        target = PATCH_TARGETS[2]
        body = '{"characteristics":{"w":{"name":"w","value":' + literal + "}}}"
    response = no_5xx(_patch(world, target, body.encode()))
    assert response.status_code in (400, 422), response.text


# ETags / conditional requests ------------------------------------------------

def _if_match_cases(current: str):
    bare = current.strip('"')
    return [
        ("*", 200),
        (current, 200),
        (f" {current} ", 200),
        (f'"stale", {current}', 200),
        (f'W/"stale",{current}', 200),
        (f"{current}, *", None),  # not RFC grammar: anything but a silent wildcard
        (f"W/{current}", 412),  # weak never matches (strong comparison)
        (bare, 412),  # unquoted
        ('"stale"', 412),
        ('"stale", W/"stale2"', 412),
        ("", 412),
        (" , ,", 412),
        (f'"{bare}x"', 412),
        (f'"a"b{bare}"', 412),
        (current[:-1], 412),
        # Malformed values carrying a "*" somewhere are not "*".
        ("bogus*", 412),
        ('"stale*', 412),
        ("W/*", 412),
        ('"stale", *', 412),
    ]


def _strict_if_match(header: str, current: str):
    """RFC 9110 13.1.1 reference: True/False for a well-formed field, None for
    one that is not ('*' / #entity-tag)."""
    value = header.strip(" \t")
    if value == "*":
        return True
    tags = [item.strip(" \t") for item in value.split(",")]
    tags = [tag for tag in tags if tag]
    entity = re.compile(r'(W/)?"[\x21\x23-\x7e\x80-\xff]*"')
    if not all(entity.fullmatch(tag) for tag in tags):
        return None
    return any(tag == current for tag in tags)


@pytest.mark.parametrize("which", ["product", "variant"])
@pytest.mark.parametrize("index", [
    pytest.param(i, marks=bug(BUG_B4) if i >= 15 else ()) for i in range(19)])
def test_if_match_table(world, which, index):
    """RFC 9110 13.1.1: If-Match passes for "*" or a strongly matching tag of the
    list; a weak tag never matches; otherwise 412 (with the current ETag) and the
    resource is unchanged."""
    etag_of = world.product_etag if which == "product" else world.variant_etag
    target = PATCH_TARGETS[2] if which == "product" else PATCH_TARGETS[3]
    current, before = etag_of()
    header, status = _if_match_cases(current)[index]
    response = no_5xx(_patch(world, target, {"stock_quantity": before["stock_quantity"] + 1},
                             extra_headers={"If-Match": header}))
    if status is None:
        assert response.status_code in (200, 400, 412), response.text
        return
    assert response.status_code == status, (header, response.text)
    if status == 412:
        assert response.headers.get("etag") == current
        _, after = etag_of()
        assert after["stock_quantity"] == before["stock_quantity"]
    else:
        assert response.headers.get("etag") and response.headers["etag"] != current


_etag_tokens = st.sampled_from(["CUR", "W/CUR", '"stale"', 'W/"stale"', "*", "bogus", '"', ",",
                                " ", "W/", '""', "CURRAW", '"x*"'])


@_settings(150)
@given(parts=st.lists(_etag_tokens, min_size=0, max_size=5), sep=st.sampled_from([",", ", ", " ", ""]))
@strict_only(example(parts=['"stale"', "*"], sep=", "))
def test_if_match_only_passes_for_star_or_the_current_strong_tag(world, parts, sep):
    """Safety property of If-Match (lost-update protection): the PATCH applies
    only when the field is "*" or lists the current entity-tag (strong); a
    well-formed list naming it always applies. (A malformed field carrying a
    "*" is BUG_B4, left out unless TORTURE_STRICT=1.)"""
    current, before = world.product_etag()
    header = sep.join(parts).replace("CURRAW", current.strip('"')).replace("CUR", current)
    assume(TORTURE_STRICT or "*" not in header or _strict_if_match(header, current) is not None)
    response = no_5xx(_patch(world, PATCH_TARGETS[2], {"stock_quantity": before["stock_quantity"] + 1},
                             extra_headers={"If-Match": header}))
    assert response.status_code in (200, 412), response.text
    verdict = _strict_if_match(header, current)
    if response.status_code == 200:
        # A field that is not well-formed may still be honoured for the current
        # strong tag it contains; what must never happen is a pass without it.
        names_current = re.search(r"(?<!W/)" + re.escape(current), header) is not None
        assert verdict is True or names_current, (
            f"If-Match {header!r} applied the update without naming the current tag {current}")
    elif verdict is True:
        pytest.fail(f"If-Match {header!r} names the current tag yet got 412")


@bug(BUG_B4)
def test_bug_if_match_wildcard_inside_a_malformed_field_is_honoured(world):
    """app/core/etag.py:_TAG finds a "*" anywhere in the field, so a stale tag
    followed by garbage containing "*" (`"stale", *`, `bogus*`, `W/*`) is read as
    If-Match: * and overwrites a concurrent change (RFC 9110 13.1.1: the field
    is "*" or a list of entity-tags)."""
    _, before = world.product_etag()
    response = no_5xx(_patch(world, PATCH_TARGETS[2], {"stock_quantity": 99},
                             extra_headers={"If-Match": '"stale", *'}))
    assert response.status_code == 412, response.text
    response = no_5xx(world.api.get(f"{API}/products/{world.product.id}",
                                    headers={"If-None-Match": "bogus*"}))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("target", PATCH_TARGETS[:2], ids=["user", "address"])
def test_if_match_is_out_of_scope_on_profile_and_address_patch(world, target):
    """POLICY (B8): docs/API.md scopes optimistic concurrency (If-Match) to the
    product and variant endpoints; /users/me and /users/addresses/{id} emit no
    ETag, so there is no validator to compare and a stale If-Match is ignored."""
    body = {"full_name": "Lost Update"} if target[0] == "user" else {"city": "Lost Update"}
    response = no_5xx(_patch(world, target, body, extra_headers={"If-Match": '"stale-tag"'}))
    assert response.status_code == 200, response.text
    assert "etag" not in response.headers


def test_no_if_match_is_an_unconditional_update_not_428(world):
    """Documented (admin.update_product docstring): without If-Match the update
    is unconditional; 428 (RFC 6585) is not required."""
    response = no_5xx(_patch(world, PATCH_TARGETS[2], {"stock_quantity": 3}))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("which", ["product", "variant"])
@pytest.mark.parametrize("make_header, expected", [
    (lambda c: c, 304),
    (lambda c: f"W/{c}", 304),  # weak comparison
    (lambda c: f'"x", W/"y", {c}', 304),
    (lambda c: "*", 304),
    (lambda c: f'"stale", W/{c}', 304),
    (lambda c: '"stale"', 200),
    (lambda c: c.strip('"'), 200),
    (lambda c: "", 200),
    (lambda c: f'"{c.strip(chr(34))}x"', 200),
], ids=["strong", "weak", "list", "star", "list-weak", "stale", "unquoted", "empty", "prefix"])
def test_if_none_match_on_get(world, which, make_header, expected):
    """RFC 9110 13.1.2: If-None-Match uses weak comparison; a match (or "*" on an
    existing resource) is a 304 with the ETag and no body; otherwise 200. Checked
    twice for the product (database path, then the Redis copy)."""
    if which == "product":
        url, headers = f"{API}/products/{world.product.id}", {}
    else:
        url, headers = world.variant_url, world.admin
    current = observe(world.api.get(url, headers=headers)).headers["etag"]
    for _ in range(2):
        response = observe(world.api.get(url, headers={**headers, "If-None-Match": make_header(current)}))
        assert response.status_code == expected, response.text
        assert response.headers.get("etag") == current
        if expected == 200:
            assert response.json()["id"]


def test_if_none_match_star_on_a_missing_resource_is_404(world):
    response = no_5xx(world.api.get(f"{API}/products/2147483647", headers={"If-None-Match": "*"}))
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 3. Money & pagination boundaries
# ---------------------------------------------------------------------------

# The documented input contract (OpenAPI pattern of NonNegativeMoney, ECMA-262
# regex: \d is ASCII).
_MONEY_DOCUMENTED = re.compile(r"0*[0-9]{1,8}(?:\.[0-9]{1,2}0*)?")
_NEGATIVE_ZERO = re.compile(r"-0+(\.0+)?")  # a sign is let through on purpose (order.py)


def _expected_price(sent):
    if isinstance(sent, str) and (_MONEY_DOCUMENTED.fullmatch(sent) or _NEGATIVE_ZERO.fullmatch(sent)):
        try:
            return str(abs(Decimal(sent)).quantize(Decimal("0.01")))
        except InvalidOperation:
            return None
    return None


_B5_INPUTS = {"１０.５０", "١٠", "\U0001d7cf\U0001d7ce", "১০.৫"}
MONEY_TABLE = [
    "0", "0.00", "-0", "-0.00", "19.99", "19.9", "10.500", "99999999.99", "00000000000001.00",
    "1e-2", "1E+2", "1e2", "1E2", "1e0", " 10.00", "10.00 ", "\t1", "1\n", "0.0000001", "10.999",
    "NaN", "nan", "sNaN", "Infinity", "-Infinity", "inf", "-1", "-0.01", "+1", "1_000", "0x10",
    "1,5", "", ".", ".5", "1.", "100000000", "1e400", "9" * 400,
    "１０.５０", "١٠", "\U0001d7cf\U0001d7ce", "১০.৫",
    1, 10.5, True, [], {}, None,
]


@pytest.mark.parametrize("where", ["product-patch", "product-create", "variant-patch", "variant-create"])
@pytest.mark.parametrize("sent", [
    pytest.param(v, id=repr(v)[:24], marks=bug(BUG_B5) if isinstance(v, str) and v in _B5_INPUTS else ())
    for v in MONEY_TABLE])
def test_money_input_matches_the_documented_contract(world, where, sent):
    """A price is accepted exactly when it is a decimal string the published
    pattern admits (ASCII digits, at most two significant fraction digits, at
    most 8 integer digits) - stored as two decimals - and is otherwise a 422.
    Never a 500, never '-0.00'."""
    field = "price" if where.startswith("product") else "price_override"
    if where == "product-patch":
        response = world.api.patch(world.product_url, json={"price": sent}, headers=world.admin)
    elif where == "variant-patch":
        response = world.api.patch(world.variant_url, json={"price_override": sent}, headers=world.admin)
    elif where == "product-create":
        response = world.api.post(f"{API}/admin/products", headers=world.admin, json={
            "name": "Money thing", "description": "A product about money", "price": sent,
            "stock_quantity": 1, "category_id": world.product.category_id,
            "images": ["https://e.com/m.png"]})
    else:
        response = world.api.post(f"{API}/admin/products/{world.product.id}/variants",
                                  headers=world.admin,
                                  json={"sku": "SKU-" + uuid.uuid4().hex[:8], "price_override": sent})
    no_5xx(response)
    expected = _expected_price(sent)
    if sent is None and where.startswith("variant"):
        assert response.status_code in (200, 201), response.text  # null clears the override
    elif expected is None:
        assert response.status_code == 422, (sent, response.text)
    else:
        assert response.status_code in (200, 201), (sent, response.text)
        assert response.json()[field] == expected


@pytest.mark.parametrize("literal", ["1e400", "-1e400", "NaN", "Infinity", "-Infinity", "1e-400",
                                     "10.50", "1E1", "-0.0", "0"])
def test_money_as_a_raw_json_number_literal_is_422(world, literal):
    """A JSON number (or the non-JSON literals Python's parser lets through) is
    never money: 422, whatever its value, and never a 500 from the quantize."""
    response = no_5xx(world.api.patch(world.product_url, content=('{"price":' + literal + "}").encode(),
                                      headers={**world.admin, "content-type": "application/json"}))
    assert response.status_code == 422, response.text


@_settings(250)
@given(sent=st.one_of(
    st.decimals(allow_nan=True, allow_infinity=True).map(str),
    st.from_regex(r"\A-?[0-9]{0,12}(\.[0-9]{0,5})?([eE][+-]?[0-9]{1,4})?\Z"),
    # Non-ASCII digits are BUG_B5 (test_bug_money_accepts_non_ascii_digits):
    # only generated under TORTURE_STRICT=1.
    st.text(alphabet=st.characters(categories=["Nd", "Zs", "Pd", "Po"],
                                   **({} if TORTURE_STRICT else {"max_codepoint": 127})), max_size=12),
))
@strict_only(example(sent="١٠"))
def test_money_fuzz(world, sent):
    response = no_5xx(world.api.patch(world.product_url, json={"price": sent}, headers=world.admin))
    expected = _expected_price(sent)
    if expected is None:
        assert response.status_code == 422, (sent, response.text)
    else:
        assert response.status_code == 200, (sent, response.text)
        assert response.json()["price"] == expected


@bug(BUG_B5)
@pytest.mark.parametrize("sent", ["１０.５０", "١٠", "\U0001d7cf\U0001d7ce"])
def test_bug_money_accepts_non_ascii_digits(world, sent):
    """app/schemas/order.py:_MONEY_INPUT uses \\d on str, which matches every
    Unicode decimal digit, and Decimal() reads them: '１０.５０' is stored as
    10.50 although the published pattern (ECMA-262, ASCII \\d) refuses it."""
    response = no_5xx(world.api.patch(world.product_url, json={"price": sent}, headers=world.admin))
    assert response.status_code == 422, response.text


@_settings(250)
@given(value=st.one_of(
    st.decimals(allow_nan=False, allow_infinity=False).map(str),
    st.builds(lambda m, e: f"{m}e{e}", st.integers(0, 10**6), st.integers(-10**7, 10**7)),
    st.text(max_size=12),
), param=st.sampled_from(["min_price", "max_price"]))
@strict_only(example(value="1e131072", param="min_price"))
@strict_only(example(value="1e-16384", param="max_price"))
def test_price_filters_never_500(world, value, param):
    # An exponent beyond PostgreSQL numeric is BUG_B9 (PostgreSQL only), left out
    # unless TORTURE_STRICT=1.
    if IS_POSTGRES and not TORTURE_STRICT:
        try:
            number = Decimal(value)
        except InvalidOperation:
            number = None
        assume(number is None or not number.is_finite() or
               (number.adjusted() < 100000 and number.as_tuple().exponent > -10000))
    response = no_5xx(world.api.get(f"{API}/products", params={param: value}))
    assert response.status_code in (200, 422), response.text


@bug(BUG_B9, IS_POSTGRES)
@pytest.mark.parametrize("value", ["1e131072", "1e-16384", "0E-1000000", "1e999999"])
def test_bug_price_filter_beyond_postgres_numeric_is_500(world, value):
    """GET /products?min_price=1e-16384: ProductFilter.min_price/max_price are
    bare Decimal(ge=0) with no digit or exponent bound; PostgreSQL's numeric
    cannot hold the parameter and the query fails (500 'A database error
    occurred'). SQLite answers 200. Even '0E-1000000', which is zero, fails."""
    for param in ("min_price", "max_price"):
        response = no_5xx(world.api.get(f"{API}/products", params={param: value}))
        assert response.status_code in (200, 422), response.text


LIST_ENDPOINTS = [
    ("products", f"{API}/products", None, {}),
    ("search", f"{API}/products/search", None, {"query": "widget"}),
    ("categories", f"{API}/categories", None, {}),
    ("addresses", f"{API}/users/addresses", "buyer", {}),
    ("my-orders", f"{API}/users/orders", "buyer", {}),
    ("admin-orders", f"{API}/admin/orders", "admin", {}),
    ("discrepancies", f"{API}/admin/reconciliation/discrepancies", "admin", {}),
    ("webhook-events", f"{API}/admin/webhook-events", "admin", {}),
]
PAGE_VALUES = [
    (1, 200), (2, 200), (2**31 - 1, 200),
    (0, 422), (-1, 422), (-(2**31), 422), (2**31, 422), (2**63 - 1, 422), (2**63, 422),
    (10**100, 422), ("1.5", 422), ("1e3", 422), ("abc", 422), ("", 422), ("１", 422),
    ("NaN", 422), ("0x10", 422), ("1.0", None), (" 1", None), ("+1", None), ("1 ", None),
]
SIZE_VALUES = [
    (1, 200), (100, 200), (0, 422), (-1, 422), (101, 422), (2**31 - 1, 422), (2**63, 422),
    (10**100, 422), ("2.5", 422), ("x", 422),
]


def _list(world, endpoint, params):
    _, url, auth, base = endpoint
    headers = {"buyer": world.buyer.headers, "admin": world.admin, None: {}}[auth]
    return world.api.get(url, params={**base, **params}, headers=headers)


@pytest.mark.parametrize("endpoint", LIST_ENDPOINTS, ids=[e[0] for e in LIST_ENDPOINTS])
@pytest.mark.parametrize("value, status", PAGE_VALUES, ids=[repr(v)[:20] for v, _ in PAGE_VALUES])
def test_page_bounds(world, endpoint, value, status):
    """page is 1..2**31-1 (Int32Max): beyond it 422, the last page an empty 200;
    the OFFSET it makes ((page-1)*size) must not overflow anything on PostgreSQL."""
    response = no_5xx(_list(world, endpoint, {"page": value, "size": 100}))
    if status is None:
        assert response.status_code in (200, 422)
    else:
        assert response.status_code == status, response.text
    if response.status_code == 200 and value == 2**31 - 1:
        body = response.json()
        assert body["items"] == [] and body["has_more"] is False and body["page"] == 2**31 - 1


@pytest.mark.parametrize("endpoint", LIST_ENDPOINTS, ids=[e[0] for e in LIST_ENDPOINTS])
@pytest.mark.parametrize("value, status", SIZE_VALUES, ids=[repr(v)[:20] for v, _ in SIZE_VALUES])
def test_size_bounds(world, endpoint, value, status):
    response = no_5xx(_list(world, endpoint, {"size": value}))
    assert response.status_code == status, response.text


@_settings(250)
@given(page=st.one_of(st.integers(-(2**70), 2**70), st.text(max_size=8)),
       size=st.one_of(st.integers(-(2**40), 2**40), st.text(max_size=5)),
       endpoint=st.sampled_from(LIST_ENDPOINTS))
@example(page=2**31 - 1, size=100, endpoint=LIST_ENDPOINTS[0])
def test_pagination_fuzz(world, page, size, endpoint):
    response = no_5xx(_list(world, endpoint, {"page": page, "size": size}))
    assert response.status_code in (200, 422), response.text
    if response.status_code == 200:
        body = response.json()
        assert 1 <= body["page"] <= 2**31 - 1 and 1 <= body["size"] <= 100
        assert len(body["items"]) <= body["size"]


ID_ROUTES = [
    ("product", lambda w, i: f"{API}/products/{i}", None),
    ("category", lambda w, i: f"{API}/categories/{i}", None),
    ("address", lambda w, i: f"{API}/users/addresses/{i}", "buyer"),
    ("variant", lambda w, i: f"{API}/admin/products/{w.product.id}/variants/{i}", "admin"),
    ("variant-product", lambda w, i: f"{API}/admin/products/{i}/variants", "admin"),
    ("order", lambda w, i: f"{API}/orders/{i}", "buyer"),
]


@_settings(200)
@given(identifier=st.one_of(st.integers(-(2**80), 2**80), st.sampled_from(
    ["0", "-0", "00001", "1.0", "1e3", "0x1", "%2B1", "１", "9" * 30, "-" + "9" * 30])),
    route=st.sampled_from(ID_ROUTES))
@example(identifier=2**63, route=ID_ROUTES[0])
@example(identifier=-(2**31) - 1, route=ID_ROUTES[2])
def test_ids_in_paths_never_overflow_the_database(world, identifier, route):
    _, url, auth = route
    headers = {"buyer": world.buyer.headers, "admin": world.admin, None: {}}[auth]
    response = no_5xx(world.api.get(url(world, identifier), headers=headers))
    assert response.status_code in (200, 404, 422), response.text


# ---------------------------------------------------------------------------
# 4. RFC 9457 under random input
# ---------------------------------------------------------------------------

BODY_TARGETS = [
    ("register", "post", lambda w: f"{API}/users/register", lambda w: {}),
    ("patch-me", "patch", lambda w: f"{API}/users/me", lambda w: w.buyer.headers),
    ("patch-variant", "patch", lambda w: w.variant_url, lambda w: w.admin),
    ("create-address", "post", lambda w: f"{API}/users/addresses", lambda w: w.buyer.headers),
]


@_settings(250)
@given(body=st.one_of(
    st.binary(max_size=120),
    st.recursive(st.one_of(st.none(), st.booleans(), st.integers(), st.floats(), st.text(max_size=8)),
                 lambda c: st.one_of(st.lists(c, max_size=3), st.dictionaries(st.text(max_size=6), c, max_size=3)),
                 max_leaves=10).map(lambda v: json.dumps(v).encode()),
), target=st.sampled_from(BODY_TARGETS),
    content_type=st.sampled_from(["application/json", MERGE_PATCH, "text/plain"]))
def test_any_body_gets_a_success_or_a_strict_problem(world, body, target, content_type):
    _, method, url, auth = target
    response = no_5xx(world.api.request(method, url(world), content=body,
                                        headers={**auth(world), "content-type": content_type}))
    assert response.status_code in (200, 201, 202, 400, 404, 409, 415, 422), response.text


@pytest.mark.parametrize("method, path", [
    ("get", f"{API}/no-such-route"),
    ("delete", f"{API}/products"),
    ("get", f"{API}/users/me"),
    ("put", f"{API}/admin/products/1"),
    ("get", f"{API}/products/abc"),
])
def test_framework_errors_are_strict_problems(api, method, path):
    response = observe(api.request(method, path))
    assert response.status_code in (401, 404, 405, 422)
