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
    assert shape.complex["count"].kind == "plural"
    assert set(shape.complex["count"].branches) == {"=0", "one", "other"}
    assert shape.simple == frozenset()


def test_nested_select_keeps_inner_arguments():
    shape = parse("{copied, select, true {Copied. Ask {name} about this: {role}} other {Ask {name}: {role}}}")
    assert shape.names == {"copied", "name", "role"}
    assert shape.complex["copied"].kind == "select"
    assert set(shape.complex["copied"].branches) == {"true", "other"}


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
        "{n, plural}",
        "{n, select}",
        "{n, selectordinal }",
        "{n, foo, bar}",
        "{n, foo}",
        "{n, plural, one {a}}",
        "{n, select, a {x}}",
        "{n, selectordinal, one {#st}}",
        "{n, plural, =abc {x} other {y}}",
        "{n, plural, =inf {x} other {y}}",
        "{n, plural, =nan {x} other {y}}",
        "{n, plural, =1e3 {x} other {y}}",
        "{n, plural, =1.5 {x} other {y}}",
        "{n, plural, =-1 {x} other {y}}",
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


@pytest.mark.parametrize("message", ["{d, date, short}", "{n, number, ::currency/EUR}", "{n, spellout}", "{t, time}"])
def test_simple_types_with_and_without_style(message: str):
    assert parse(message).simple == {message[1]}


def test_branches_record_names_hash_tags_and_offset():
    shape = parse("<b>{n, plural, offset:1 =0 {<i>none</i>} other {{name} and # <0>more</0>}}</b>")
    arg = shape.complex["n"]
    assert (arg.kind, arg.offset) == ("plural", 1)
    zero, other = arg.branches["=0"], arg.branches["other"]
    assert (zero.names, zero.hash, zero.tags) == (frozenset(), False, ("</i>", "<i>"))
    assert (other.names, other.hash, other.tags) == (frozenset({"name"}), True, ("</0>", "<0>"))
    assert shape.tags == ("</b>", "<b>")
    assert shape.names == {"n", "name"}


def test_hash_in_select_nested_in_plural_is_literal():
    shape = parse("{n, plural, other {{g, select, other {# x}}}}")
    assert shape.text == "# x"
    assert not shape.complex["n"].branches["other"].hash
    assert parse("{n, plural, other {{g, select, other {'#'x}}}}").text == "'#'x"
