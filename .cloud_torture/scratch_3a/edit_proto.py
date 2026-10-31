import re
p="tests/torture_suite/test_protocol_rfc_torture.py"
s=open(p).read()
def rep(old,new,count=1):
    global s
    assert s.count(old)==count,(old,s.count(old))
    s=s.replace(old,new)

rep("from conftest import scaled_examples\n","from conftest import IS_POSTGRES, scaled_examples\n")
# B1
rep('''@_settings(80)
@given(local=_local_parts, domain=st.sampled_from(DOMAINS))
@example(local="jane", domain="xn--mnchen-3ya.de")
def test_an_account_logs_in_with_the_exact_email_it_registered_with(api, local, domain):
    """Round trip: whatever spelling of an address registration accepts, login
    with that very same string finds the account."""
''','''# Domains whose registered (IDNA/UTS #46) form differs from what login looks up:
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
''')
rep('''@pytest.mark.parametrize("email", [
    pytest.param("jane@xn--mnchen-3ya.de", id="idna-ace-domain"),
    pytest.param("jane@ｅｘａｍｐｌｅ.com", id="fullwidth-domain"),
    pytest.param("jane@example。com", id="ideographic-full-stop"),
])''','''@pytest.mark.parametrize("email", [
    pytest.param("jane@xn--mnchen-3ya.de", id="idna-ace-domain", marks=bug(BUG_B1)),
    pytest.param("jane@ｅｘａｍｐｌｅ.com", id="fullwidth-domain", marks=bug(BUG_B1)),
    pytest.param("jane@example。com", id="ideographic-full-stop", marks=bug(BUG_B1)),
])''')
# B7
rep('''@pytest.mark.parametrize("target", PATCH_TARGETS, ids=[t[0] for t in PATCH_TARGETS])
def test_bug_json_patch_media_type''','''@bug(BUG_B7)
@pytest.mark.parametrize("target", PATCH_TARGETS, ids=[t[0] for t in PATCH_TARGETS])
def test_bug_json_patch_media_type''')
# B6 policy
rep('''@example(stored={"size": "M"}, patch={"color": "red"})
def test_merge_patch_of_a_free_form_object_follows_rfc7396(world, stored, patch):
    """RFC 7396 section 2 is recursive: a member that is an object is merged into
    the stored object, null removes a member at any depth, absent members stay.
    Checked on the free-form `attributes` of a variant."""
    _set_attributes(world, stored)
    response = no_5xx(_patch(world, PATCH_TARGETS[3], {"attributes": patch}))
    assert response.status_code == 200, response.text
    assert response.json()["attributes"] == rfc7396_merge(stored, patch)
''','''@example(stored={"size": "M"}, patch={"color": "red"})
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
''')
rep('''def test_bug_merge_patch_replaces_nested_objects_instead_of_merging(world):
    """RFC 7396 section 2 / Appendix A: {"attributes": {"color": "red"}} on
    {"size": "M"} gives {"size": "M", "color": "red"}; {"attributes":
    {"size": null}} removes "size". The API replaces the whole object and
    stores the null."""
    _set_attributes(world, {"size": "M"})
    response = no_5xx(_patch(world, PATCH_TARGETS[3], {"attributes": {"color": "red"}}))
    assert response.status_code == 200, response.text
    assert response.json()["attributes"] == {"size": "M", "color": "red"}
    response = no_5xx(_patch(world, PATCH_TARGETS[3], {"attributes": {"size": None}}))
    assert response.status_code == 200, response.text
    assert response.json()["attributes"] == {"color": "red"}
''','''def test_merge_patch_of_nested_object_replaces_it_as_documented(world):
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
''')
# B2
rep('''def test_deeply_nested_free_form_json_is_never_a_500(world, depth, kind):
    """A free-form member (variant `attributes`) nested arbitrarily deep is
    stored and served (200) or refused (400/422); it never makes the write - or
    any later read - a 500."""
''','''@strict_only(example(depth=300, kind="list"))
def test_deeply_nested_free_form_json_is_never_a_500(world, depth, kind):
    """A free-form member (variant `attributes`) nested arbitrarily deep is
    stored and served (200) or refused (400/422); it never makes the write - or
    any later read - a 500. (Depth >= 200 is BUG_B2, left out unless
    TORTURE_STRICT=1; test_bug_deeply_nested_characteristic_... reproduces it.)"""
    assume(TORTURE_STRICT or depth < 200)
''')
rep("def test_bug_deeply_nested_characteristic","@bug(BUG_B2)\ndef test_bug_deeply_nested_characteristic")
# B3
rep('''@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"])
@pytest.mark.parametrize("where", ["variant-attributes", "product-characteristics"])
def test_bug_non_finite''','''@bug(BUG_B3)
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"])
@pytest.mark.parametrize("where", ["variant-attributes", "product-characteristics"])
def test_bug_non_finite''')
# B4
rep('''@pytest.mark.parametrize("index", range(19))''','''@pytest.mark.parametrize("index", [
    pytest.param(i, marks=bug(BUG_B4) if i >= 15 else ()) for i in range(19)])''')
rep('''def test_if_match_only_passes_for_star_or_the_current_strong_tag(world, parts, sep):
    """Safety property of If-Match (lost-update protection): the PATCH applies
    only when the field is "*" or lists the current entity-tag (strong); a
    well-formed list naming it always applies."""
    current, before = world.product_etag()
    header = sep.join(parts).replace("CURRAW", current.strip('"')).replace("CUR", current)
''','''@strict_only(example(parts=['"stale"', "*"], sep=", "))
def test_if_match_only_passes_for_star_or_the_current_strong_tag(world, parts, sep):
    """Safety property of If-Match (lost-update protection): the PATCH applies
    only when the field is "*" or lists the current entity-tag (strong); a
    well-formed list naming it always applies. (A malformed field carrying a
    "*" is BUG_B4, left out unless TORTURE_STRICT=1.)"""
    current, before = world.product_etag()
    header = sep.join(parts).replace("CURRAW", current.strip('"')).replace("CUR", current)
    assume(TORTURE_STRICT or "*" not in header or _strict_if_match(header, current) is not None)
''')
rep('''def test_bug_if_match_wildcard_inside''','''@bug(BUG_B4)
def test_bug_if_match_wildcard_inside''')
# B8 policy
rep('''@pytest.mark.parametrize("target", PATCH_TARGETS[:2], ids=["user", "address"])
def test_bug_if_match_is_ignored_on_profile_and_address_patch(world, target):
    """RFC 9110 13.1.1: an origin server that receives If-Match MUST evaluate it
    before performing the method. PATCH /users/me and /users/addresses/{id}
    ignore it: a stale tag still overwrites."""
    body = {"full_name": "Lost Update"} if target[0] == "user" else {"city": "Lost Update"}
    response = no_5xx(_patch(world, target, body, extra_headers={"If-Match": '"stale-tag"'}))
    assert response.status_code == 412, response.text
''','''@pytest.mark.parametrize("target", PATCH_TARGETS[:2], ids=["user", "address"])
def test_if_match_is_out_of_scope_on_profile_and_address_patch(world, target):
    """POLICY (B8): docs/API.md scopes optimistic concurrency (If-Match) to the
    product and variant endpoints; /users/me and /users/addresses/{id} emit no
    ETag, so there is no validator to compare and a stale If-Match is ignored."""
    body = {"full_name": "Lost Update"} if target[0] == "user" else {"city": "Lost Update"}
    response = no_5xx(_patch(world, target, body, extra_headers={"If-Match": '"stale-tag"'}))
    assert response.status_code == 200, response.text
    assert "etag" not in response.headers
''')
# B5
rep('''MONEY_TABLE = [''','''_B5_INPUTS = {"１０.５０", "١٠", "\\U0001d7cf\\U0001d7ce", "১০.৫"}
MONEY_TABLE = [''')
rep('''@pytest.mark.parametrize("sent", MONEY_TABLE, ids=[repr(v)[:24] for v in MONEY_TABLE])''','''@pytest.mark.parametrize("sent", [
    pytest.param(v, id=repr(v)[:24], marks=bug(BUG_B5) if isinstance(v, str) and v in _B5_INPUTS else ())
    for v in MONEY_TABLE])''')
rep('''    st.text(alphabet=st.characters(categories=["Nd", "Zs", "Pd", "Po"]), max_size=12),
))
def test_money_fuzz''','''    # Non-ASCII digits are BUG_B5 (test_bug_money_accepts_non_ascii_digits):
    # only generated under TORTURE_STRICT=1.
    st.text(alphabet=st.characters(categories=["Nd", "Zs", "Pd", "Po"],
                                   **({} if TORTURE_STRICT else {"max_codepoint": 127})), max_size=12),
))
@strict_only(example(sent="١٠"))
def test_money_fuzz''')
rep('''@pytest.mark.parametrize("sent", ["１０.５０", "١٠", "\\U0001d7cf\\U0001d7ce"])
def test_bug_money''','''@bug(BUG_B5)
@pytest.mark.parametrize("sent", ["１０.５０", "١٠", "\\U0001d7cf\\U0001d7ce"])
def test_bug_money''')
# B9
rep('''@example(value="1e131072", param="min_price")
@example(value="1e-16384", param="max_price")
def test_price_filters_never_500(world, value, param):
''','''@strict_only(example(value="1e131072", param="min_price"))
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
''')
rep('''@pytest.mark.parametrize("value", ["1e131072", "1e-16384", "0E-1000000", "1e999999"])
def test_bug_price_filter''','''@bug(BUG_B9, IS_POSTGRES)
@pytest.mark.parametrize("value", ["1e131072", "1e-16384", "0E-1000000", "1e999999"])
def test_bug_price_filter''')
open(p,"w").write(s)
