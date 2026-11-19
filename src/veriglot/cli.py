"""The ``veriglot`` command."""

import argparse
import io
import json
import os
import signal
import sys
import webbrowser
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args

from veriglot import bench as bench_mod
from veriglot import model as model_mod
from veriglot.apply import ApplyReport, apply_decisions
from veriglot.catalog import is_read_only, load_catalog
from veriglot.csvio import read_decisions, write_findings
from veriglot.review import KIND_HELP, MODEL_ONLY_KINDS, Finding, Severity, check_catalogs
from veriglot.web import make_server

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from veriglot.catalog import Catalog

SCHEMA_VERSION = 1
DEFAULT_LOG = Path("veriglot-model.log.jsonl")
MAX_PORT = 65535

EPILOG = """\
exit codes:
  0  no error findings (check) or no rejected decisions (apply)
  1  error findings, or rejected decisions
  2  usage error
  3  could not run: unreadable file, bad catalog or decisions CSV, or a model call failed
  130  interrupted (Ctrl-C)
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="veriglot",
        description="Check translation catalogs and apply reviewed corrections.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"veriglot {version('veriglot')}")
    sub = parser.add_subparsers(dest="command", required=True)
    read_only = argparse.ArgumentParser(add_help=False)
    read_only.add_argument(
        "--read-only",
        action="append",
        default=[],
        metavar="GLOB",
        help="treat keys matching GLOB as vendored (repeatable; CRediT role keys always are)",
    )

    check = sub.add_parser(
        "check",
        parents=[read_only],
        help="check target catalogs against a source catalog",
        description="Check target catalogs against a source catalog.",
        epilog="example:\n  veriglot check src/locales/en/messages.po src/locales/*/messages.po --format csv\n\n"
        "`veriglot kinds` lists the finding kinds.\n\n" + EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    check.add_argument("source", type=Path, help="source-language catalog (.po or flat .json)")
    check.add_argument("targets", type=Path, nargs="+", help="target catalogs; the source itself is skipped")
    check.add_argument("--format", choices=("text", "json", "csv"), default="text", help="output format")

    apply = sub.add_parser(
        "apply",
        parents=[read_only],
        help="apply accepted and edited decisions to a target catalog",
        description="Apply accepted and edited decisions from a reviewed findings CSV to a target catalog.",
        epilog="example:\n"
        "  veriglot apply src/locales/nl/messages.po review.csv --source src/locales/en/messages.po\n\n" + EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    apply.add_argument("target", type=Path, help="target catalog to change (.po)")
    apply.add_argument("decisions", help="reviewed findings CSV, or - for stdin")
    apply.add_argument("--source", type=Path, required=True, help="source-language catalog")
    apply.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    apply.add_argument("--format", choices=("text", "json"), default="text", help="output format")

    review = sub.add_parser(
        "review",
        parents=[read_only],
        help="review findings in a local web page and save the decisions",
        description="Serve a local web page on 127.0.0.1 for reviewing findings; saving applies the decisions.",
        epilog="example:\n  veriglot review src/locales/en/messages.po src/locales/nl/messages.po\n\n" + EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    review.add_argument("source", type=Path, help="source-language catalog (.po or flat .json)")
    review.add_argument("targets", type=Path, nargs="+", help="target catalogs; the source itself is skipped")
    review.add_argument("--port", type=int, default=0, metavar="N", help="port to listen on (default: any free port)")
    review.add_argument("--no-browser", action="store_true", help="print the URL without opening a browser")

    model = sub.add_parser(
        "review-model",
        parents=[read_only],
        help="review translations with a language model on a chat-completions server",
        description="Ask a language model to review each translation. Findings use the same kinds, CSV and "
        "apply step as `check`; they are suggestions, not native review. A rerun with the same --log reuses "
        "the answers already there and asks only for the rest; a terminal shows progress on stderr.",
        epilog="example:\n  veriglot review-model src/locales/en/messages.po src/locales/nl/messages.po "
        "--base-url http://127.0.0.1:8000/v1 --model my-model --glossary glossary.toml --format csv\n\n" + EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    model.add_argument("source", type=Path, help="source-language catalog (.po or flat .json)")
    model.add_argument("targets", type=Path, nargs="+", help="target catalogs; the source itself is skipped")
    _model_options(model)
    model.add_argument(
        "--glossary", type=Path, help="TOML glossary: [[term]] with en and locale keys, [style] per locale"
    )
    model.add_argument(
        "--with-checks", action="store_true", help="also run the deterministic checks and merge the findings"
    )
    model.add_argument(
        "--format",
        choices=("text", "json", "jsonl", "csv"),
        default="text",
        help="output format; jsonl prints each finding as it arrives, then a summary line",
    )

    kinds = sub.add_parser(
        "kinds",
        help="list the finding kinds",
        description="List the finding kinds, their severity, and which command reports them.",
        epilog="example:\n  veriglot kinds --format json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    kinds.add_argument("--format", choices=("text", "json"), default="text", help="output format")

    _bench_parser(sub)
    return parser


def _bench_parser(sub: argparse._SubParsersAction) -> None:
    bench = sub.add_parser("bench", help="planted-error benchmark for model review", description=bench_mod.__doc__)
    bench_sub = bench.add_subparsers(dest="bench_command", required=True)
    make = bench_sub.add_parser(
        "make", help="build items: clean controls and planted errors; prints a JSON summary with the items path"
    )
    make.set_defaults(format="json")
    make.add_argument("source", type=Path, help="source-language catalog")
    make.add_argument("targets", type=Path, nargs="+", help="target catalogs of the benchmark languages")
    make.add_argument("--out", type=Path, default=Path("data/bench"), help="output directory (default data/bench)")
    make.add_argument(
        "--handwritten", type=Path, help="JSON Lines of hand-written sense, terminology and register errors"
    )
    make.add_argument("--seed", type=int, default=1)
    make.add_argument("--controls", type=int, default=50, help="clean strings per language (default 50)")
    make.add_argument("--per-kind", type=int, default=12, help="most scripted planted errors per kind and language")
    brun = bench_sub.add_parser(
        "run", help="ask a model about every item, review prompt and direct judge; prints the manifest as JSON"
    )
    brun.set_defaults(format="json")
    brun.add_argument("items", type=Path, help="items.jsonl from `bench make`")
    brun.add_argument("--out", type=Path, required=True, help="run directory (rerun to resume)")
    _model_options(brun)
    brun.add_argument("--glossary", type=Path, help="TOML glossary for the review prompt")
    brun.add_argument(
        "--modes",
        type=_modes,
        default=",".join(bench_mod.MODES),
        help="review, judge or both, comma-separated (default both)",
    )
    for name in ("model-repo", "revision", "quantisation", "image", "server-flags"):
        brun.add_argument(f"--{name}", default="", help=f"recorded in the manifest ({name.replace('-', ' ')})")
    score = bench_sub.add_parser("score", help="recall per kind and language with Wilson 95%% intervals")
    score.add_argument("items", type=Path, help="items.jsonl from `bench make`")
    score.add_argument("runs", type=Path, nargs="*", help="run directories from `bench run`")
    score.add_argument("--locale", default="all", help="language to tabulate (default all)")
    score.add_argument("--qe", type=Path, metavar="DIR", help="MetricX scores directory (scripts/metricx_qe.py output)")
    score.add_argument(
        "--qe-rate",
        type=float,
        action="append",
        metavar="RATE",
        help="share of controls the QE threshold flags, on the controls themselves (repeatable; default 0.07 and 0.10)",
    )
    score.add_argument("--format", choices=("text", "json"), default="text", help="output format")


def _modes(text: str) -> tuple[str, ...]:
    modes = tuple(text.split(","))
    if bad := [m for m in modes if m not in bench_mod.MODES]:
        msg = f"unknown mode {bad[0]!r}; choose from {', '.join(bench_mod.MODES)}"
        raise argparse.ArgumentTypeError(msg)
    return modes


def _model_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base-url", required=True, help="base URL of a chat-completions server, e.g. http://127.0.0.1:8000/v1"
    )
    parser.add_argument("--model", required=True, help="model name the server serves")
    parser.add_argument("--concurrency", type=int, default=8, help="requests at once (default 8)")
    parser.add_argument("--allow-remote", action="store_true", help="allow a base URL that is not on this machine")
    parser.add_argument(
        "--constraint",
        choices=model_mod.CONSTRAINTS,
        default="structured_outputs",
        help="how the server constrains the JSON answer",
    )
    parser.add_argument(
        "--extra-body",
        type=_json_object,
        default={},
        metavar="JSON",
        help='merged into every request, e.g. \'{"chat_template_kwargs": {"enable_thinking": false}}\'',
    )
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--log",
        type=Path,
        help=f"per-call JSON Lines log (default {DEFAULT_LOG}); review-model reuses the answers in a log "
        "given here, and bench run logs into its run directory",
    )


def _json_object(text: str) -> dict[str, object]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"not JSON: {exc}"
        raise argparse.ArgumentTypeError(msg) from exc
    if not isinstance(value, dict):
        msg = "must be a JSON object such as '{}'"
        raise argparse.ArgumentTypeError(msg)
    return value


def _client(args: argparse.Namespace) -> model_mod.ModelClient:
    model_mod.check_endpoint(args.base_url, allow_remote=args.allow_remote)
    settings = model_mod.Settings(args.temperature, args.seed, args.max_tokens, args.constraint, args.extra_body)
    client = model_mod.ModelClient(args.base_url, args.model, settings=settings, log_path=args.log or DEFAULT_LOG)
    client.preflight()
    return client


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command and return its exit code."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", newline="\n")
    args = _parser().parse_args(argv)
    commands = {
        "check": _check,
        "apply": _apply,
        "review": _review,
        "review-model": _review_model,
        "kinds": _kinds,
        "bench": _bench,
    }
    try:
        return commands[args.command](args)
    except KeyboardInterrupt:
        _print_failure(args, "interrupted", RESUME_HELP.get(args.command))
        print("interrupted", file=sys.stderr)
        return _die_of_sigint()
    except (OSError, ValueError, RuntimeError) as exc:
        _print_failure(args, str(exc), None)
        print(f"error: {exc}", file=sys.stderr)
        return 3


def _envelope(errors: Sequence[dict[str, str | None]] = (), **fields: object) -> dict[str, object]:
    """The one JSON shape of every command: ``schema_version``, the command's fields, then ``errors``."""
    return {"schema_version": SCHEMA_VERSION, **fields, "errors": list(errors)}


# The result key of each command's JSON, kept in the envelope of a run that stopped.
RESULT_KEY = {"check": "findings", "review-model": "findings", "apply": "outcomes"}
RESUME_HELP = {
    "review-model": f"rerun with the same --log (default {DEFAULT_LOG}) to reuse the answers so far",
    "bench": "rerun into the same --out to resume",
}


def _print_failure(args: argparse.Namespace, message: str, help_text: str | None) -> None:
    """Print the envelope of a run that stopped before it had results, with the command's empty result key."""
    fmt = getattr(args, "format", None)
    if fmt not in ("json", "jsonl"):
        return
    fields = {RESULT_KEY[args.command]: []} if fmt == "json" and args.command in RESULT_KEY else {}
    print(json.dumps(_envelope([{"message": message, "help": help_text}], **fields), ensure_ascii=False))


def _die_of_sigint() -> int:
    """Die of SIGINT, as the shell expects of an interrupted command (exit 130 where that cannot happen)."""
    sys.stdout.flush()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGINT)
    return 130


def _check(args: argparse.Namespace) -> int:
    source = load_catalog(args.source)
    findings: list[Finding] = []
    for path in args.targets:
        if path.resolve() != args.source.resolve():
            findings += check_catalogs(source, load_catalog(path), tuple(args.read_only))
    return _emit(findings, args.format)


def _emit(findings: list[Finding], fmt: str, errors: Sequence[dict[str, str | None]] = ()) -> int:
    """Print the findings; ``jsonl`` prints only the summary line, as the findings were streamed."""
    counts = {s: sum(f.severity is s for f in findings) for s in Severity}
    if fmt == "json":
        print(json.dumps(_envelope(errors, findings=[f.to_dict() for f in findings]), ensure_ascii=False, indent=2))
    elif fmt == "jsonl":
        summary = _envelope(errors, total=len(findings), counts={str(s): n for s, n in counts.items()})
        print(json.dumps(summary, ensure_ascii=False))
    elif fmt == "csv":
        write_findings(findings, sys.stdout)
    else:
        for f in findings:
            print(f"{f.severity}: {f.locale} {f.key} [{f.kind}] {f.message}")
            print(f"  source: {f.source!r}")
            print(f"  target: {f.target!r}")
            if f.proposed is not None:
                print(f"  proposed: {f.proposed!r}")
    summary = ", ".join(f"{n} {s}" for s, n in counts.items())
    print(f"{len(findings)} findings ({summary})", file=sys.stderr)
    return 3 if errors else 1 if counts[Severity.ERROR] else 0


def _review_model(args: argparse.Namespace) -> int:
    source = load_catalog(args.source)
    glossary = model_mod.Glossary.load(args.glossary) if args.glossary else model_mod.Glossary()
    read_only = tuple(args.read_only)
    targets = _model_targets(args.source, args.targets)
    client = _client(args)
    if args.log is not None:
        client.load_answers()
    prompt = model_mod.load_prompt("review")
    det = {t.locale: check_catalogs(source, t, read_only) if args.with_checks else [] for t in targets}
    seen = {f.id for found in det.values() for f in found}
    entries = [
        (t.locale, k, s.text, e.text, e.context)
        for t in targets
        for k, s in source.entries.items()
        if (e := t.entries.get(k)) and e.text.strip() and not is_read_only(k, read_only)
    ]
    if args.format == "jsonl":
        _print_lines(f for found in det.values() for f in found)
    results, stopped = _ask_all(
        lambda e: model_mod.review_entry(client, prompt, glossary, *e),
        entries,
        args.concurrency,
        stream=args.format == "jsonl",
        seen=seen,
        progress=sys.stderr.isatty() and args.format not in ("json", "jsonl"),
    )
    findings, errors = _collect(targets, det, entries, results, seen)
    if stopped is not None:  # the answers so far still go out, with the stop as the last error
        message = "interrupted" if isinstance(stopped, KeyboardInterrupt) else str(stopped)
        errors.append({"message": message, "help": RESUME_HELP["review-model"]})
    if client.reused:
        print(f"reused {client.reused} answers from {_show(args.log.as_posix())}", file=sys.stderr)
    for e in errors:
        print(f"error: {e['message']}", file=sys.stderr)
    code = _emit(findings, args.format, errors)
    return _die_of_sigint() if isinstance(stopped, KeyboardInterrupt) else code


def _collect(
    targets: list[Catalog],
    det: dict[str, list[Finding]],
    entries: list[_Entry],
    results: dict[_Entry, model_mod.EntryReview],
    seen: set[str],
) -> tuple[list[Finding], list[dict[str, str | None]]]:
    """Findings per target in catalog order, and one error per failed entry; unanswered entries are left out."""
    answered = [e for e in entries if e in results]
    findings: list[Finding] = []
    for t in targets:
        findings += det[t.locale]
        findings += [f for e in answered if e[0] == t.locale for f in results[e].findings if f.id not in seen]
    errors: list[dict[str, str | None]] = [
        {"message": f"{e[0]}:{e[1]}: {results[e].failure}", "help": "see the call log"}
        for e in answered
        if results[e].failure
    ]
    return findings, errors


def _model_targets(source_path: Path, paths: Sequence[Path]) -> list[Catalog]:
    """Load every target before the first call, so a bad one stops the run before it costs anything."""
    targets: list[Catalog] = []
    for path in paths:
        if path.resolve() == source_path.resolve():
            continue
        target = load_catalog(path)
        if target.locale in {t.locale for t in targets}:
            msg = f"two targets for {target.locale}; ids are <locale>:<key>:<kind>, so each locale may appear once"
            raise ValueError(msg)
        targets.append(target)
    return targets


type _Entry = tuple[str, str, str, str, str]


def _ask_all(
    review: Callable[[_Entry], model_mod.EntryReview],
    entries: list[_Entry],
    concurrency: int,
    *,
    stream: bool,
    seen: set[str],
    progress: bool,
) -> tuple[dict[_Entry, model_mod.EntryReview], BaseException | None]:
    """Review every entry; return the answers, and what stopped the run early (Ctrl-C, an error), if anything.

    ``stream`` prints each finding as it arrives, in completion order; ``progress`` shows ``N/M`` on stderr.
    """
    results: dict[_Entry, model_mod.EntryReview] = {}
    stopped = None
    try:
        pairs = model_mod.iter_concurrently(lambda e: (e, review(e)), entries, concurrency)
        for done, (entry, r) in enumerate(pairs, 1):
            results[entry] = r
            if stream:
                _print_lines(f for f in r.findings if f.id not in seen)
            if progress:
                print(f"\rreviewed {done}/{len(entries)}", end="", file=sys.stderr, flush=True)
    except (KeyboardInterrupt, OSError, ValueError, RuntimeError) as exc:
        stopped = exc
    if progress:
        print(file=sys.stderr)
    return results, stopped


def _print_lines(findings: Iterable[Finding]) -> None:
    for f in findings:
        print(json.dumps(f.to_dict(), ensure_ascii=False), flush=True)


def _kinds(args: argparse.Namespace) -> int:
    model_kinds = set(get_args(model_mod.ModelFinding.model_fields["kind"].annotation))
    rows = [
        {
            "kind": str(kind),
            "severity": severity,
            "reported_by": [
                c for c, on in (("check", kind not in MODEL_ONLY_KINDS), ("review-model", kind in model_kinds)) if on
            ],
            "description": finds,
        }
        for kind, (severity, finds) in KIND_HELP.items()
    ]
    if args.format == "json":
        print(json.dumps(_envelope(kinds=rows), indent=2))
    else:
        for r in rows:
            print(f"{r['kind']:<13} {r['severity']:<17} {', '.join(r['reported_by']):<21} {r['description']}")
    return 0


def _bench(args: argparse.Namespace) -> int:
    if args.bench_command == "make":
        counts = bench_mod.make(
            load_catalog(args.source),
            [load_catalog(t) for t in args.targets],
            args.out,
            handwritten=args.handwritten,
            seed=args.seed,
            controls=args.controls,
            per_kind=args.per_kind,
        )
        print(json.dumps(_envelope(path=(args.out / "items.jsonl").as_posix(), **counts), indent=2))
        return 0
    if args.bench_command == "run":
        glossary = model_mod.Glossary.load(args.glossary) if args.glossary else model_mod.Glossary()
        client = _client(args)
        info = bench_mod.RunInfo(args.model_repo, args.revision, args.quantisation, args.image, args.server_flags)
        bench_mod.warn_if_not_ignored(args.out)
        manifest = bench_mod.run(
            args.items, args.out, client, glossary, info, modes=args.modes, concurrency=args.concurrency
        )
        failed = sum(manifest["failed"].values())
        message = f"{failed} answers failed the schema twice"
        errors: list[dict[str, str | None]] = [{"message": message, "help": "rerun to retry them"}] if failed else []
        print(json.dumps(_envelope(errors, run_dir=args.out.as_posix(), **manifest), indent=2))
        if failed:
            print(f"error: {message}; rerun to retry them", file=sys.stderr)
            return 3
        return 0
    if not args.runs and not args.qe:
        _parser().error("bench score: give at least one run directory or --qe")
    return _score(args)


def _score(args: argparse.Namespace) -> int:
    items = bench_mod.read_items(args.items)
    items_sha = bench_mod.sha256_file(args.items)
    runs = dict(zip(bench_mod.run_labels(args.runs), args.runs, strict=True))
    rates = args.qe_rate or [0.07, 0.10]
    scores = bench_mod.read_scores(args.qe, items_sha) if args.qe else None
    if args.format == "json":
        payload: dict[str, Any] = {"locale": args.locale}
        payload["runs"] = [
            {"run": name, "mode": mode, "cells": _cells(cells, args.locale)}
            for (name, mode), cells in bench_mod.score_columns(items, runs, items_sha).items()
        ]
        if scores is not None:
            thresholds = [bench_mod.qe_threshold(items, scores, r) for r in rates]
            payload["qe"] = [
                {"rate": r, "threshold": t, "cells": _cells(bench_mod.qe_cells(items, scores, t), args.locale)}
                for r, t in zip(rates, thresholds, strict=True)
            ]
        print(json.dumps(_envelope(**payload), indent=2))
        return 0
    if runs:
        print(bench_mod.table(items, runs, args.locale, items_sha))
    if scores is not None:
        if runs:
            print()
        print(bench_mod.qe_table(items, scores, rates, args.locale))
    return 0


def _cells(cells: dict[tuple[str, str], bench_mod.Cell], locale: str) -> list[dict[str, Any]]:
    """One row per planted kind, then the controls, as in the text table; kinds without items are left out."""
    kinds = (*bench_mod.KINDS, bench_mod.CONTROL)
    return [{"kind": k, **cells[k, locale].to_dict()} for k in kinds if (k, locale) in cells]


def _review(args: argparse.Namespace) -> int:
    if not 0 <= args.port <= MAX_PORT:
        _parser().error(f"--port must be between 0 and {MAX_PORT}")
    server = make_server(args.source, args.targets, tuple(args.read_only), port=args.port)
    print(server.url, flush=True)
    print("serving on 127.0.0.1; press Ctrl-C to stop", file=sys.stderr)
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


def _apply(args: argparse.Namespace) -> int:
    source = load_catalog(args.source)
    target = load_catalog(args.target)
    if args.decisions == "-":
        # Spreadsheets save CSV with a byte order mark and CRLF; read stdin like a file.
        rows = read_decisions(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8-sig", newline=""))
    else:
        with Path(args.decisions).open(newline="", encoding="utf-8-sig") as f:
            rows = read_decisions(f)
    report = apply_decisions(source, target, rows, read_only=tuple(args.read_only), dry_run=args.dry_run)
    if args.format == "json":
        print(json.dumps(_report_dict(report), ensure_ascii=False, indent=2))
    else:
        for o in report.outcomes:
            print(f"{o.status} {_show(o.id)}: {o.reason or 'ok'}" + (f" ({_show(o.detail)})" if o.detail else ""))
        if args.dry_run:
            done = "dry run, nothing written"
        else:
            done = f"wrote {report.path.as_posix()}" if report.written else "nothing to write"
        skipped = len(report.outcomes) - len(report.applied) - len(report.rejected)
        print(f"applied {len(report.applied)}, rejected {len(report.rejected)}, skipped {skipped}; {done}")
    return 1 if report.rejected else 0


def _report_dict(report: ApplyReport) -> dict[str, object]:
    return _envelope(path=report.path.as_posix(), written=report.written, outcomes=[asdict(o) for o in report.outcomes])


def _show(text: str) -> str:
    """Escape control characters before echoing file content to a terminal."""
    return text if text.isprintable() else repr(text)
