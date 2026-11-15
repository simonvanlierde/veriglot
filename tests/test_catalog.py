import shutil
from pathlib import Path

import pytest

from veriglot.catalog import CatalogChangedError, is_read_only, load_catalog, write_texts

FIXTURES = Path(__file__).parent / "fixtures"
NL = FIXTURES / "credit-matrix/nl/messages.po"


def test_load_po_keeps_text_exactly():
    catalog = load_catalog(NL)
    assert catalog.locale == "nl"
    assert "" not in catalog.entries
    assert catalog.entries["output.labelSeparator"].text == ": "
    assert catalog.entries["sampleNames"].text == "Christiaan Huygens\nJohanna Westerdijk\nJan Ingenhousz"


def test_locale_from_header_not_folder(tmp_path: Path):
    path = tmp_path / "messages.po"
    shutil.copy(FIXTURES / "credit-matrix/ja/messages.po", path)
    assert load_catalog(path).locale == "ja"


def test_write_changes_only_the_edited_msgstr(tmp_path: Path):
    path = tmp_path / "messages.po"
    shutil.copy(NL, path)
    before = path.read_text(encoding="utf-8").splitlines()
    write_texts(load_catalog(path), {"cancel": 'Annuleer "nu"\n', "sampleNames": "A\nB"})
    after = path.read_text(encoding="utf-8").splitlines()
    changed = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
    assert changed == [
        ('msgstr "Annuleren"', r'msgstr "Annuleer \"nu\"\n"'),
        (r'msgstr "Christiaan Huygens\nJohanna Westerdijk\nJan Ingenhousz"', r'msgstr "A\nB"'),
    ]
    reloaded = load_catalog(path)
    assert reloaded.entries["cancel"].text == 'Annuleer "nu"\n'
    assert reloaded.entries["sampleNames"].text == "A\nB"


def test_write_without_changes_is_byte_identical(tmp_path: Path):
    path = tmp_path / "messages.po"
    shutil.copy(NL, path)
    write_texts(load_catalog(path), {})
    assert path.read_bytes() == NL.read_bytes()


def test_write_replaces_wrapped_msgstr(tmp_path: Path):
    path = tmp_path / "messages.po"
    path.write_text(
        'msgid ""\nmsgstr ""\n"Language: nl\\n"\n\n'
        '#. note\nmsgid "a"\nmsgstr ""\n"one\\n"\n"two"\n\nmsgid "b"\nmsgstr "x"\n',
        encoding="utf-8",
    )
    catalog = load_catalog(path)
    assert catalog.entries["a"].text == "one\ntwo"
    assert "note" in catalog.entries["a"].context
    write_texts(catalog, {"a": "drie"})
    assert path.read_text(encoding="utf-8").endswith('#. note\nmsgid "a"\nmsgstr "drie"\n\nmsgid "b"\nmsgstr "x"\n')


def test_write_refuses_when_file_changed(tmp_path: Path):
    path = tmp_path / "messages.po"
    shutil.copy(NL, path)
    catalog = load_catalog(path)
    path.write_text(path.read_text(encoding="utf-8").replace("Annuleren", "Stoppen"), encoding="utf-8")
    with pytest.raises(CatalogChangedError):
        write_texts(catalog, {"cancel": "Afbreken"})
    assert "Stoppen" in path.read_text(encoding="utf-8")


def test_flat_json_catalog(tmp_path: Path):
    path = tmp_path / "nl.json"
    path.write_text('{"common.pointsShort": "{count} ptn", "common.and": "en"}', encoding="utf-8")
    catalog = load_catalog(path)
    assert catalog.locale == "nl"
    assert catalog.entries["common.pointsShort"].text == "{count} ptn"


def test_vendored_role_keys_are_read_only():
    assert is_read_only("https://credit.niso.org/contributor-roles/software/name", ())
    assert not is_read_only("output.lead", ())
    assert is_read_only("output.lead", ("output.*",))
