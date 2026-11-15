"""Deterministic checks of a target catalog against its source catalog.

Each check yields at most one finding per entry and kind. The CLI, CSV export and the
apply step all consume these findings; none of them run checks of their own.
"""

import hashlib
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from enum import StrEnum
from functools import cache

from babel import Locale, UnknownLocaleError

from veriglot.catalog import Catalog, is_read_only
from veriglot.icu import IcuSyntaxError, Shape, parse

# Typographic spacing: French puts a space before these.
_SPACED_PUNCTUATION = frozenset(":;!?»")
# Integers searched for each plural category; CLDR categories that select none of them
# (French "many" is for millions) are left to "other".
_SAMPLE_INTEGERS = range(1000)


class Kind(StrEnum):
    """What a finding is about."""

    MISSING = "missing"
    EXTRA = "extra"
    UNTRANSLATED = "untranslated"
    PLACEHOLDER = "placeholder"
    PLURAL = "plural"
    SYNTAX = "syntax"
    WHITESPACE = "whitespace"
    TAG = "tag"


class Severity(StrEnum):
    """How bad a finding is: only errors fail a check."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2}


@dataclass(frozen=True)
class Finding:
    """One problem with one entry, with the snapshot it was found in."""

    id: str
    """``<locale>:<key>:<kind>``, stable across runs."""
    locale: str
    key: str
    kind: Kind
    severity: Severity
    source: str
    target: str
    context: str
    message: str
    proposed: str | None
    """A corrected target, when one follows from the check alone."""
    fingerprint: str
    """``fingerprint(source, target)``: apply refuses the finding once either text changes."""

    def to_dict(self) -> dict[str, str | None]:
        """Plain strings for JSON and CSV."""
        return {k: str(v) if isinstance(v, StrEnum) else v for k, v in asdict(self).items()}


def fingerprint(source: str, target: str) -> str:
    """A short hash of the source and target text a finding was made against."""
    return hashlib.sha256(f"{source}\0{target}".encode()).hexdigest()[:16]


def check_catalogs(source: Catalog, target: Catalog, read_only: tuple[str, ...] = ()) -> list[Finding]:
    """Check every entry of ``target`` against ``source``, in source order, then extra keys."""
    findings: list[Finding] = []
    for key, src in source.entries.items():
        entry = target.entries.get(key)
        context = _context(target, key, read_only)
        if entry is None:
            findings += _finding(target.locale, key, src.text, "", context, Kind.MISSING, Severity.ERROR, ["no entry"])
        else:
            findings += check_entry(target.locale, key, src.text, entry.text, context)
    for key, entry in target.entries.items():
        if key not in source.entries:
            context = _context(target, key, read_only)
            message = ["not in the source catalog"]
            findings += _finding(target.locale, key, "", entry.text, context, Kind.EXTRA, Severity.WARNING, message)
    return findings


def _context(catalog: Catalog, key: str, read_only: tuple[str, ...]) -> str:
    entry = catalog.entries.get(key)
    where = catalog.path.as_posix() + (f":{entry.line}" if entry and entry.line else "")
    notes = [where]
    if entry and entry.context:
        notes.append(entry.context)
    if is_read_only(key, read_only):
        notes.append("read-only (vendored)")
    return "; ".join(notes)


def check_entry(locale: str, key: str, source: str, target: str, context: str = "") -> list[Finding]:
    """Check one translation against its source text."""
    if not target and source:
        return _finding(locale, key, source, target, context, Kind.MISSING, Severity.ERROR, ["empty translation"])
    issues: list[tuple[Kind, Severity, str]] = []
    try:
        src = parse(source)
    except IcuSyntaxError as exc:
        issues.append((Kind.SYNTAX, Severity.ERROR, f"source: {exc}"))
        src = None
    try:
        tgt = parse(target)
    except IcuSyntaxError as exc:
        issues.append((Kind.SYNTAX, Severity.ERROR, str(exc)))
        tgt = None
    if src and tgt:
        issues += _compare(locale, source, target, src, tgt)
    ws_messages, proposed = _whitespace(source, target)
    issues += [(Kind.WHITESPACE, Severity.WARNING, m) for m in ws_messages]
    by_kind: dict[Kind, list[tuple[Severity, str]]] = {}
    for kind, severity, message in issues:
        by_kind.setdefault(kind, []).append((severity, message))
    findings: list[Finding] = []
    for kind, found in by_kind.items():
        severity = max((s for s, _ in found), key=_RANK.__getitem__)
        findings += _finding(
            locale,
            key,
            source,
            target,
            context,
            kind,
            severity,
            [m for _, m in found],
            proposed if kind is Kind.WHITESPACE else None,
        )
    return findings


def _compare(locale: str, source: str, target: str, src: Shape, tgt: Shape) -> list[tuple[Kind, Severity, str]]:
    issues: list[tuple[Kind, Severity, str]] = []
    if target == source and any(c.isalpha() for c in src.text):
        issues.append((Kind.UNTRANSLATED, Severity.INFO, "same as the source text"))
    if missing := sorted(src.names - tgt.names):
        issues.append((Kind.PLACEHOLDER, Severity.ERROR, "missing " + ", ".join(f"{{{n}}}" for n in missing)))
    if unexpected := sorted(tgt.names - src.names):
        issues.append((Kind.PLACEHOLDER, Severity.ERROR, "unexpected " + ", ".join(f"{{{n}}}" for n in unexpected)))
    issues += [(Kind.PLURAL, severity, message) for severity, message in _branch_issues(locale, src, tgt)]
    if Counter(src.tags) != Counter(tgt.tags):
        issues.append((Kind.TAG, Severity.ERROR, _diff("tag", src.tags, tgt.tags)))
    return issues


def _finding(
    locale: str,
    key: str,
    source: str,
    target: str,
    context: str,
    kind: Kind,
    severity: Severity,
    messages: list[str],
    proposed: str | None = None,
) -> list[Finding]:
    return [
        Finding(
            id=f"{locale}:{key}:{kind}",
            locale=locale,
            key=key,
            kind=kind,
            severity=severity,
            source=source,
            target=target,
            context=context,
            message="; ".join(messages),
            proposed=proposed,
            fingerprint=fingerprint(source, target),
        )
    ]


def _diff(what: str, source: tuple[str, ...], target: tuple[str, ...]) -> str:
    missing = Counter(source) - Counter(target)
    extra = Counter(target) - Counter(source)
    parts = [f"missing {what} {t}" for t in sorted(missing.elements())]
    parts += [f"unexpected {what} {t}" for t in sorted(extra.elements())]
    return "; ".join(parts)


@cache
def _categories(locale: str, kind: str) -> dict[str, frozenset[int]] | None:
    """CLDR plural categories for a locale, each with the integers below 1000 it selects."""
    try:
        loc = Locale.parse(locale.replace("-", "_"))
    except UnknownLocaleError, ValueError:
        return None
    rule = loc.ordinal_form if kind == "selectordinal" else loc.plural_form
    samples: dict[str, set[int]] = {tag: set() for tag in (*rule.tags, "other")}
    for n in _SAMPLE_INTEGERS:
        samples[rule(n)].add(n)
    return {tag: frozenset(ns) for tag, ns in samples.items()}


def _branch_issues(locale: str, src: Shape, tgt: Shape) -> list[tuple[Severity, str]]:
    issues: list[tuple[Severity, str]] = []
    for name in sorted(src.names & tgt.names):
        s, t = src.complex.get(name), tgt.complex.get(name)
        if s is None:
            continue  # a target may add plural or select branches where the source has none
        if t is None:
            cats = _categories(locale, s[0])
            # Japanese and Chinese have only "other", so a plain {count} is complete there.
            if not (s[0] != "select" and cats is not None and set(cats) == {"other"}):
                issues.append((Severity.ERROR, f"{{{name}}} has no {s[0]} branches"))
            continue
        if s[0] != t[0]:
            issues.append((Severity.ERROR, f"{{{name}}} is {t[0]} in the target, {s[0]} in the source"))
            continue
        keys = t[1]
        if "other" not in keys:
            issues.append((Severity.ERROR, f"{{{name}}} has no 'other' branch"))
        if t[0] == "select":
            if missing := sorted(s[1] - keys - {"other"}):
                issues.append((Severity.WARNING, f"{{{name}}} lacks select branches {', '.join(missing)}"))
            if extra := sorted(keys - s[1]):
                issues.append(
                    (Severity.WARNING, f"{{{name}}} has select branches not in the source: {', '.join(extra)}")
                )
            continue
        issues += _category_issues(locale, name, t[0], keys)
    return issues


def _category_issues(locale: str, name: str, kind: str, keys: frozenset[str]) -> list[tuple[Severity, str]]:
    cats = _categories(locale, kind)
    if cats is None:
        return [(Severity.WARNING, f"no CLDR plural rules for locale {locale!r}")]
    exact = {int(float(k[1:])) for k in keys if k.startswith("=")}
    named = {k for k in keys if not k.startswith("=")}
    issues: list[tuple[Severity, str]] = []
    if unknown := sorted(named - set(cats)):
        issues.append((Severity.WARNING, f"{{{name}}} has categories {locale} never selects: {', '.join(unknown)}"))
    # A missing category matters only if it selects an integer no exact branch covers.
    if missing := sorted(c for c, ns in cats.items() if c not in named and c != "other" and ns - exact):
        issues.append((Severity.WARNING, f"{{{name}}} lacks {locale} categories {', '.join(missing)}"))
    return issues


def _wide(char: str) -> bool:
    """Full-width punctuation and CJK text carry their own spacing."""
    return unicodedata.east_asian_width(char) in {"W", "F"}


def _whitespace(source: str, target: str) -> tuple[list[str], str | None]:
    """Compare leading/trailing whitespace and newline counts; never trims.

    Returns the messages and, when only the ends differ, the target with the source's
    whitespace restored on the wrong ends.
    """
    messages: list[str] = []
    core = target.strip()
    if not core:
        return messages, None
    s_lead, s_trail = source[: len(source) - len(source.lstrip())], source[len(source.rstrip()) :]
    t_lead, t_trail = target[: len(target) - len(target.lstrip())], target[len(target.rstrip()) :]
    lead, trail = t_lead, t_trail
    if s_lead and not t_lead and not _wide(core[0]):
        messages.append("missing leading whitespace")
        lead = s_lead
    elif t_lead and not s_lead and core[0] not in _SPACED_PUNCTUATION:
        messages.append("unexpected leading whitespace")
        lead = ""
    if s_trail and not t_trail and not _wide(core[-1]):
        messages.append("missing trailing whitespace")
        trail = s_trail
    elif t_trail and not s_trail:
        messages.append("unexpected trailing whitespace")
        trail = ""
    if (s_lines := source.count("\n")) != (t_lines := target.count("\n")):
        messages.append(f"{t_lines} newlines, source has {s_lines}")
        return messages, None
    return messages, (lead + core + trail if messages else None)
