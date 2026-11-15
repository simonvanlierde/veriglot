"""The reviewer CSV: findings out, decisions back in. A thin layer over review and apply.

Text cells (``source``, ``target``, ``proposed``, ``edited``, ``context``, ``message``,
``note``) that a spreadsheet would run as a formula, those starting with ``=``, ``+``,
``-``, ``@``, a tab or a carriage return, get one leading ``'``. So do cells that start
with apostrophes followed by one of those characters, which keeps the escape reversible:
reading strips exactly that one ``'``. Sensebank's review CSV follows the same rule.
"""

import csv
import re
from typing import TYPE_CHECKING, TextIO

from veriglot.apply import DecisionRow

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from veriglot.review import Finding

# Shared with Sensebank's review CSV: keep names and order.
COLUMNS = (
    "id",
    "locale",
    "key",
    "kind",
    "severity",
    "source",
    "target",
    "context",
    "message",
    "proposed",
    "fingerprint",
    "decision",
    "edited",
    "note",
)
_REQUIRED = ("id", "fingerprint", "decision")
_TEXT = frozenset({"source", "target", "proposed", "edited", "context", "message", "note"})
_FORMULA = re.compile(r"'*[=+\-@\t\r]")


def escape_cell(text: str) -> str:
    """Neutralise a text cell a spreadsheet would run as a formula (see the module docstring)."""
    return "'" + text if _FORMULA.match(text) else text


def unescape_cell(text: str) -> str:
    """Undo ``escape_cell``."""
    return text[1:] if text.startswith("'") and _FORMULA.match(text, 1) else text


def write_findings(
    findings: Iterable[Finding], stream: TextIO, decisions: Mapping[str, DecisionRow] | None = None
) -> None:
    """Write findings with the decision columns empty, or filled from ``decisions`` by finding id."""
    # Quote every cell so spreadsheets keep leading and trailing spaces.
    writer = csv.DictWriter(stream, COLUMNS, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writeheader()
    for finding in findings:
        row = {k: escape_cell(v or "") if k in _TEXT else v or "" for k, v in finding.to_dict().items()}
        made = (decisions or {}).get(finding.id)
        writer.writerow(
            {
                **row,
                "decision": made.decision if made else "",
                "edited": escape_cell(made.edited or "") if made else "",
                "note": escape_cell(made.note) if made else "",
            }
        )


def read_decisions(stream: TextIO) -> list[DecisionRow]:
    """Read decision rows; other columns are ignored.

    Raises:
        ValueError: a required column is missing.
    """
    reader = csv.DictReader(stream)
    if missing := [c for c in _REQUIRED if c not in (reader.fieldnames or ())]:
        msg = f"decisions CSV lacks column(s) {', '.join(missing)}"
        raise ValueError(msg)
    return [
        DecisionRow(
            id=row["id"],
            fingerprint=row["fingerprint"],
            decision=row["decision"] or "",
            proposed=unescape_cell(row.get("proposed") or "") or None,
            edited=unescape_cell(row.get("edited") or "") or None,
            note=unescape_cell(row.get("note") or ""),
        )
        for row in reader
    ]
