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

from veriglot.apply import ApplyReport, apply_decisions
from veriglot.catalog import load_catalog
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command and return its exit code."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", newline="\n")
    args = _parser().parse_args(argv)
    try:
        return {"check": _check, "apply": _apply, "review": _review}[args.command](args)
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
    if args.format == "json":
        payload = {"schema_version": SCHEMA_VERSION, "findings": [f.to_dict() for f in findings], "errors": []}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.format == "csv":
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
    return 1 if counts[Severity.ERROR] else 0


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
