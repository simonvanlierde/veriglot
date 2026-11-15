"""A local web UI for reviewing findings: stdlib HTTP server, one static page, a small JSON API.

The server calls ``check_catalogs`` and ``apply_decisions`` like the CLI does and keeps no
review state: catalogs are read again for every request, so edits made elsewhere show up.
It listens on 127.0.0.1 only, accepts only its own ``Host`` values (DNS rebinding), needs a
per-run token on every ``/api`` request and sends no CORS headers.
"""

import hmac
import io
import json
import secrets
import threading
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import TYPE_CHECKING, Any

from veriglot.apply import DecisionRow, Outcome, apply_decisions
from veriglot.catalog import load_catalog
from veriglot.csvio import read_decisions, write_findings
from veriglot.review import Finding, check_catalogs

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA_VERSION = 1
MAX_BODY = 1024 * 1024
TOKEN_HEADER = "X-Veriglot-Token"  # noqa: S105 - a header name, not a secret
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'"
)


class _HttpError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class ReviewServer(ThreadingHTTPServer):
    """Serves one source catalog and its target catalogs on the loopback interface."""

    def __init__(self, source: Path, targets: dict[str, Path], read_only: tuple[str, ...], token: str, port: int):
        super().__init__(("127.0.0.1", port), _Handler)
        self.source, self.targets, self.read_only, self.token = source, targets, read_only, token
        self.lock = threading.Lock()
        self.hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}

    @property
    def url(self) -> str:
        """The address to open: the token rides in the fragment, which browsers never send."""
        return f"http://127.0.0.1:{self.server_port}/#token={self.token}"

    def findings(self) -> list[Finding]:
        """Findings for every target, from the files as they are now."""
        source = load_catalog(self.source)
        return [f for path in self.targets.values() for f in check_catalogs(source, load_catalog(path), self.read_only)]

    def decide(self, rows: list[DecisionRow], *, dry_run: bool) -> tuple[list[Outcome], bool]:
        """Apply rows per locale; rows for a locale without a catalog are rejected as unknown."""
        source = load_catalog(self.source)
        outcomes: dict[str, Outcome] = {}
        written = False
        with self.lock:
            for locale, path in self.targets.items():
                mine = [r for r in rows if r.id.partition(":")[0] == locale]
                report = apply_decisions(source, load_catalog(path), mine, read_only=self.read_only, dry_run=dry_run)
                written |= report.written
                outcomes |= {o.id: o for o in report.outcomes}
        unknown = [Outcome(r.id, "rejected", "unknown", "no catalog for this locale") for r in rows]
        return [outcomes.get(r.id, u) for r, u in zip(rows, unknown, strict=True)], written


def make_server(
    source: Path,
    targets: list[Path],
    read_only: tuple[str, ...] = (),
    *,
    port: int = 0,
    token: str | None = None,
) -> ReviewServer:
    """Load the catalogs (so a bad file fails now) and bind 127.0.0.1:``port`` (0: any free port).

    Raises:
        ValueError: two targets share a locale.
    """
    by_locale: dict[str, Path] = {}
    load_catalog(source)
    for path in targets:
        if path.resolve() == source.resolve():
            continue
        locale = load_catalog(path).locale
        if locale in by_locale:
            msg = f"{path} and {by_locale[locale]} are both locale {locale!r}"
            raise ValueError(msg)
        by_locale[locale] = path
    return ReviewServer(source, by_locale, read_only, token or secrets.token_urlsafe(24), port)


class _Handler(BaseHTTPRequestHandler):
    server: ReviewServer  # ty: ignore[invalid-mutable-override]
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._serve(self._get)

    def do_POST(self) -> None:
        self._serve(self._post)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401 - http.server API
        """Keep the terminal quiet: the URL is the only output."""

    def _serve(self, route: Any) -> None:  # noqa: ANN401
        try:
            path = self._authorize()
            status, ctype, body = route(path)
        except _HttpError as exc:
            status, ctype, body = exc.status, "application/json", _json({"error": str(exc)})
            self.close_connection = True  # an unread request body may still be on the wire
        except (OSError, ValueError, RuntimeError) as exc:
            status, ctype, body = HTTPStatus.INTERNAL_SERVER_ERROR, "application/json", _json({"error": str(exc)})
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _authorize(self) -> str:
        if self.headers.get("Host") not in self.server.hosts:
            raise _HttpError(HTTPStatus.FORBIDDEN, "unexpected Host header")
        path = self.path.split("?", 1)[0]
        sent = self.headers.get(TOKEN_HEADER, "").encode()
        if path.startswith("/api/") and not hmac.compare_digest(sent, self.server.token.encode()):
            raise _HttpError(HTTPStatus.FORBIDDEN, "missing or wrong token")
        return path

    def _get(self, path: str) -> tuple[HTTPStatus, str, bytes]:
        if path in _STATIC:
            name, ctype = _STATIC[path]
            return HTTPStatus.OK, ctype, resources.files("veriglot.web").joinpath(name).read_bytes()
        if path == "/api/findings":
            found = self.server.findings()
            payload = {
                "schema_version": SCHEMA_VERSION,
                "locales": list(self.server.targets),
                "read_only": list(self.server.read_only),
                "findings": [f.to_dict() for f in found],
            }
            return HTTPStatus.OK, "application/json", _json(payload)
        raise _HttpError(HTTPStatus.NOT_FOUND, "not found")

    def _post(self, path: str) -> tuple[HTTPStatus, str, bytes]:
        if path not in {"/api/decisions", "/api/export", "/api/import"}:
            raise _HttpError(HTTPStatus.NOT_FOUND, "not found")
        body = self._body()
        if path == "/api/decisions":
            rows = _rows(body.get("rows"))
            outcomes, written = self.server.decide(rows, dry_run=bool(body.get("dry_run")))
            payload = {"schema_version": SCHEMA_VERSION, "written": written, "outcomes": [asdict(o) for o in outcomes]}
            return HTTPStatus.OK, "application/json", _json(payload)
        if path == "/api/export":
            decisions = {r.id: r for r in _rows(body.get("decisions"), need_fingerprint=False)}
            out = io.StringIO()
            write_findings(self.server.findings(), out, decisions)
            return HTTPStatus.OK, "text/csv; charset=utf-8", out.getvalue().encode()
        text = body.get("csv")
        if not isinstance(text, str):
            raise _HttpError(HTTPStatus.BAD_REQUEST, "csv must be a string")
        try:
            rows = read_decisions(io.StringIO(text.removeprefix(chr(0xFEFF)), newline=""))
        except ValueError as exc:
            raise _HttpError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        return (
            HTTPStatus.OK,
            "application/json",
            _json({"schema_version": SCHEMA_VERSION, "rows": [asdict(r) for r in rows]}),
        )

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise _HttpError(HTTPStatus.LENGTH_REQUIRED, "Content-Length required") from None
        if length > MAX_BODY:
            raise _HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"body over {MAX_BODY} bytes")
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError as exc:  # includes UnicodeDecodeError
            raise _HttpError(HTTPStatus.BAD_REQUEST, "body is not JSON") from exc
        if not isinstance(body, dict):
            raise _HttpError(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
        return body


def _rows(raw: object, *, need_fingerprint: bool = True) -> list[DecisionRow]:
    if not isinstance(raw, list):
        raise _HttpError(HTTPStatus.BAD_REQUEST, "rows must be a list")
    rows = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise _HttpError(HTTPStatus.BAD_REQUEST, "each row needs a string id")
        fields = {k: item.get(k) for k in ("fingerprint", "decision", "proposed", "edited", "note")}
        text_ok = all(v is None or isinstance(v, str) for v in fields.values())
        if not text_ok or (need_fingerprint and not isinstance(fields["fingerprint"], str)):
            raise _HttpError(HTTPStatus.BAD_REQUEST, "row fields must be strings")
        rows.append(
            DecisionRow(
                id=item["id"],
                fingerprint=fields["fingerprint"] or "",
                decision=fields["decision"] or "",
                proposed=fields["proposed"],
                edited=fields["edited"],
                note=fields["note"] or "",
            )
        )
    return rows


def _json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode()
