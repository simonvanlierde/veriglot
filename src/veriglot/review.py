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
from typing import TYPE_CHECKING

from babel import Locale, UnknownLocaleError

from veriglot.catalog import Catalog, is_read_only
from veriglot.icu import Arg, Branch, IcuSyntaxError, Shape, parse

if TYPE_CHECKING:
    from collections.abc import Iterable

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
    FUZZY = "fuzzy"
    # Kinds only a model review reports; the deterministic checks never produce them.
    MEANING = "meaning"
    OMISSION = "omission"
    ADDITION = "addition"
    TERMINOLOGY = "terminology"
    REGISTER = "register"
    GRAMMAR = "grammar"


# Kinds the deterministic checks never report: their absence after an edit proves nothing.
MODEL_ONLY_KINDS = frozenset(
    {Kind.MEANING, Kind.OMISSION, Kind.ADDITION, Kind.TERMINOLOGY, Kind.REGISTER, Kind.GRAMMAR}
)


# What `veriglot kinds` prints: the severity each kind gets, and what it finds.
KIND_HELP: dict[Kind, tuple[str, str]] = {
    Kind.MISSING: ("error", "a source key with no entry, or an empty or whitespace-only translation"),
    Kind.EXTRA: ("warning", "a key that is not in the source catalog"),
    Kind.UNTRANSLATED: ("info", "a translation identical to source text that has letters"),
    Kind.PLACEHOLDER: ("error or warning", "arguments such as {name} missing or added, also per plural branch"),
    Kind.PLURAL: ("error or warning", "plural or select branches that do not match the source or the CLDR categories"),
    Kind.SYNTAX: ("error", "malformed ICU MessageFormat in the source or target"),
    Kind.WHITESPACE: ("warning", "leading or trailing whitespace, or a newline count, that differs from the source"),
    Kind.TAG: ("error", "rich-text tags such as <contrib> or <0> missing or added"),
    Kind.FUZZY: ("info", "an entry marked #, fuzzy"),
    Kind.MEANING: ("per finding", "the translation says something different from the source"),
    Kind.OMISSION: ("per finding", "part of the source meaning is missing from the translation"),
    Kind.ADDITION: ("per finding", "the translation adds meaning the source does not have"),
    Kind.TERMINOLOGY: ("per finding", "a glossary term is not used, or another word for the same concept"),
    Kind.REGISTER: ("per finding", "formal or informal address differs from the style rule"),
    Kind.GRAMMAR: ("per finding", "spelling, grammar or agreement errors in the translation"),
}


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
            if entry.fuzzy:
                message = ["marked fuzzy: the translation awaits review"]
                findings += _finding(
                    target.locale, key, src.text, entry.text, context, Kind.FUZZY, Severity.INFO, message
                )
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
    if not target.strip() and source.strip():
        message = "empty translation" if not target else "translation is only whitespace"
        return _finding(locale, key, source, target, context, Kind.MISSING, Severity.ERROR, [message])
    issues = _issues(locale, source, target)
    ws_messages, proposed = _whitespace(source, target)
    if proposed is not None and any(s is Severity.ERROR for _, s, _ in _issues(locale, source, proposed)):
        proposed = None  # apply would reject it
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


type Issue = tuple[Kind, Severity, str]


def _issues(locale: str, source: str, target: str) -> list[Issue]:
    """Syntax and structure issues; whitespace is checked separately."""
    issues: list[Issue] = []
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
    return issues


def _compare(locale: str, source: str, target: str, src: Shape, tgt: Shape) -> list[Issue]:
    issues: list[Issue] = []
    if target == source and any(c.isalpha() for c in src.text):
        issues.append((Kind.UNTRANSLATED, Severity.INFO, "same as the source text"))
    if missing := sorted(src.names - tgt.names):
        issues.append((Kind.PLACEHOLDER, Severity.ERROR, "missing " + ", ".join(f"{{{n}}}" for n in missing)))
    if unexpected := sorted(tgt.names - src.names):
        issues.append((Kind.PLACEHOLDER, Severity.ERROR, "unexpected " + ", ".join(f"{{{n}}}" for n in unexpected)))
    issues += _branch_issues(locale, src, tgt)
    # Tags outside branches, plus the 'other' branch of an argument only one side branches on.
    # shortcut: a branching argument nested in a branch adds its tags at the top level too; track
    # nesting if catalogs nest tagged plurals.
    src_tags = list(src.tags) + [t for n, a in src.complex.items() if n not in tgt.complex for t in _other(a).tags]
    tgt_tags = list(tgt.tags) + [t for n, a in tgt.complex.items() if n not in src.complex for t in _other(a).tags]
    if Counter(src_tags) != Counter(tgt_tags):
        issues.append((Kind.TAG, Severity.ERROR, _diff("tag", src_tags, tgt_tags)))
    return issues


def _other(arg: Arg) -> Branch:
    return arg.branches["other"]  # the parser requires it


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


def _diff(what: str, source: Iterable[str], target: Iterable[str]) -> str:
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


def _branch_issues(locale: str, src: Shape, tgt: Shape) -> list[Issue]:
    issues: list[Issue] = []
    for name in sorted(src.names & tgt.names):
        s, t = src.complex.get(name), tgt.complex.get(name)
        if s is None:
            continue  # a target may add plural or select branches where the source has none
        if t is None:
            cats = _categories(locale, s.kind)
            # Japanese and Chinese have only "other", so a plain {count} is complete there.
            if not (s.kind != "select" and cats is not None and set(cats) == {"other"}):
                issues.append((Kind.PLURAL, Severity.ERROR, f"{{{name}}} has no {s.kind} branches"))
            continue
        if s.kind != t.kind:
            issues.append(
                (Kind.PLURAL, Severity.ERROR, f"{{{name}}} is {t.kind} in the target, {s.kind} in the source")
            )
            continue
        issues += _per_branch(locale, name, s, t)
        keys = frozenset(t.branches)
        if t.kind == "select":
            if missing := sorted(set(s.branches) - keys - {"other"}):
                issues.append((Kind.PLURAL, Severity.WARNING, f"{{{name}}} lacks select branches {', '.join(missing)}"))
            if extra := sorted(keys - set(s.branches)):
                message = f"{{{name}}} has select branches not in the source: {', '.join(extra)}"
                issues.append((Kind.PLURAL, Severity.WARNING, message))
            continue
        issues += [(Kind.PLURAL, sev, m) for sev, m in _category_issues(locale, name, t)]
    return issues


def _per_branch(locale: str, name: str, s: Arg, t: Arg) -> list[Issue]:
    """Compare each target branch with the source branch of the same key, else source 'other'.

    Branch counts differ by locale, so names, ``#`` and tags are compared branch by branch
    rather than summed over all branches.
    """
    issues: list[Issue] = []
    counted = s.kind != "select"
    cats = _categories(locale, s.kind) if counted else None
    for key, tb in t.branches.items():
        sb = s.branches.get(key, _other(s))
        where = f"{{{name}}} branch {key!r}"
        # A branch for one number (=0, or Dutch "one") may spell it out.
        single = key.startswith("=") or (cats is not None and len(cats.get(key, ())) == 1)
        # In a plural, {n} and # both show the number, so they stand in for each other.
        if counted and not single and (sb.hash or name in sb.names) and not (tb.hash or name in tb.names):
            issues.append((Kind.PLACEHOLDER, Severity.WARNING, f"{where} shows neither # nor {{{name}}}"))
        if lacking := sorted(sb.names - tb.names - ({name} if counted else set())):
            issues.append(
                (Kind.PLACEHOLDER, Severity.WARNING, f"{where} lacks " + ", ".join(f"{{{n}}}" for n in lacking))
            )
        if Counter(sb.tags) != Counter(tb.tags):
            issues.append((Kind.TAG, Severity.ERROR, f"{where}: " + _diff("tag", sb.tags, tb.tags)))
    return issues


def _category_issues(locale: str, name: str, arg: Arg) -> list[tuple[Severity, str]]:
    cats = _categories(locale, arg.kind)
    if cats is None:
        return [(Severity.WARNING, f"no CLDR plural rules for locale {locale!r}")]
    # Exact keys match the number itself; categories match the number minus the offset.
    exact = {int(k[1:]) - arg.offset for k in arg.branches if k.startswith("=")}
    named = {k for k in arg.branches if not k.startswith("=")}
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
