"""Read message catalogs and write changed translations back without reformatting them."""

import codecs
import io
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from babel.messages.pofile import PoFileError, escape, read_po, unescape

# CRediT role names and descriptions are vendored from the credit-translation project;
# fixes go upstream, so they are never written here.
VENDORED_KEYS = ("https://credit.niso.org/contributor-roles/*",)

_LANGUAGE = re.compile(r'^"Language: *([^\\"]+?) *\\n"', re.MULTILINE)
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')


class CatalogChangedError(RuntimeError):
    """The file on disk changed after it was read, so writing would overwrite newer edits."""


@dataclass(frozen=True)
class Entry:
    """One message: its key, translated text and where it came from."""

    key: str
    text: str
    context: str
    """The ``msgctxt`` and comments, if any."""
    line: int
    """1-based line of the ``msgid``; 0 for formats without lines."""
    fuzzy: bool = False
    """Marked ``#, fuzzy``: a draft awaiting review."""


@dataclass(frozen=True)
class Catalog:
    """One locale's messages, keyed by message id, in file order."""

    path: Path
    locale: str
    entries: dict[str, Entry]
    raw: bytes
    """The file as read, byte order mark included, so a write can check nothing changed since."""


def is_read_only(key: str, patterns: tuple[str, ...]) -> bool:
    """Whether a key is vendored (``VENDORED_KEYS``) or matches one of ``patterns`` (globs)."""
    return any(fnmatchcase(key, p) for p in (*VENDORED_KEYS, *patterns))


def load_catalog(path: Path) -> Catalog:
    """Load a Lingui/gettext ``.po`` catalog or a flat JSON catalog (``{"key": "text"}``).

    The locale comes from the ``.po`` ``Language`` header, else the folder name; for JSON
    it is the file name (``nl.json``). A UTF-8 byte order mark is allowed.

    Raises:
        OSError: the file cannot be read.
        ValueError: the file is not a catalog this module reads, or a ``.po`` line is malformed
            (the message names the line).
    """
    raw = path.read_bytes()
    if path.suffix == ".json":
        return _load_json(path, raw)
    if path.suffix != ".po":
        msg = f"{path}: unsupported catalog format {path.suffix!r} (expected .po or .json)"
        raise ValueError(msg)
    text = raw.decode("utf-8-sig")
    _check_strings(path, text)
    header = _LANGUAGE.search(text)
    locale = header.group(1) if header else path.parent.name
    try:
        # StringIO splits lines on "\n" only, so Babel's line numbers count "\n" only too.
        messages = read_po(io.StringIO(text), abort_invalid=True)
    except PoFileError as exc:
        msg = f"{path}:{exc.lineno + 1}: {str(exc).rpartition(' on ')[0]}: {exc.line!r}"
        raise ValueError(msg) from exc
    entries: dict[str, Entry] = {}
    for message in messages:
        if not message.id:
            continue  # the header
        if not isinstance(message.id, str):
            msg = f"{path}:{message.lineno}: gettext plural entries are not supported; use ICU plurals"
            raise ValueError(msg)  # noqa: TRY004 - bad file content, not a caller's type error
        if message.id in entries:
            # NOTE: keys are msgids; Lingui catalogs never reuse one under another msgctxt.
            msg = f"{path}:{message.lineno}: duplicate msgid {message.id!r}"
            raise ValueError(msg)
        notes = [f"msgctxt: {message.context}"] if message.context else []
        notes += [*message.auto_comments, *message.user_comments]
        entries[message.id] = Entry(
            message.id, str(message.string), "; ".join(notes), message.lineno or 0, fuzzy=message.fuzzy
        )
    return Catalog(path, locale, entries, raw)


def _check_strings(path: Path, text: str) -> None:
    """Refuse unterminated or trailing-garbage strings, which Babel reads without complaint."""
    for number, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()  # as Babel strips each line
        if not stripped or stripped.startswith("#"):
            continue
        string = stripped if stripped.startswith('"') else stripped.partition(" ")[2].strip()
        if not _STRING.fullmatch(string):
            msg = f"{path}:{number}: malformed string: {line!r}"
            raise ValueError(msg)


def _lines(text: str) -> list[str]:
    r"""Split on "\n" only, keeping it: Babel's line numbers ignore U+2028 and other breaks."""
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _block_end(lines: list[str], i: int) -> int:
    """The index after the keyword line at ``i`` and its continuation lines."""
    end = i + 1
    while end < len(lines) and lines[end].lstrip().startswith('"'):
        end += 1
    return end


def _string(lines: list[str]) -> str:
    """The text of a keyword line and its continuation lines."""
    first = lines[0].strip().partition(" ")[2].strip()
    return "".join(unescape(s) for s in (first, *(line.strip() for line in lines[1:])))


def _clear_fuzzy(lines: list[str], msgid: int) -> None:
    """Drop ``fuzzy`` from the ``#,`` line of the entry whose ``msgid`` line is at ``msgid``."""
    k = msgid - 1
    while k >= 0 and (s := lines[k].strip()) and not s.startswith("#~") and (s[0] in '#"' or s.startswith("msgctxt")):
        if s.startswith("#,"):
            flags = [f.strip() for f in s[2:].split(",")]
            if "fuzzy" in flags:
                rest = [f for f in flags if f != "fuzzy"]
                ending = lines[k][len(lines[k].rstrip("\r\n")) :]
                lines[k : k + 1] = [f"#, {', '.join(rest)}{ending}"] if rest else []
            return
        k -= 1


def _load_json(path: Path, raw: bytes) -> Catalog:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        msg = f"{path}: expected a flat JSON object of strings"
        raise ValueError(msg)
    return Catalog(path, path.stem, {k: Entry(k, v, "", 0) for k, v in data.items()}, raw)


def _source_head(source: Catalog | None, key: str, lines: list[str] | None) -> list[str]:
    """The comment, ``msgctxt`` and ``msgid`` lines of ``key`` in ``source``, without line endings.

    Translator comments (``# ``) and previous msgids (``#|``) describe the source's own translation
    and are left out, as is a ``fuzzy`` flag.
    """
    if source is None or key not in source.entries:
        msg = f"no source entry for new key {key!r}"
        raise ValueError(msg)
    if lines is None:
        return [f"msgid {escape(key)}"]
    line = source.entries[key].line
    i = line - 1
    end = _block_end(lines, i) if 0 <= i < len(lines) else i
    if not (0 <= i < len(lines) and lines[i].lstrip().startswith("msgid ") and _string(lines[i:end]) == key):
        msg = f"{source.path}:{line}: expected msgid {key!r} here"
        raise ValueError(msg)
    start = i
    while (
        start > 0
        and (s := lines[start - 1].strip())
        and not s.startswith("#~")
        and (s[0] in '#"' or s.startswith("msgctxt"))
    ):
        start -= 1
    while lines[start].lstrip().startswith('"'):  # the end of a previous entry with no blank line after it
        start += 1
    head = [x.rstrip("\r\n") for x in lines[start:end] if not (c := x.lstrip()).startswith("#") or c[1:2] in ".:,"]
    _clear_fuzzy(head, next(n for n, x in enumerate(head) if x.lstrip().startswith("msgid ")))
    return head


def write_texts(catalog: Catalog, texts: dict[str, str], *, source: Catalog | None = None) -> None:
    """Replace the ``msgstr`` of each key in ``texts`` and leave every other byte alone.

    The new text is written as one ``msgstr "..."`` line, as Lingui writes them, and a
    ``fuzzy`` flag on that entry is dropped: the text has been reviewed. A key the catalog
    lacks is appended as a new entry with the extracted comments, references, flags (without
    ``fuzzy``), ``msgctxt`` and ``msgid`` of its ``source`` entry, in the file's line ending.
    The file is replaced atomically.

    Raises:
        CatalogChangedError: the file changed since ``catalog`` was read.
        ValueError: the catalog is not a ``.po`` file, an entry's line does not hold its msgid,
            or a new key has no ``source`` entry.
    """
    if catalog.path.suffix != ".po":
        msg = f"{catalog.path}: only .po catalogs can be written"
        raise ValueError(msg)
    bom = codecs.BOM_UTF8 if catalog.raw.startswith(codecs.BOM_UTF8) else b""
    lines = _lines(catalog.raw.decode("utf-8-sig"))
    new = [key for key in texts if key not in catalog.entries]
    # Replace from the bottom up so earlier line numbers stay valid.
    for key in sorted(texts.keys() - new, key=lambda k: catalog.entries[k].line, reverse=True):
        line = catalog.entries[key].line
        i = line - 1
        end = _block_end(lines, i) if 0 <= i < len(lines) else i
        # msgids are unique in a loaded catalog (load_catalog refuses duplicates), so the msgid
        # alone identifies the block, msgctxt or not.
        if not (0 <= i < len(lines) and lines[i].lstrip().startswith("msgid ") and _string(lines[i:end]) == key):
            msg = f"{catalog.path}:{line}: expected msgid {key!r} here; check the catalog again before applying"
            raise ValueError(msg)
        if end >= len(lines) or not lines[end].lstrip().startswith("msgstr "):
            msg = f"{catalog.path}:{end + 1}: expected the msgstr of {key!r}"
            raise ValueError(msg)
        stop = _block_end(lines, end)
        newline = lines[stop - 1][len(lines[stop - 1].rstrip("\r\n")) :]
        lines[end:stop] = [f"msgstr {escape(texts[key])}{newline}"]
        _clear_fuzzy(lines, i)
    ending = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    source_lines = _lines(source.raw.decode("utf-8-sig")) if new and source and source.path.suffix == ".po" else None
    for key in new:
        head = _source_head(source, key, source_lines)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += ending
        if lines and lines[-1].strip():
            lines.append(ending)
        lines += [line + ending for line in head] + [f"msgstr {escape(texts[key])}{ending}"]
    if catalog.path.read_bytes() != catalog.raw:
        msg = f"{catalog.path} changed since it was read; check it again before applying"
        raise CatalogChangedError(msg)
    fd, tmp = tempfile.mkstemp(dir=catalog.path.parent, prefix=f".{catalog.path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(bom + "".join(lines).encode("utf-8"))
        shutil.copymode(catalog.path, tmp)
        Path(tmp).replace(catalog.path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
