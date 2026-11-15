"""The reviewer CSV: findings out, decisions back in. A thin layer over review and apply."""

import csv
from typing import TYPE_CHECKING, TextIO

from veriglot.apply import DecisionRow

if TYPE_CHECKING:
    from collections.abc import Iterable

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


def write_findings(findings: Iterable[Finding], stream: TextIO) -> None:
    """Write findings with empty decision columns for a reviewer to fill in."""
    # Quote every cell so spreadsheets keep leading and trailing spaces.
    writer = csv.DictWriter(stream, COLUMNS, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writeheader()
    for finding in findings:
        row = {k: v or "" for k, v in finding.to_dict().items()}
        writer.writerow({**row, "decision": "", "edited": "", "note": ""})


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
            proposed=row.get("proposed") or None,
            edited=row.get("edited") or None,
            note=row.get("note") or "",
        )
        for row in reader
    ]
