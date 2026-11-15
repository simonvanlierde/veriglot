"""Apply reviewer decisions to a target catalog, accounting for every decision."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from veriglot.catalog import Catalog, CatalogChangedError, is_read_only, write_texts
from veriglot.review import Kind, Severity, check_entry, fingerprint

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

type Status = Literal["applied", "rejected", "skipped"]


@dataclass(frozen=True)
class DecisionRow:
    """A reviewer's decision on one finding, as the review UI or CSV hands it back."""

    id: str
    fingerprint: str
    decision: str
    """``accept`` (write ``proposed``), ``edit`` (write ``edited``), ``reject`` or empty."""
    proposed: str | None = None
    edited: str | None = None
    note: str = ""


@dataclass(frozen=True)
class Outcome:
    """What happened to one decision row."""

    id: str
    status: Status
    reason: str
    """Empty when applied. Skipped: ``other locale``, ``declined``, ``undecided``,
    ``already applied`` or ``resolved`` (an earlier row in the run fixed it). Rejected:
    ``unknown``, ``invalid decision``, ``invalid``, ``read-only``, ``unsupported``, ``stale``
    or ``catalog changed``."""
    detail: str = ""
    text: str | None = None
    """The text written or that would have been written."""


@dataclass(frozen=True)
class ApplyReport:
    """Every decision row with its outcome, and whether the catalog file was written."""

    path: Path
    outcomes: tuple[Outcome, ...]
    written: bool

    @property
    def applied(self) -> list[Outcome]:
        """Rows whose text was (or, in a dry run, would be) written."""
        return [o for o in self.outcomes if o.status == "applied"]

    @property
    def rejected(self) -> list[Outcome]:
        """Rows that could not be applied."""
        return [o for o in self.outcomes if o.status == "rejected"]


def apply_decisions(
    source: Catalog,
    target: Catalog,
    rows: Iterable[DecisionRow],
    *,
    read_only: tuple[str, ...] = (),
    dry_run: bool = False,
) -> ApplyReport:
    """Apply accepted and edited rows to ``target`` in order, then write it once.

    Each row is checked against the current text, including earlier rows' changes: a later
    decision on an entry already changed is ``resolved`` when its finding is gone, else stale.
    Rows for another locale are skipped, so one sheet for all locales applies per locale.
    If the target file changed on disk meanwhile, nothing is written and every row is
    rejected as ``catalog changed``.
    """
    current = {key: entry.text for key, entry in target.entries.items()}
    changes: dict[str, str] = {}
    rows = list(rows)
    outcomes = [_decide(row, source, target, current, changes, read_only) for row in rows]
    written = bool(changes) and not dry_run
    if written:
        try:
            write_texts(target, changes)
        except CatalogChangedError as exc:
            outcomes = [Outcome(row.id, "rejected", "catalog changed", str(exc)) for row in rows]
            written = False
    return ApplyReport(target.path, tuple(outcomes), written)


def _decide(  # noqa: C901, PLR0911, PLR0912 - one return per outcome keeps the order of checks readable
    row: DecisionRow,
    source: Catalog,
    target: Catalog,
    current: dict[str, str],
    changes: dict[str, str],
    read_only: tuple[str, ...],
) -> Outcome:
    def rejected(reason: str, detail: str, text: str | None = None) -> Outcome:
        return Outcome(row.id, "rejected", reason, detail, text)

    locale, _, rest = row.id.partition(":")
    key, sep, kind = rest.rpartition(":")
    if not (sep and key):
        return rejected("unknown", f"malformed id {row.id!r}; expected <locale>:<key>:<kind>")
    if locale != target.locale:
        return Outcome(row.id, "skipped", "other locale", f"row is for {locale}, catalog is {target.locale}")
    if kind not in set(Kind):
        return rejected("unknown", f"unknown kind {kind!r}")
    decision = row.decision.strip().lower()
    if decision == "reject":
        return Outcome(row.id, "skipped", "declined")
    if not decision:
        return Outcome(row.id, "skipped", "undecided")
    if decision not in {"accept", "edit"}:
        return rejected("invalid decision", f"decision {row.decision!r} is not accept, edit, reject or empty")
    text = row.proposed if decision == "accept" else row.edited
    if not text:
        field = "proposed" if decision == "accept" else "edited"
        return rejected("invalid", f"{decision} needs text in the {field} column")
    if key not in current:
        return rejected("unknown", f"no {key!r} entry for {target.locale} in {target.path.as_posix()}")
    if is_read_only(key, read_only):
        return rejected("read-only", f"{key!r} is vendored; fix it upstream", text)
    if target.path.suffix != ".po":
        return rejected("unsupported", f"{target.path.suffix} catalogs are check-only", text)
    # Writing unchanged text to a fuzzy entry confirms it: the write drops the flag.
    fuzzy = target.entries[key].fuzzy and key not in changes
    if current[key] == text and not fuzzy:
        return Outcome(row.id, "skipped", "already applied", text=text)
    source_text = source.entries[key].text if key in source.entries else ""
    if fingerprint(source_text, current[key]) != row.fingerprint:
        if key in changes and kind not in {f.kind for f in check_entry(target.locale, key, source_text, current[key])}:
            return Outcome(row.id, "skipped", "resolved", "an earlier row fixed it", text)
        return rejected("stale", "the source or target text changed after the review", text)
    errors = [f.message for f in check_entry(target.locale, key, source_text, text) if f.severity is Severity.ERROR]
    if errors:
        return rejected("invalid", "; ".join(errors), text)
    current[key] = changes[key] = text
    return Outcome(row.id, "applied", "", "cleared fuzzy flag" if fuzzy else "", text)
