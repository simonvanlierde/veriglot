from dataclasses import replace
from pathlib import Path

import pytest

from veriglot.catalog import load_catalog
from veriglot.review import Kind, Severity, check_catalogs, check_entry, fingerprint

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = FIXTURES / "credit-matrix/en/messages.po"


@pytest.mark.parametrize("locale", ["nl", "ja", "fr"])
def test_real_catalogs_pass(locale: str):
    target = load_catalog(FIXTURES / f"credit-matrix/{locale}/messages.po")
    assert check_catalogs(load_catalog(SOURCE), target) == []


def test_seeded_defects_are_found():
    findings = check_catalogs(load_catalog(SOURCE), load_catalog(FIXTURES / "broken/nl/messages.po"))
    found = {(f.key, f.kind) for f in findings}
    assert found == {
        ("stepContributors", Kind.MISSING),
        ("removedKey", Kind.EXTRA),
        ("cancel", Kind.UNTRANSLATED),
        ("annContributorRestored", Kind.PLACEHOLDER),
        ("importedContributors", Kind.SYNTAX),
        ("normsPosition", Kind.PLURAL),
        ("errImportNoXmlContribs", Kind.TAG),
        ("sampleNames", Kind.WHITESPACE),
        ("output.labelSeparator", Kind.WHITESPACE),
        ("output.levelAnnotation", Kind.SYNTAX),
    }
    by_key = {f.key: f for f in findings}
    assert by_key["output.labelSeparator"].proposed == ": "
    assert by_key["sampleNames"].proposed is None
    assert by_key["cancel"].severity is Severity.INFO
    assert by_key["removedKey"].severity is Severity.WARNING
    assert by_key["importedContributors"].severity is Severity.ERROR
    assert by_key["annContributorRestored"].id == "nl:annContributorRestored:placeholder"
    assert "messages.po" in by_key["cancel"].context


def test_finding_carries_snapshot():
    (f,) = check_entry("nl", "k", "{name} restored.", "{naam} hersteld.")
    assert (f.source, f.target, f.locale) == ("{name} restored.", "{naam} hersteld.", "nl")
    assert f.fingerprint == fingerprint("{name} restored.", "{naam} hersteld.")
    assert "{name}" in f.message
    assert "{naam}" in f.message


def test_vendored_entries_are_marked_read_only():
    target = load_catalog(FIXTURES / "broken/nl/messages.po")
    key = "https://credit.niso.org/contributor-roles/conceptualization/name"
    entry = replace(target.entries[key], text="Conceptualization")
    target = replace(target, entries={**target.entries, key: entry})
    (f,) = [f for f in check_catalogs(load_catalog(SOURCE), target) if f.key == key]
    assert "read-only" in f.context


def kinds(locale: str, source: str, target: str) -> set[tuple[Kind, Severity]]:
    return {(f.kind, f.severity) for f in check_entry(locale, "k", source, target)}


def test_plural_may_become_simple_where_locale_has_only_other():
    source = "Imported {count, plural, one {# contributor} other {# contributors}}."
    assert kinds("ja", source, "{count}名の貢献者をインポートしました。") == set()
    assert kinds("nl", source, "{count} bijdragers geïmporteerd.") == {(Kind.PLURAL, Severity.ERROR)}


def test_plural_categories_follow_cldr():
    source = "{count, plural, one {# item} other {# items}}"
    # French "many" only selects numbers of a million and up, so "other" covers it.
    assert kinds("fr", source, "{count, plural, one {# élément} other {# éléments}}") == set()
    assert kinds("ja", source, "{count, plural, one {#件} other {#件}}") == {(Kind.PLURAL, Severity.WARNING)}
    assert kinds("nl", source, "{count, plural, other {# items}}") == {(Kind.PLURAL, Severity.WARNING)}
    assert kinds("nl", source, "{count, plural, =1 {één item} other {# items}}") == set()
    assert kinds("pl", source, "{count, plural, one {# element} other {# elementów}}") == {
        (Kind.PLURAL, Severity.WARNING)
    }


def test_select_branches_compared_with_source():
    source = "{p, select, single {Sole} first {First} other {Last}}"
    assert kinds("nl", source, "{p, select, single {Enige} other {Laatste}}") == {(Kind.PLURAL, Severity.WARNING)}
    assert kinds("nl", source, "{p, select, single {Enige} first {Eerste}}") == {(Kind.SYNTAX, Severity.ERROR)}


@pytest.mark.parametrize(
    ("locale", "source", "target"),
    [
        ("fr", ": ", " : "),  # French spacing before high punctuation
        ("ja", ": ", "："),  # noqa: RUF001 - full-width punctuation carries its own spacing
        ("ja", ", ", "、"),
        ("nl", "a\nb", "c\nd"),
    ],
)
def test_whitespace_differences_that_are_correct(locale: str, source: str, target: str):
    assert kinds(locale, source, target) == set()


def test_whitespace_proposal_restores_only_the_wrong_side():
    (f,) = check_entry("fr", "k", ": ", " :")
    assert f.kind is Kind.WHITESPACE
    assert f.proposed == " : "
    (f,) = check_entry("nl", "k", "Name", " Naam")
    assert f.proposed == "Naam"


def test_untranslated_ignores_text_without_letters():
    assert kinds("nl", "{label} ({level})", "{label} ({level})") == set()
    assert kinds("nl", "Cancel", "Cancel") == {(Kind.UNTRANSLATED, Severity.INFO)}


@pytest.mark.parametrize("target", ["", " ", "\n\t"])
def test_empty_or_blank_translation_is_missing(target: str):
    assert kinds("nl", "Cancel", target) == {(Kind.MISSING, Severity.ERROR)}
    assert kinds("nl", " ", target or " ") == set()


def test_source_syntax_error_is_reported():
    (f,) = check_entry("nl", "k", "{broken", "{broken")
    assert f.kind is Kind.SYNTAX
    assert "source" in f.message


@pytest.mark.parametrize("key", ["=abc", "=inf", "=nan", "=1e3", "=1.5", "=99999999999999999999999e999"])
def test_non_integer_exact_keys_are_syntax_errors(key: str):
    source = "{n, plural, one {# item} other {# items}}"
    assert kinds("fr", source, f"{{n, plural, {key} {{x}} other {{y}}}}") == {(Kind.SYNTAX, Severity.ERROR)}


@pytest.mark.parametrize(
    "target", ["{n, plural}", "{n, select}", "{n, foo, bar}", "{n, select, a {x}}", "{n, plural, one {#}}"]
)
def test_branchless_unknown_or_otherless_arguments_are_syntax_errors(target: str):
    assert (Kind.SYNTAX, Severity.ERROR) in kinds("ja", "{n}", target)


def test_dropped_hash_or_branch_placeholder_warns():
    source = "{n, plural, one {# item} other {{name} has # items}}"
    dropped_hash = "{n, plural, one {un élément} other {{name} a # éléments}}"
    assert kinds("fr", source, dropped_hash) == {(Kind.PLACEHOLDER, Severity.WARNING)}
    dropped_name = "{n, plural, one {{name} a # élément} other {# éléments}}"
    assert kinds("fr", source, dropped_name) == {(Kind.PLACEHOLDER, Severity.WARNING)}
    # {n} in place of #, and an exact branch without a number where the source has none either.
    exact = "{n, plural, =0 {Geen} one {{n} item} other {{name} heeft {n} items}}"
    assert kinds("nl", "{n, plural, =0 {None} one {# item} other {{name} has # items}}", exact) == set()
    # A name missing from every branch is still an error.
    assert (Kind.PLACEHOLDER, Severity.ERROR) in kinds("fr", source, "{n, plural, one {#} other {#}}")


@pytest.mark.parametrize(
    ("locale", "target"),
    [
        ("ja", "{n, plural, other {<b>#</b> 件}}"),
        ("fr", "{n, plural, one {<b>#</b> élément} many {<b>#</b> d'éléments} other {<b>#</b> éléments}}"),
        ("ru", "{n, plural, one {<b>#</b>} few {<b>#</b>} many {<b>#</b>} other {<b>#</b>}}"),
        ("ja", "<b>{n}</b> 件"),
    ],
)
def test_tags_are_compared_per_branch(locale: str, target: str):
    source = "{n, plural, one {<b>#</b> item} other {<b>#</b> items}}"
    assert kinds(locale, source, target) == set()


def test_tag_dropped_in_one_branch_is_an_error():
    source = "<i>{n, plural, one {<b>#</b> item} other {<b>#</b> items}}</i>"
    assert kinds("fr", source, "<i>{n, plural, one {<b>#</b> élément} other {# éléments}}</i>") == {
        (Kind.TAG, Severity.ERROR)
    }
    assert kinds("fr", source, "{n, plural, one {<b>#</b> élément} other {<b>#</b> éléments}}") == {
        (Kind.TAG, Severity.ERROR)
    }


def test_offset_shifts_exact_keys_before_category_coverage():
    source = "{n, plural, offset:1 =0 {Nobody} =1 {You} one {You and # other} other {You and # others}}"
    # With offset 1, "one" selects n = 2, which neither =0 nor =1 covers.
    lacks_one = "{n, plural, offset:1 =0 {Niemand} =1 {Jij} other {Jij en # anderen}}"
    assert kinds("nl", source, lacks_one) == {(Kind.PLURAL, Severity.WARNING)}
    covered = "{n, plural, offset:1 =0 {Niemand} =1 {Jij} =2 {Jij en één ander} other {Jij en # anderen}}"
    assert kinds("nl", source, covered) == set()


def test_fuzzy_entries_get_an_info_finding(tmp_path: Path):
    path = tmp_path / "messages.po"
    path.write_text(
        'msgid ""\nmsgstr ""\n"Language: nl\\n"\n\n#, fuzzy\nmsgid "cancel"\nmsgstr "Annuleren"\n', encoding="utf-8"
    )
    source = replace(load_catalog(SOURCE), entries={"cancel": load_catalog(SOURCE).entries["cancel"]})
    (f,) = check_catalogs(source, load_catalog(path))
    assert (f.kind, f.severity, f.proposed) == (Kind.FUZZY, Severity.INFO, None)


def test_whitespace_proposal_only_when_it_passes_the_checks():
    (f,) = [f for f in check_entry("nl", "k", "{name} ", "{name}") if f.kind is Kind.WHITESPACE]
    assert f.proposed == "{name} "
    (f,) = [f for f in check_entry("nl", "k", "{name} ", "{naam}") if f.kind is Kind.WHITESPACE]
    assert f.proposed is None
