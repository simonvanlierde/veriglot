"""Read message catalogs and write changed translations back without reformatting them."""

import io
import json
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING

from babel.messages.pofile import escape, read_po

if TYPE_CHECKING:
    from pathlib import Path

# CRediT role names and descriptions are vendored from the credit-translation project;
# fixes go upstream, so they are never written here.
VENDORED_KEYS = ("https://credit.niso.org/contributor-roles/*",)

_LANGUAGE = re.compile(r'^"Language: *([^\\"]+?) *\\n"', re.MULTILINE)


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


@dataclass(frozen=True)
class Catalog:
    """One locale's messages, keyed by message id, in file order."""

    path: Path
    locale: str
    entries: dict[str, Entry]
    raw: bytes
    """The file as read, so a write can check nothing changed since."""


def is_read_only(key: str, patterns: tuple[str, ...]) -> bool:
    """Whether a key is vendored (``VENDORED_KEYS``) or matches one of ``patterns`` (globs)."""
    return any(fnmatchcase(key, p) for p in (*VENDORED_KEYS, *patterns))


def load_catalog(path: Path) -> Catalog:
    """Load a Lingui/gettext ``.po`` catalog or a flat JSON catalog (``{"key": "text"}``).

    The locale comes from the ``.po`` ``Language`` header, else the folder name; for JSON
    it is the file name (``nl.json``).

    Raises:
        OSError: the file cannot be read.
        ValueError: the file is not a catalog this module reads.
    """
    raw = path.read_bytes()
    if path.suffix == ".json":
        return _load_json(path, raw)
    if path.suffix != ".po":
        msg = f"{path}: unsupported catalog format {path.suffix!r} (expected .po or .json)"
        raise ValueError(msg)
    text = raw.decode("utf-8")
    header = _LANGUAGE.search(text)
    locale = header.group(1) if header else path.parent.name
    entries: dict[str, Entry] = {}
    for message in read_po(io.StringIO(text)):
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
        entries[message.id] = Entry(message.id, str(message.string), "; ".join(notes), message.lineno or 0)
    return Catalog(path, locale, entries, raw)


def _load_json(path: Path, raw: bytes) -> Catalog:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        msg = f"{path}: expected a flat JSON object of strings"
        raise ValueError(msg)
    return Catalog(path, path.stem, {k: Entry(k, v, "", 0) for k, v in data.items()}, raw)


def write_texts(catalog: Catalog, texts: dict[str, str]) -> None:
    """Replace the ``msgstr`` of each key in ``texts`` and leave every other byte alone.

    The new text is written as one ``msgstr "..."`` line, as Lingui writes them.

    Raises:
        CatalogChangedError: the file changed since ``catalog`` was read.
        ValueError: the catalog is not a ``.po`` file.
    """
    if catalog.path.suffix != ".po":
        msg = f"{catalog.path}: only .po catalogs can be written"
        raise ValueError(msg)
    lines = catalog.raw.decode("utf-8").splitlines(keepends=True)
    # Replace from the bottom up so earlier line numbers stay valid.
    for key in sorted(texts, key=lambda k: catalog.entries[k].line, reverse=True):
        start = catalog.entries[key].line
        i = start - 1
        while not lines[i].startswith("msgstr"):
            i += 1
        end = i + 1
        while end < len(lines) and lines[end].lstrip().startswith('"'):
            end += 1
        newline = lines[end - 1][len(lines[end - 1].rstrip("\r\n")) :]
        lines[i:end] = [f"msgstr {escape(texts[key])}{newline}"]
    if catalog.path.read_bytes() != catalog.raw:
        msg = f"{catalog.path} changed since it was read; check it again before applying"
        raise CatalogChangedError(msg)
    catalog.path.write_bytes("".join(lines).encode("utf-8"))
