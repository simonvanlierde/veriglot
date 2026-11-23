import codecs
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

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


HEAD = 'msgid ""\nmsgstr ""\n"Language: nl\\n"\n\n'


def _po(tmp_path: Path, body: str, *, prefix: bytes = b"") -> Path:
    path = tmp_path / "messages.po"
    path.write_bytes(prefix + (HEAD + body).encode())
    return path


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x0b", "\x0c", "\x1c", "\x1e", "\x85"])
def test_write_counts_only_newlines_as_line_breaks(tmp_path: Path, separator: str):
    z = separator.join("1234")
    path = _po(tmp_path, f'msgid "z"\nmsgstr "{z}"\n\nmsgid "a"\nmsgstr "A"\n\nmsgid "b"\nmsgstr "B"\n')
    write_texts(load_catalog(path), {"b": "B2"})
    assert {k: e.text for k, e in load_catalog(path).entries.items()} == {"z": z, "a": "A", "b": "B2"}


def test_write_refuses_a_block_that_is_not_the_entry(tmp_path: Path):
    path = _po(tmp_path, 'msgid "a"\nmsgstr "A"\n\nmsgid "b"\nmsgstr "B"\n')
    catalog = load_catalog(path)
    wrong = replace(catalog.entries["b"], line=catalog.entries["a"].line)
    with pytest.raises(ValueError, match="'b'"):
        write_texts(replace(catalog, entries={**catalog.entries, "b": wrong}), {"b": "B2"})
    assert load_catalog(path).entries["a"].text == "A"


@given(st.text())
@example("\x1e\x0c")
def test_written_text_reads_back_and_later_writes_land(text: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = _po(Path(tmp), 'msgid "z"\nmsgstr "Z"\n\nmsgid "a"\nmsgstr "A"\n\nmsgid "b"\nmsgstr "B"\n')
        write_texts(load_catalog(path), {"z": text})
        write_texts(load_catalog(path), {"b": "B2"})
        assert {k: e.text for k, e in load_catalog(path).entries.items()} == {"z": text, "a": "A", "b": "B2"}


def test_bom_is_not_part_of_the_header_and_survives_a_write(tmp_path: Path):
    path = _po(tmp_path, 'msgid "a"\nmsgstr "{naam}"\n', prefix=codecs.BOM_UTF8)
    catalog = load_catalog(path)
    assert catalog.entries["a"].text == "{naam}"
    assert catalog.locale == "nl"
    write_texts(catalog, {"a": "{name}"})
    assert path.read_bytes().startswith(codecs.BOM_UTF8)
    assert load_catalog(path).entries["a"].text == "{name}"


@pytest.mark.parametrize(
    ("body", "line"),
    [
        ('msgid "a"\nmsgstr "unclosed\n', 6),
        ('msgid "a"\nmsgstr "x"\n"tail\n', 7),
        ('msgid "a"\nmsgstr "x" y\n', 6),
        ('msgid "a"\nmsgstr "x"\nmsgtypo "y"\n', 7),
        ('msgid "a"\n', 5),
    ],
)
def test_malformed_po_is_refused_with_its_line(tmp_path: Path, body: str, line: int):
    with pytest.raises(ValueError, match=rf"messages\.po:{line}:"):
        load_catalog(_po(tmp_path, body))


def test_write_clears_fuzzy_from_the_written_entry_only(tmp_path: Path):
    path = _po(
        tmp_path,
        '#, fuzzy\nmsgid "a"\nmsgstr "A"\n\n'
        '#. note\n#, fuzzy, javascript-format\nmsgctxt "c"\nmsgid "b"\nmsgstr "B"\n\n'
        '#, fuzzy\nmsgid "c"\nmsgstr "C"\n',
    )
    catalog = load_catalog(path)
    assert [e.fuzzy for e in catalog.entries.values()] == [True, True, True]
    write_texts(catalog, {"a": "A2", "b": "B2"})
    assert path.read_text(encoding="utf-8") == HEAD + (
        'msgid "a"\nmsgstr "A2"\n\n#. note\n#, javascript-format\nmsgctxt "c"\nmsgid "b"\nmsgstr "B2"\n\n'
        '#, fuzzy\nmsgid "c"\nmsgstr "C"\n'
    )


def test_failed_write_leaves_the_file_and_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "messages.po"
    shutil.copy(NL, path)
    path.chmod(0o640)

    def fail(*_: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        write_texts(load_catalog(path), {"cancel": "Afbreken"})
    assert path.read_bytes() == NL.read_bytes()
    assert [p.name for p in tmp_path.iterdir()] == ["messages.po"]
    monkeypatch.undo()
    write_texts(load_catalog(path), {"cancel": "Afbreken"})
    assert path.stat().st_mode & 0o777 == 0o640


def test_write_inserts_a_missing_key_from_the_source_block(tmp_path: Path):
    source = tmp_path / "en.po"
    source.write_text(
        'msgid ""\nmsgstr ""\n"Language: en\\n"\n\n'
        '# translator note in en\n#. extracted\n#: src/App.tsx:3\n#, fuzzy, icu-format\n#| msgid "old"\n'
        'msgctxt "menu"\nmsgid ""\n"long "\n"key"\nmsgstr "Long key"\n',
        encoding="utf-8",
    )
    path = tmp_path / "nl.po"
    head = codecs.BOM_UTF8 + b'msgid ""\r\nmsgstr ""\r\n"Language: nl\\n"\r\n\r\nmsgid "a"\r\nmsgstr "b"'
    path.write_bytes(head)
    write_texts(load_catalog(path), {"long key": 'Lange "sleutel"'}, source=load_catalog(source))
    assert path.read_bytes() == head + (
        b'\r\n\r\n#. extracted\r\n#: src/App.tsx:3\r\n#, icu-format\r\nmsgctxt "menu"\r\n'
        b'msgid ""\r\n"long "\r\n"key"\r\nmsgstr "Lange \\"sleutel\\""\r\n'
    )
    entry = load_catalog(path).entries["long key"]
    assert (entry.text, entry.fuzzy) == ('Lange "sleutel"', False)
    assert entry.context.startswith("msgctxt: menu")


def test_write_inserts_a_missing_key_without_a_po_source(tmp_path: Path):
    path = tmp_path / "nl.po"
    path.write_text('msgid ""\nmsgstr ""\n"Language: nl\\n"\n', encoding="utf-8")
    source = tmp_path / "en.json"
    source.write_text('{"a\\nb": "A"}', encoding="utf-8")
    write_texts(load_catalog(path), {"a\nb": "Een"}, source=load_catalog(source))
    assert path.read_text(encoding="utf-8").endswith('\n\nmsgid "a\\nb"\nmsgstr "Een"\n')


def test_write_refuses_a_missing_key_without_its_source(tmp_path: Path):
    path = tmp_path / "messages.po"
    shutil.copy(NL, path)
    with pytest.raises(ValueError, match="noSuchKey"):
        write_texts(load_catalog(path), {"noSuchKey": "x"})
    assert path.read_bytes() == NL.read_bytes()


def test_insert_classifies_indented_comments(tmp_path: Path):
    source = tmp_path / "en.po"
    source.write_text(
        'msgid ""\nmsgstr ""\n"Language: en\\n"\n\n'
        ' # translator-only note\n #. extracted note\n  #: src/App.tsx:3\n #, fuzzy\n #| msgid "old"\n'
        'msgid "k"\nmsgstr "K"\n',
        encoding="utf-8",
    )
    path = tmp_path / "nl.po"
    path.write_text('msgid ""\nmsgstr ""\n"Language: nl\\n"\n', encoding="utf-8")
    write_texts(load_catalog(path), {"k": "Ka"}, source=load_catalog(source))
    assert path.read_text(encoding="utf-8").endswith(
        '\n\n #. extracted note\n  #: src/App.tsx:3\nmsgid "k"\nmsgstr "Ka"\n'
    )
