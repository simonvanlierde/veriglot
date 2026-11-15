"""A planted-error benchmark for model review: build it, run models over it, score recall.

Clean translations get one planted error each, so recall per error kind is measurable. The
perturbations that need no language knowledge are code (seeded, deterministic); wrong senses,
terminology swaps and register shifts are written by hand into a file the generator reads.
The clean translations are themselves unreviewed machine output, so a flag on an unperturbed
string is reported as such, not as a false positive.

The data (items, model answers) is private: every data file starts with a canary line so it can
be found in a training corpus, and the repository ignores ``data/bench``.
"""

import hashlib
import json
import math
import random
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from veriglot.canary import CANARY
from veriglot.catalog import Catalog, is_read_only
from veriglot.model import (
    Glossary,
    ModelClient,
    ModelReview,
    Verdict,
    iter_concurrently,
    judge_entry,
    load_prompt,
    review_entry,
)
from veriglot.review import check_entry

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

CONTROL = "clean"
KINDS = ("omission", "addition", "negation", "number", "entity", "untranslated", "sense", "terminology", "register")
HANDWRITTEN_KINDS = ("sense", "terminology", "register")
# The model kind that names each planted error; "meaning" covers negation, numbers and names.
MODEL_KIND = {
    "omission": "omission",
    "addition": "addition",
    "negation": "meaning",
    "number": "meaning",
    "entity": "meaning",
    "untranslated": "untranslated",
    "sense": "meaning",
    "terminology": "terminology",
    "register": "register",
}
MODES = ("review", "judge")

_INFORMAL = {
    "nl": re.compile(r"\b(je|jij|jouw|jullie|jou)\b", re.IGNORECASE),
    "de": re.compile(r"\b(du|dir|dich|dein\w*|verwende|kopiere|füge|wähle|klicke)\b", re.IGNORECASE),
    "fr": re.compile(r"\b(tu|toi|ton|ta|tes)\b", re.IGNORECASE),
}
_ENTITIES = {
    "ORCID": "ROR",
    "DOI": "ISBN",
    "CRediT": "ORCID",
    "Word": "Excel",
    "JSON": "XML",
    "XML": "JSON",
    "PNG": "PDF",
    "SVG": "GIF",
    "LaTeX": "Typst",
}
_ADDITIONS = {
    "nl": "en dit geldt ook voor alle andere gebruikers",
    "de": "und das gilt auch für alle anderen Nutzer",
    "fr": "et cela vaut aussi pour tous les autres utilisateurs",
}
_NEGATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    # (pattern, replacement) in order; the first pattern that matches is applied once.
    "nl": ((r"\bniet ", ""), (r"\bgeen ", "een "), (r"\b(is|zijn|wordt|worden) ", r"\1 niet ")),
    "de": ((r"\bnicht ", ""), (r"\b(ist|sind|wird|werden|wurde|wurden) ", r"\1 nicht ")),
    "fr": ((r"\b(est) ", "n'est pas "), (r"\b(sont) ", "ne sont pas "), (r"\b(a été) ", "n'a pas été ")),
}
_NUMBER = re.compile(r"(?<![\w{.,])\d+(?![\w}.,])")
_BRACES = re.compile(r"\{[^}]*\}")


@dataclass(frozen=True)
class Item:
    """One string a model reviews: a clean control or a clean string with one planted error."""

    id: str
    locale: str
    key: str
    kind: str
    """``clean`` or one of ``KINDS``."""
    source: str
    target: str
    original: str
    """The unperturbed translation."""
    detail: str = ""
    author: str = "script"
    """``script`` for seeded perturbations, ``agent`` for hand-written ones."""
    native_checked: bool = False


# --- data files -----------------------------------------------------------------------------------


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    """Write JSON Lines whose first line is the canary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"canary": CANARY}) + "\n")
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one record; a new file starts with the canary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="\n") as f:
        if new:
            f.write(json.dumps({"canary": CANARY}) + "\n")
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read JSON Lines, skipping the canary line.

    Raises:
        ValueError: a line is not JSON, or the file lacks the canary.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or json.loads(lines[0]).get("canary") != CANARY:
        msg = f"{path.as_posix()}: first line is not the benchmark canary"
        raise ValueError(msg)
    return [json.loads(line) for line in lines[1:] if line.strip()]


def read_items(path: Path) -> list[Item]:
    """Read an items file written by ``make``."""
    return [Item(**r) for r in read_jsonl(path)]


# --- perturbations --------------------------------------------------------------------------------


def _rng(seed: int, item_id: str) -> random.Random:
    return random.Random(f"{seed}:{item_id}")  # noqa: S311 - not security; str seeds hash with SHA-512: stable across runs


def _span(words: list[str], rng: random.Random) -> tuple[int, int] | None:
    """Start and length of a run of about 30% of the words that holds no placeholder or the last word."""
    n = len(words)
    length = max(2, round(0.3 * n))
    if n < 6:
        return None
    starts = [s for s in range(1, n - length) if not any("{" in w or "}" in w for w in words[s : s + length])]
    return (rng.choice(starts), length) if starts else None


def omission(source: str, target: str, locale: str, rng: random.Random) -> tuple[str, str] | None:  # noqa: ARG001
    """Drop a run of words from the middle."""
    words = target.split(" ")
    if (span := _span(words, rng)) is None:
        return None
    start, length = span
    return " ".join(words[:start] + words[start + length :]), f"dropped words {start}-{start + length - 1}"


def addition(source: str, target: str, locale: str, rng: random.Random) -> tuple[str, str] | None:  # noqa: ARG001
    """Append a clause the source does not have, before the final punctuation."""
    if locale not in _ADDITIONS:
        return None
    body = target.rstrip(" .:;!?…")
    tail = target[len(body) :]
    return f"{body} {_ADDITIONS[locale]}{tail}", "appended clause"


def negation(source: str, target: str, locale: str, rng: random.Random) -> tuple[str, str] | None:  # noqa: ARG001
    """Remove a negation, or add one after a be-verb."""
    for pattern, repl in _NEGATIONS.get(locale, ()):
        new, count = re.subn(pattern, repl, target, count=1)
        if count and new != target:
            return new, f"negation pattern {pattern}"
    return None


def number(source: str, target: str, locale: str, rng: random.Random) -> tuple[str, str] | None:  # noqa: ARG001
    """Change the first number outside placeholders, in the translation only."""
    protected = [m.span() for m in _BRACES.finditer(target)]
    for m in _NUMBER.finditer(target):
        if any(a <= m.start() < b for a, b in protected):
            continue
        old = int(m.group())
        new = old + rng.choice([2, 3, 5, 7])
        return target[: m.start()] + str(new) + target[m.end() :], f"{old} -> {new}"
    return None


def entity(source: str, target: str, locale: str, rng: random.Random) -> tuple[str, str] | None:  # noqa: ARG001
    """Swap a product or format name that both texts carry for another one."""
    for name, other in _ENTITIES.items():
        pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        if pattern.search(source) and pattern.search(target):
            return pattern.sub(other, target, count=1), f"{name} -> {other}"
    return None


def untranslated(source: str, target: str, locale: str, rng: random.Random) -> tuple[str, str] | None:  # noqa: ARG001
    """Replace a run of target words by the source words at the same relative position."""
    words, src = target.split(" "), source.split(" ")
    if len(src) < 6 or (span := _span(words, rng)) is None:
        return None
    start, length = span
    s_start = min(round(start / len(words) * len(src)), len(src) - length - 1)
    piece = src[s_start : s_start + length]
    # A source too short for the span would plant an omission (or a negative slice), not an untranslated run.
    if s_start < 0 or len(piece) != length or any("{" in w or "}" in w for w in piece):
        return None
    new = words[:start] + piece + words[start + length :]
    return " ".join(new), f"source words {s_start}-{s_start + length - 1} left in place"


PERTURBATIONS: dict[str, Callable[[str, str, str, random.Random], tuple[str, str] | None]] = {
    "omission": omission,
    "addition": addition,
    "negation": negation,
    "number": number,
    "entity": entity,
    "untranslated": untranslated,
}


# --- building the benchmark -----------------------------------------------------------------------


def _pool(source: Catalog, target: Catalog, seed: int) -> list[str]:
    """Keys of reviewable strings: one line, 40-170 characters, formal address, not machine-identical."""
    informal = _INFORMAL.get(target.locale.split("-")[0])
    keys = []
    for key, src in source.entries.items():
        t = target.entries.get(key)
        if t is None or is_read_only(key, ()) or t.fuzzy or not t.text.strip() or t.text == src.text:
            continue
        if not 40 <= len(src.text) <= 170 or any(c in src.text + t.text for c in "<\n") or "plural" in src.text:
            continue
        if informal and informal.search(t.text):
            continue
        keys.append(key)
    return sorted(keys, key=lambda k: hashlib.sha256(f"veriglot-bench-{seed}:{k}".encode()).hexdigest())


def _det_kinds(locale: str, key: str, source: str, target: str) -> set[str]:
    return {str(f.kind) for f in check_entry(locale, key, source, target)}


def planted_items(
    source: Catalog, target: Catalog, seed: int = 1, controls: int = 50, per_kind: int = 12
) -> tuple[list[Item], dict[str, int]]:
    """Clean controls and scripted planted errors for one target catalog.

    Returns the items and, per kind, how many strings the perturbation applied to.
    """
    pool = _pool(source, target, seed)
    items = [
        Item(
            f"{target.locale}:{k}:{CONTROL}",
            target.locale,
            k,
            CONTROL,
            source.entries[k].text,
            target.entries[k].text,
            target.entries[k].text,
        )
        for k in pool[:controls]
    ]
    applicable: dict[str, int] = {}
    for kind, func in PERTURBATIONS.items():
        made = 0
        for key in pool:
            src, orig = source.entries[key].text, target.entries[key].text
            item_id = f"{target.locale}:{key}:{kind}"
            result = func(src, orig, target.locale, _rng(seed, item_id))
            # A planted error must not trip a deterministic check the clean string passes.
            if (
                result is None
                or result[0] == orig
                or not _det_kinds(target.locale, key, src, result[0]) <= _det_kinds(target.locale, key, src, orig)
            ):
                continue
            applicable[kind] = applicable.get(kind, 0) + 1
            if made < per_kind:
                items.append(Item(item_id, target.locale, key, kind, src, result[0], orig, result[1]))
                made += 1
    return items, applicable


def handwritten_items(path: Path, catalogs: dict[str, Catalog], source: Catalog) -> list[Item]:
    """Items written by hand; each must still match the catalog text it was written against.

    Raises:
        ValueError: a record has an unknown kind or locale, or its ``original`` no longer matches.
    """
    items = []
    for r in read_jsonl(path):
        kind, locale, key = r["kind"], r["locale"], r["key"]
        if kind not in HANDWRITTEN_KINDS or locale not in catalogs or key not in source.entries:
            msg = f"{path.as_posix()}: bad record {locale}:{key}:{kind}"
            raise ValueError(msg)
        if key not in catalogs[locale].entries:
            msg = f"{path.as_posix()}: {locale} has no entry {key!r}"
            raise ValueError(msg)
        if catalogs[locale].entries[key].text != r["original"]:
            msg = f"{path.as_posix()}: {locale}:{key} no longer matches the catalog; rewrite it"
            raise ValueError(msg)
        items.append(
            Item(
                f"{locale}:{key}:{kind}",
                locale,
                key,
                kind,
                source.entries[key].text,
                r["target"],
                r["original"],
                r.get("detail", ""),
                "agent",
                native_checked=False,
            )
        )
    return items


def make(
    source: Catalog,
    targets: list[Catalog],
    out_dir: Path,
    *,
    handwritten: Path | None,
    seed: int = 1,
    controls: int = 50,
    per_kind: int = 12,
) -> dict[str, Any]:
    """Write ``items.jsonl`` and ``make.json`` (counts) under ``out_dir``; return the counts."""
    items: list[Item] = []
    applicable: dict[str, dict[str, int]] = {}
    for target in targets:
        planted, applicable[target.locale] = planted_items(source, target, seed, controls, per_kind)
        items += planted
    if handwritten is not None:
        items += handwritten_items(handwritten, {t.locale: t for t in targets}, source)
    ids = [i.id for i in items]
    if len(ids) != len(set(ids)):
        msg = "duplicate item ids"
        raise ValueError(msg)
    write_jsonl(out_dir / "items.jsonl", (asdict(i) for i in items))
    counts: dict[str, Any] = {"seed": seed, "applicable": applicable, "items": {}}
    for i in items:
        counts["items"].setdefault(i.locale, {}).setdefault(i.kind, 0)
        counts["items"][i.locale][i.kind] += 1
    (out_dir / "make.json").write_text(json.dumps(counts, indent=2) + "\n", encoding="utf-8")
    return counts


# --- running --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunInfo:
    """What the manifest records beyond what the client and prompts know themselves."""

    model_repo: str = ""
    revision: str = ""
    quantisation: str = ""
    image: str = ""
    server_flags: str = ""
    notes: dict[str, str] = field(default_factory=dict)


# Manifest fields that must match for two invocations to be one run.
_SAME_RUN = (
    "items_sha256",
    "model_name",
    "model_repo",
    "revision",
    "quantisation",
    "image",
    "server_version",
    "server_flags",
    "settings",
    "prompts",
    "schemas",
)


def sha256_file(path: Path) -> str:
    """SHA-256 of a file's bytes, as the manifest records for the items file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(
    items_path: Path,
    run_dir: Path,
    client: ModelClient,
    glossary: Glossary,
    info: RunInfo,
    *,
    modes: tuple[str, ...] = MODES,
    concurrency: int = 16,
) -> dict[str, Any]:
    """Ask the model about every item in each mode; write ``manifest.json`` and ``<mode>.jsonl``.

    Each answer is appended as it arrives. A rerun skips item and mode pairs already answered
    and asks again for the failed ones, so a stopped run resumes; it refuses to resume with other
    items, model, prompts or settings than the manifest records. The manifest's ``failed`` counts
    the items whose answer failed in each mode.

    Raises:
        ValueError: the run directory holds a run with different items, model, prompts or settings.
    """
    items = read_items(items_path)
    review_prompt, judge_prompt = load_prompt("review"), load_prompt("direct-judge")
    run_dir.mkdir(parents=True, exist_ok=True)
    client.log_path = run_dir / "calls.jsonl"
    manifest = {
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "model_name": client.model,
        "model_repo": info.model_repo,
        "revision": info.revision,
        "quantisation": info.quantisation,
        "image": info.image,
        "server_version": client.server_version,
        "server_flags": info.server_flags,
        "settings": {
            "temperature": client.settings.temperature,
            "seed": client.settings.seed,
            "max_tokens": client.settings.max_tokens,
            "constraint": client.settings.constraint,
            "extra_body": client.settings.extra_body,
        },
        "prompts": {p.name: p.sha256 for p in (review_prompt, judge_prompt)},
        "schemas": {
            s.__name__: hashlib.sha256(json.dumps(s.model_json_schema(), sort_keys=True).encode()).hexdigest()
            for s in (ModelReview, Verdict)
        },
        "items_sha256": sha256_file(items_path),
        "n_items": len(items),
        "modes": list(modes),
        "notes": info.notes,
    }
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        earlier = json.loads(manifest_path.read_text(encoding="utf-8"))
        if differing := [k for k in _SAME_RUN if earlier.get(k) != manifest[k]]:
            msg = f"{run_dir.as_posix()} holds another run: {', '.join(differing)} differ; use a new --out"
            raise ValueError(msg)
        manifest = earlier | {"resumed": [*earlier.get("resumed", []), manifest["date"]]}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    failed: dict[str, int] = {}
    for mode in modes:
        out = run_dir / f"{mode}.jsonl"
        done = {r["id"] for r in read_jsonl(out) if not r["failure"]} if out.exists() else set()
        todo = [i for i in items if i.id not in done]

        def ask(item: Item, mode: str = mode) -> dict[str, Any]:
            if mode == "review":
                r = review_entry(client, review_prompt, glossary, item.locale, item.key, item.source, item.target)
                return {
                    "id": item.id,
                    "flagged": bool(r.findings),
                    "kinds": sorted({str(f.kind) for f in r.findings}),
                    "rationale": r.rationale,
                    "failure": r.failure,
                }
            correct, rationale, failure = judge_entry(
                client, judge_prompt, item.locale, item.key, item.source, item.target
            )
            return {"id": item.id, "flagged": correct is False, "kinds": [], "rationale": rationale, "failure": failure}

        failed[mode] = 0
        for record in iter_concurrently(ask, todo, concurrency):
            append_jsonl(out, record)
            failed[mode] += bool(record["failure"])
    return manifest | {"failed": failed}


# --- scoring --------------------------------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for ``k`` successes in ``n`` trials (95% by default)."""
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    # The bounds are exactly 0 and 1 at the extremes; rounding would leave 1e-17 off.
    return (0.0 if k == 0 else centre - half, 1.0 if k == n else centre + half)


@dataclass(frozen=True)
class Cell:
    """Flags among the answered items of one kind: ``k`` of ``n``, with items that failed counted apart."""

    k: int
    n: int
    failed: int = 0
    kind_match: int = 0
    """Flags that also carry the matching model kind (review mode only)."""

    @property
    def rate(self) -> float:
        """Share flagged."""
        return self.k / self.n if self.n else math.nan

    @property
    def interval(self) -> tuple[float, float]:
        """95% Wilson interval."""
        return wilson(self.k, self.n)


def score_run(
    items: list[Item],
    run_dir: Path,
    mode: str,
    locales: Iterable[str] | None = None,
    items_sha: str | None = None,
) -> dict[tuple[str, str], Cell]:
    """Cells keyed by ``(kind, locale)`` for one run and mode; ``locale`` ``all`` sums the locales.

    The last answer per item counts, so a retried item replaces its failure.

    Raises:
        ValueError: the run was made on other items than ``items_sha`` or answers an unknown item.
    """
    by_id = {i.id: i for i in items}
    manifest = run_dir / "manifest.json"
    if (
        items_sha
        and manifest.exists()
        and json.loads(manifest.read_text(encoding="utf-8"))["items_sha256"] != items_sha
    ):
        msg = f"{run_dir.as_posix()} was run on other items (items_sha256 differs)"
        raise ValueError(msg)
    answers = {r["id"]: r for r in read_jsonl(run_dir / f"{mode}.jsonl")}
    counts: dict[tuple[str, str], list[int]] = {}
    for item_id, a in answers.items():
        if item_id not in by_id:
            msg = f"{run_dir.as_posix()}/{mode}.jsonl answers unknown item {item_id!r}"
            raise ValueError(msg)
        item = by_id[item_id]
        if locales is not None and item.locale not in set(locales):
            continue
        for loc in (item.locale, "all"):
            c = counts.setdefault((item.kind, loc), [0, 0, 0, 0])
            if a["failure"]:
                c[2] += 1
                continue
            c[1] += 1
            c[0] += bool(a["flagged"])
            c[3] += bool(a["flagged"]) and MODEL_KIND.get(item.kind) in a["kinds"]
    return {k: Cell(c[0], c[1], c[2], c[3]) for k, c in counts.items()}


def _fmt(cell: Cell | None, *, kind_match: bool = False) -> str:
    if cell is None or cell.n == 0:
        return "n/a"
    lo, hi = cell.interval
    text = f"{cell.k}/{cell.n} = {cell.rate:.0%} [{lo:.0%}-{hi:.0%}]"
    if kind_match:
        text += f"; kind {cell.kind_match}"
    return text + (f"; {cell.failed} failed" if cell.failed else "")


def run_labels(paths: list[Path]) -> list[str]:
    """Column labels: the directory name, or the whole path when two runs share a name."""
    names = [p.name for p in paths]
    return [n if names.count(n) == 1 else p.as_posix() for n, p in zip(names, paths, strict=True)]


def warn_if_not_ignored(path: Path) -> None:
    """Warn on stderr when ``path`` is inside a git repository and not ignored: the data is private."""
    here = path.resolve()
    probe = next((p for p in (here, *here.parents) if p.exists()), here)
    git = ["git", "-C", probe.as_posix()]
    if subprocess.run([*git, "rev-parse"], capture_output=True, check=False).returncode:  # noqa: S603
        return  # not in a repository
    if subprocess.run([*git, "check-ignore", "-q", here.as_posix()], check=False).returncode == 1:  # noqa: S603
        sys.stderr.write(f"warning: {here.as_posix()} is not ignored by git; benchmark data is private\n")


def table(items: list[Item], runs: dict[str, Path], locale: str = "all", items_sha: str | None = None) -> str:
    """Markdown table: rows are planted kinds, then flags on unperturbed strings; columns are run and mode."""
    columns: list[tuple[str, str]] = []
    cells: dict[tuple[str, str], dict[tuple[str, str], Cell]] = {}
    for name, path in runs.items():
        for mode in MODES:
            if (path / f"{mode}.jsonl").exists():
                columns.append((name, mode))
                cells[name, mode] = score_run(items, path, mode, items_sha=items_sha)
    head = "| Planted kind | " + " | ".join(f"{n} {m}" for n, m in columns) + " |"
    rows = [head, "|---|" + "---|" * len(columns)]
    for kind in (*KINDS, CONTROL):
        label = "flags on unperturbed strings" if kind == CONTROL else kind
        rows.append(
            f"| {label} | "
            + " | ".join(
                _fmt(cells[c].get((kind, locale)), kind_match=c[1] == "review" and kind != CONTROL) for c in columns
            )
            + " |"
        )
    return "\n".join(rows)
