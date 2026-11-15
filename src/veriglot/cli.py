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
from typing import TYPE_CHECKING

from veriglot import bench as bench_mod
from veriglot import model as model_mod
from veriglot.apply import ApplyReport, apply_decisions
from veriglot.catalog import is_read_only, load_catalog
from veriglot.csvio import read_decisions, write_findings
from veriglot.review import Finding, Severity, check_catalogs
from veriglot.web import make_server

if TYPE_CHECKING:
    from collections.abc import Sequence

SCHEMA_VERSION = 1
MAX_PORT = 65535

EPILOG = """\
exit codes:
  0  no error findings (check) or no rejected decisions (apply)
  1  error findings, or rejected decisions
  2  usage error
  3  could not run: unreadable file, bad catalog or decisions CSV
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
        + EPILOG,
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
        "apply step as `check`; they are suggestions, not native review.",
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
    model.add_argument("--format", choices=("text", "json", "csv"), default="text", help="output format")

    bench = sub.add_parser("bench", help="planted-error benchmark for model review", description=bench_mod.__doc__)
    bench_sub = bench.add_subparsers(dest="bench_command", required=True)
    make = bench_sub.add_parser("make", help="build items: clean controls and planted errors")
    make.add_argument("source", type=Path, help="source-language catalog")
    make.add_argument("targets", type=Path, nargs="+", help="target catalogs of the benchmark languages")
    make.add_argument("--out", type=Path, default=Path("data/bench"), help="output directory (default data/bench)")
    make.add_argument(
        "--handwritten", type=Path, help="JSON Lines of hand-written sense, terminology and register errors"
    )
    make.add_argument("--seed", type=int, default=1)
    make.add_argument("--controls", type=int, default=50, help="clean strings per language (default 50)")
    make.add_argument("--per-kind", type=int, default=12, help="most scripted planted errors per kind and language")
    brun = bench_sub.add_parser("run", help="ask a model about every item, review prompt and direct judge")
    brun.add_argument("items", type=Path, help="items.jsonl from `bench make`")
    brun.add_argument("--out", type=Path, required=True, help="run directory (rerun to resume)")
    _model_options(brun)
    brun.add_argument("--glossary", type=Path, help="TOML glossary for the review prompt")
    brun.add_argument("--modes", default=",".join(bench_mod.MODES), help="review, judge or both (default both)")
    for name in ("model-repo", "revision", "quantisation", "image", "server-flags"):
        brun.add_argument(f"--{name}", default="", help=f"recorded in the manifest ({name.replace('-', ' ')})")
    score = bench_sub.add_parser("score", help="recall per kind and language with Wilson 95%% intervals")
    score.add_argument("items", type=Path, help="items.jsonl from `bench make`")
    score.add_argument("runs", type=Path, nargs="+", help="run directories from `bench run`")
    score.add_argument("--locale", default="all", help="language to tabulate (default all)")
    return parser


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
        default="{}",
        metavar="JSON",
        help='merged into every request, e.g. \'{"chat_template_kwargs": {"enable_thinking": false}}\'',
    )
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--log",
        type=Path,
        default=Path("veriglot-model.log.jsonl"),
        help="per-call JSON Lines log (bench run logs into its run directory)",
    )


def _client(args: argparse.Namespace) -> model_mod.ModelClient:
    model_mod.check_endpoint(args.base_url, allow_remote=args.allow_remote)
    try:
        extra = json.loads(args.extra_body)
    except json.JSONDecodeError as exc:
        msg = f"--extra-body is not JSON: {exc}"
        raise ValueError(msg) from exc
    settings = model_mod.Settings(args.temperature, args.seed, args.max_tokens, args.constraint, extra)
    client = model_mod.ModelClient(args.base_url, args.model, settings=settings, log_path=args.log)
    client.preflight()
    return client


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command and return its exit code."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", newline="\n")
    args = _parser().parse_args(argv)
    try:
        return {"check": _check, "apply": _apply, "review": _review, "review-model": _review_model, "bench": _bench}[
            args.command
        ](args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.stdout.flush()
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGINT)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        if getattr(args, "format", None) == "json":
            print(json.dumps({"schema_version": SCHEMA_VERSION, "errors": [{"message": str(exc), "help": None}]}))
        print(f"error: {exc}", file=sys.stderr)
        return 3


def _check(args: argparse.Namespace) -> int:
    source = load_catalog(args.source)
    findings: list[Finding] = []
    for path in args.targets:
        if path.resolve() != args.source.resolve():
            findings += check_catalogs(source, load_catalog(path), tuple(args.read_only))
    return _emit(findings, args.format)


def _emit(findings: list[Finding], fmt: str, errors: Sequence[dict[str, str | None]] = ()) -> int:
    if fmt == "json":
        payload = {
            "schema_version": SCHEMA_VERSION,
            "findings": [f.to_dict() for f in findings],
            "errors": list(errors),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif fmt == "csv":
        write_findings(findings, sys.stdout)
    else:
        for f in findings:
            print(f"{f.severity}: {f.locale} {f.key} [{f.kind}] {f.message}")
            print(f"  source: {f.source!r}")
            print(f"  target: {f.target!r}")
            if f.proposed is not None:
                print(f"  proposed: {f.proposed!r}")
    counts = {s: sum(f.severity is s for f in findings) for s in Severity}
    summary = ", ".join(f"{n} {s}" for s, n in counts.items())
    print(f"{len(findings)} findings ({summary})", file=sys.stderr)
    return 3 if errors else 1 if counts[Severity.ERROR] else 0


def _review_model(args: argparse.Namespace) -> int:
    source = load_catalog(args.source)
    glossary = model_mod.Glossary.load(args.glossary) if args.glossary else model_mod.Glossary()
    client = _client(args)
    prompt = model_mod.load_prompt("review")
    findings: list[Finding] = []
    errors: list[dict[str, str | None]] = []
    for path in args.targets:
        if path.resolve() == args.source.resolve():
            continue
        target = load_catalog(path)
        entries = [
            (k, s.text, target.entries[k].text, target.entries[k].context)
            for k, s in source.entries.items()
            if k in target.entries and target.entries[k].text.strip() and not is_read_only(k, tuple(args.read_only))
        ]
        results = model_mod.map_concurrently(
            lambda e, t=target: model_mod.review_entry(client, prompt, glossary, t.locale, e[0], e[1], e[2], e[3]),
            entries,
            args.concurrency,
        )
        det = check_catalogs(source, target, tuple(args.read_only)) if args.with_checks else []
        seen = {f.id for f in det}
        findings += det
        for (key, *_), r in zip(entries, results, strict=True):
            findings += [f for f in r.findings if f.id not in seen]
            if r.failure:
                errors.append({"message": f"{target.locale}:{key}: {r.failure}", "help": "see the call log"})
    for e in errors:
        print(f"error: {e['message']}", file=sys.stderr)
    return _emit(findings, args.format, errors)


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
        print(json.dumps(counts["items"], indent=2))
        return 0
    if args.bench_command == "run":
        modes = tuple(args.modes.split(","))
        if bad := [m for m in modes if m not in bench_mod.MODES]:
            msg = f"unknown mode {bad[0]!r}; choose from {', '.join(bench_mod.MODES)}"
            raise ValueError(msg)
        glossary = model_mod.Glossary.load(args.glossary) if args.glossary else model_mod.Glossary()
        client = _client(args)
        info = bench_mod.RunInfo(args.model_repo, args.revision, args.quantisation, args.image, args.server_flags)
        manifest = bench_mod.run(
            args.items, args.out, client, glossary, info, modes=modes, concurrency=args.concurrency
        )
        print(json.dumps(manifest, indent=2))
        return 0
    items = bench_mod.read_items(args.items)
    print(bench_mod.table(items, {p.name: p for p in args.runs}, args.locale))
    return 0


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
    return {
        "schema_version": SCHEMA_VERSION,
        "path": report.path.as_posix(),
        "written": report.written,
        "outcomes": [asdict(o) for o in report.outcomes],
        "errors": [],
    }


def _show(text: str) -> str:
    """Escape control characters before echoing file content to a terminal."""
    return text if text.isprintable() else repr(text)
