import shutil
from pathlib import Path

import pytest

from veriglot.apply import ApplyReport, DecisionRow, apply_decisions
from veriglot.catalog import load_catalog
from veriglot.review import check_catalogs, fingerprint

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = load_catalog(FIXTURES / "credit-matrix/en/messages.po")
ROLE = "https://credit.niso.org/contributor-roles/conceptualization/name"


@pytest.fixture
def target(tmp_path: Path) -> Path:
    path = tmp_path / "nl" / "messages.po"
    path.parent.mkdir()
    shutil.copy(FIXTURES / "broken/nl/messages.po", path)
    return path


def row(
    key: str,
    kind: str,
    source: str,
    target: str,
    decision: str,
    *,
    proposed: str | None = None,
    edited: str | None = None,
) -> DecisionRow:
    return DecisionRow(
        id=f"nl:{key}:{kind}",
        fingerprint=fingerprint(source, target),
        decision=decision,
        proposed=proposed,
        edited=edited,
    )


def statuses(report: ApplyReport) -> list[tuple[str, str, str]]:
    return [(o.id.split(":", 1)[1], o.status, o.reason) for o in report.outcomes]


def test_accept_edit_and_every_rejection_are_accounted(target: Path):
    before = target.read_text(encoding="utf-8")
    rows = [
        row("output.labelSeparator", "whitespace", ": ", ":", "accept", proposed=": "),
        row("cancel", "untranslated", "Cancel", "Cancel", "edit", edited="Annuleren"),
        row("annContributorRestored", "placeholder", "{name} restored.", "{naam} hersteld.", "edit", edited="{nm}"),
        row("sampleNames", "whitespace", "x", "y", "edit", edited="z"),
        DecisionRow(id="nl:noSuchKey:missing", fingerprint="", decision="edit", edited="x"),
        DecisionRow(id="de:cancel:untranslated", fingerprint="", decision="edit", edited="x"),
        row(ROLE, "untranslated", "Conceptualization", "Conceptualisatie", "edit", edited="Concept"),
        row("errImportNoXmlContribs", "tag", "", "", "reject"),
        row("output.levelAnnotation", "syntax", "", "", ""),
        row("removedKey", "extra", "", "Verwijderd", "maybe"),
        row("errImportNoXmlContribs", "tag", "", "", "accept"),
    ]
    report = apply_decisions(SOURCE, load_catalog(target), rows)
    assert statuses(report) == [
        ("output.labelSeparator:whitespace", "applied", ""),
        ("cancel:untranslated", "applied", ""),
        ("annContributorRestored:placeholder", "rejected", "invalid"),
        ("sampleNames:whitespace", "rejected", "stale"),
        ("noSuchKey:missing", "rejected", "unknown"),
        ("cancel:untranslated", "rejected", "unknown"),
        (f"{ROLE}:untranslated", "rejected", "read-only"),
        ("errImportNoXmlContribs:tag", "skipped", "declined"),
        ("output.levelAnnotation:syntax", "skipped", "undecided"),
        ("removedKey:extra", "rejected", "invalid decision"),
        ("errImportNoXmlContribs:tag", "rejected", "invalid"),
    ]
    assert report.written
    assert all(o.detail for o in report.outcomes if o.status == "rejected")
    after = target.read_text(encoding="utf-8")
    assert after.replace('msgstr ": "', 'msgstr ":"').replace('"Annuleren"', '"Cancel"') == before
    assert check_catalogs(SOURCE, load_catalog(target))  # other defects remain


def test_second_decision_on_same_entry_is_stale(target: Path):
    rows = [
        row("cancel", "untranslated", "Cancel", "Cancel", "edit", edited="Annuleren"),
        row("cancel", "untranslated", "Cancel", "Cancel", "edit", edited="Afbreken"),
    ]
    report = apply_decisions(SOURCE, load_catalog(target), rows)
    assert [o.reason for o in report.outcomes] == ["", "stale"]


def test_rerun_after_apply_reports_already_applied(target: Path):
    rows = [row("cancel", "untranslated", "Cancel", "Cancel", "edit", edited="Annuleren")]
    apply_decisions(SOURCE, load_catalog(target), rows)
    again = apply_decisions(SOURCE, load_catalog(target), rows)
    assert statuses(again) == [("cancel:untranslated", "skipped", "already applied")]
    assert not again.written


def test_dry_run_writes_nothing(target: Path):
    before = target.read_bytes()
    rows = [row("cancel", "untranslated", "Cancel", "Cancel", "edit", edited="Annuleren")]
    report = apply_decisions(SOURCE, load_catalog(target), rows, dry_run=True)
    assert report.applied
    assert not report.written
    assert target.read_bytes() == before


def test_json_catalogs_are_check_only(tmp_path: Path):
    path = tmp_path / "nl.json"
    path.write_text('{"cancel": "Cancel"}', encoding="utf-8")
    source = tmp_path / "en.json"
    source.write_text('{"cancel": "Cancel"}', encoding="utf-8")
    rows = [row("cancel", "untranslated", "Cancel", "Cancel", "edit", edited="Annuleren")]
    report = apply_decisions(load_catalog(source), load_catalog(path), rows)
    assert statuses(report) == [("cancel:untranslated", "rejected", "unsupported")]
