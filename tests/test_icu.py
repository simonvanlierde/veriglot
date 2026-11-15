import contextlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from veriglot.icu import IcuSyntaxError, parse


def test_simple_arguments_and_text():
    shape = parse("{label} ({level})")
    assert shape.names == {"label", "level"}
    assert shape.simple == {"label", "level"}
    assert shape.text == " ()"


def test_plural_with_hash_and_exact_keys():
    shape = parse("{count, plural, =0 {No contributors} one {# contributor} other {# contributors}}")
    assert shape.complex == {"count": ("plural", frozenset({"=0", "one", "other"}))}
    assert shape.simple == frozenset()


def test_nested_select_keeps_inner_arguments():
    shape = parse("{copied, select, true {Copied. Ask {name} about this: {role}} other {Ask {name}: {role}}}")
    assert shape.names == {"copied", "name", "role"}
    assert shape.complex["copied"] == ("select", frozenset({"true", "other"}))


def test_offset_and_typed_argument_with_style():
    shape = parse("{n, plural, offset:1 other {{when, date, ::yyyy{MM}} #}}")
    assert shape.names == {"n", "when"}


def test_apostrophes_escape_syntax():
    shape = parse("It''s '{literal}' and l'<0>link</0>")
    assert shape.names == frozenset()
    assert shape.text == "It's {literal} and l'<0>link</0>"
    assert shape.tags == ("</0>", "<0>")


def test_quoted_tag_is_text():
    assert parse("'{<b>}'").tags == ()


def test_tags_are_kept_not_parsed():
    # Strict ICU parsers reject this as an unclosed tag.
    shape = parse("No <contrib> elements found in the XML.")
    assert shape.tags == ("<contrib>",)


@pytest.mark.parametrize(
    "message",
    [
        "{label ({level})",
        "{count, plural, one {#}",
        "text }",
        "{}",
        "{count, plural, one {a} one {b}}",
        "{count, plural, }",
        "{count, plural one {a}}",
        "{n, plural, offset: other {a}}",
    ],
)
def test_syntax_errors(message: str):
    with pytest.raises(IcuSyntaxError):
        parse(message)


def _quote(text: str) -> str:
    """Escape arbitrary text as one ICU literal: quote from the first syntax character on."""
    cut = next((i for i, c in enumerate(text) if c in "{}"), len(text))
    head, rest = text[:cut].replace("'", "''"), text[cut:].replace("'", "''")
    return f"{head}'{rest}'" if rest else head


@given(st.text())
def test_quoted_text_round_trips(text: str):
    shape = parse(_quote(text))
    assert shape.text == text
    assert shape.names == frozenset()


@given(st.text(alphabet="{}#'<>, abcpluralselectother=1"))
def test_only_syntax_errors_escape(text: str):
    with contextlib.suppress(IcuSyntaxError):
        parse(text)
