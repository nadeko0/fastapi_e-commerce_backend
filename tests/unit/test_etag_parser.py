"""The If-Match / If-None-Match parser walks the header once.

CodeQL flagged the regular expression that used to validate the whole header
(py/polynomial-redos). The parser now finds the entity-tags and checks only the
text between them. It has to accept and reject exactly what that expression did,
so the expression stays here as the reference.
"""
import re
import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.etag import _parse

_OLD_LIST = re.compile(r'[ \t,]*(?:W/)?"[^"]*"(?:[ \t]*,[ \t,]*(?:W/)?"[^"]*")*[ \t,]*')
_OLD_ENTITY = re.compile(r'(W/)?"([^"]*)"')


def _old_parse(header: str) -> list[tuple[bool, str | None]]:
    value = header.strip(" \t")
    if value == "*":
        return [(False, None)]
    if not _OLD_LIST.fullmatch(value):
        return []
    return [(bool(m.group(1)), m.group(2)) for m in _OLD_ENTITY.finditer(value)]


# Built from the pieces the grammar is made of, so that most examples come close
# to a valid header instead of being rejected at the first character.
_PIECES = st.sampled_from(['"', '"a"', '"a,b"', 'W/"a"', 'W/', "w/", ",", " ", "\t", "*", "x", '""'])


@given(st.lists(_PIECES, max_size=12).map("".join))
def test_the_parser_agrees_with_the_old_expression_on_near_valid_headers(header):
    assert _parse(header) == _old_parse(header)


@given(st.text(alphabet=' \t,"W/*xa', max_size=40))
def test_the_parser_agrees_with_the_old_expression_on_arbitrary_text(header):
    assert _parse(header) == _old_parse(header)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ('"a"', [(False, "a")]),
        ('W/"a"', [(True, "a")]),
        ('"a", W/"b"', [(False, "a"), (True, "b")]),
        (', ,"a",, "b" ,', [(False, "a"), (False, "b")]),
        ('"a,b"', [(False, "a,b")]),
        ("*", [(False, None)]),
        (' \t* ', [(False, None)]),
        ("", []),
        (",", []),
        ('"a" "b"', []),
        ('"a""b"', []),
        ('"a"x', []),
        ('x"a"', []),
        ('w/"a"', []),
        ('"a', []),
        ('"a", "b', []),
        ('*, "a"', []),
    ],
)
def test_headers_with_a_known_meaning(header, expected):
    assert _parse(header) == expected


@pytest.mark.timing_sensitive
@pytest.mark.parametrize(
    "header",
    [
        '"a"' + "\t" * 200_000 + "x",
        '"a"' + "\t," * 100_000 + "x",
        "\t," * 100_000 + "x",
        '"a",' * 50_000 + "x",
        ('"a"' + "\t" * 50 + ",") * 4_000 + "x",
        '"' * 200_001,
    ],
    ids=["tabs", "tab-comma", "leading-separators", "many-tags", "tabs-between-tags", "only-quotes"],
)
def test_a_hostile_header_far_over_the_server_limit_is_refused_quickly(header):
    # About three times what uvicorn accepts for all headers together. The bound
    # is generous on purpose: quadratic work on this length takes minutes.
    started = time.perf_counter()
    _parse(header)
    assert time.perf_counter() - started < 1.0
