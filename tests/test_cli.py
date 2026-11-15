import codecs
import csv
import io
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from veriglot.cli import main
from veriglot.csvio import COLUMNS, read_decisions, write_findings
from veriglot.review import check_entry

FIXTURES = Path(__file__).parent / "fixtures"
EN = str(FIXTURES / "credit-matrix/en/messages.po")
BROKEN = str(FIXTURES / "broken/nl/messages.po")


def test_check_valid_catalogs_exit_zero(capsys: pytest.CaptureFixture[str]):
    targets = [str(FIXTURES / f"credit-matrix/{loc}/messages.po") for loc in ("en", "nl", "ja", "fr")]
    assert main(["check", EN, *targets]) == 0
    assert capsys.readouterr().out == ""


def test_check_broken_catalog_text(capsys: pytest.CaptureFixture[str]):
    assert main(["check", EN, BROKEN]) == 1
    out, err = capsys.readouterr()
    assert "error: nl annContributorRestored [placeholder]" in out
    assert "target: '{naam} hersteld.'" in out
    assert "10 findings" in err


def test_check_json_is_stable(capsys: pytest.CaptureFixture[str]):
    assert main(["check", EN, BROKEN, "--format", "json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1
    assert data["errors"] == []
    assert set(data["findings"][0]) == set(COLUMNS) - {"decision", "edited", "note"}


def test_check_csv_then_apply_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target = tmp_path / "messages.po"
    shutil.copy(BROKEN, target)
    assert main(["check", EN, str(target), "--format", "csv"]) == 1
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert list(rows[0]) == list(COLUMNS)
    for r in rows:
        if r["kind"] == "whitespace" and r["proposed"]:
            r["decision"] = "accept"
        if r["kind"] == "untranslated":
            r["decision"], r["edited"] = "edit", "Annuleren"
    decisions = tmp_path / "decisions.csv"
    with decisions.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    assert main(["apply", str(target), str(decisions), "--source", EN, "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 1
    assert sorted(o["status"] for o in report["outcomes"]).count("applied") == 2
    assert report["written"] is True
    assert 'msgstr "Annuleren"' in target.read_text(encoding="utf-8")


def test_apply_text_reports_rejections(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target = tmp_path / "messages.po"
    shutil.copy(BROKEN, target)
    decisions = tmp_path / "d.csv"
    decisions.write_text("id,fingerprint,decision,edited\nnl:gone:missing,abc,edit,x\n", encoding="utf-8")
    assert main(["apply", str(target), str(decisions), "--source", EN, "--dry-run"]) == 1
    out = capsys.readouterr().out
    assert "rejected nl:gone:missing: unknown" in out
    assert "dry run" in out


def test_missing_file_exits_three(capsys: pytest.CaptureFixture[str]):
    assert main(["check", EN, "nope.po"]) == 3
    assert capsys.readouterr().err.startswith("error: ")


def test_missing_file_json_still_prints_object(capsys: pytest.CaptureFixture[str]):
    assert main(["check", EN, "nope.po", "--format", "json"]) == 3
    data = json.loads(capsys.readouterr().out)
    assert data["errors"][0]["message"]


def test_decisions_without_required_columns_exit_three(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    decisions = tmp_path / "d.csv"
    decisions.write_text("id,decision\n", encoding="utf-8")
    assert main(["apply", BROKEN, str(decisions), "--source", EN, "--dry-run"]) == 3
    assert "fingerprint" in capsys.readouterr().err


def test_usage_error_exits_two():
    with pytest.raises(SystemExit) as exc:
        main(["check", EN, BROKEN, "--format", "xml"])
    assert exc.value.code == 2


def test_console_script():
    result = subprocess.run(
        [sys.executable, "-m", "veriglot", "--version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert result.stdout.startswith("veriglot ")


def test_bom_catalog_header_does_not_hide_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target = tmp_path / "messages.po"
    target.write_bytes(codecs.BOM_UTF8 + Path(BROKEN).read_bytes())
    assert main(["check", EN, str(target), "--format", "json"]) == 1
    found = {(f["key"], f["kind"]) for f in json.loads(capsys.readouterr().out)["findings"]}
    assert ("annContributorRestored", "placeholder") in found


def test_malformed_catalog_exits_three(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target = tmp_path / "messages.po"
    target.write_text('msgid ""\nmsgstr ""\n"Language: nl\\n"\n\nmsgid "cancel"\nmsgstr "Annuleren\n', encoding="utf-8")
    assert main(["check", EN, str(target)]) == 3
    assert f"{target}:6:" in capsys.readouterr().err


def test_apply_reads_bom_csv_from_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "messages.po"
    shutil.copy(BROKEN, target)
    sheet = "id,fingerprint,decision,edited\r\nnl:cancel:untranslated,x,reject,\r\n"
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(codecs.BOM_UTF8 + sheet.encode())))
    assert main(["apply", str(target), "-", "--source", EN]) == 0


def test_formula_cells_are_neutralised_and_round_trip():
    texts = ["=HYPERLINK(1)", "+1", "-x", "@sum", "\tx", "\rx", "'=x", "''+x", "'s-Hertogenbosch", "plain"]
    findings = [check_entry("nl", f"k{i}", "Cancel", "Cancel")[0] for i in range(len(texts))]
    findings = [replace(f, target=t, message=t, proposed=t) for f, t in zip(findings, texts, strict=True)]
    out = io.StringIO()
    write_findings(findings, out)
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert [r["target"] for r in rows[:3]] == ["'=HYPERLINK(1)", "'+1", "'-x"]
    assert rows[6]["target"] == "''=x"
    assert rows[8]["target"] == "'s-Hertogenbosch"
    for r in rows:
        r["decision"], r["edited"] = "edit", r["target"]
    back = io.StringIO()
    writer = csv.DictWriter(back, COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    decisions = read_decisions(io.StringIO(back.getvalue()))
    assert [d.edited for d in decisions] == texts
    assert [d.proposed for d in decisions] == texts
