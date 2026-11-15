import csv
import http.client
import io
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from veriglot.web import ReviewServer, make_server

if TYPE_CHECKING:
    from collections.abc import Iterator

FIXTURES = Path(__file__).parent / "fixtures"
EN = FIXTURES / "credit-matrix/en/messages.po"
BROKEN = FIXTURES / "broken/nl/messages.po"


@pytest.fixture
def target(tmp_path: Path) -> Path:
    path = tmp_path / "nl" / "messages.po"
    path.parent.mkdir()
    shutil.copy(BROKEN, path)
    return path


@pytest.fixture
def server(target: Path) -> Iterator[ReviewServer]:
    srv = make_server(EN, [target], port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join()


def call(
    srv: ReviewServer,
    method: str,
    path: str,
    body: object = None,
    *,
    token: str | None = "real",
    host: str | None = None,
    raw: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_port)
    headers = {"Host": host or f"127.0.0.1:{srv.server_port}"}
    if token:
        headers["X-Veriglot-Token"] = srv.token if token == "real" else token
    data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, data, headers)
    resp = conn.getresponse()
    out = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), out


def findings(srv: ReviewServer) -> dict[str, dict]:
    status, _, out = call(srv, "GET", "/api/findings")
    assert status == 200
    return {f["id"]: f for f in json.loads(out)["findings"]}


def test_get_findings_shape(server: ReviewServer):
    _, _, out = call(server, "GET", "/api/findings")
    data = json.loads(out)
    assert data["schema_version"] == 1
    assert data["locales"] == ["nl"]
    assert len(data["findings"]) == 10
    first = data["findings"][0]
    assert {"id", "locale", "key", "kind", "severity", "source", "target", "context", "message", "proposed"} <= set(
        first
    )
    assert first["fingerprint"]


def test_post_decisions_apply_through_apply_decisions(server: ReviewServer, target: Path):
    f = findings(server)["nl:cancel:untranslated"]
    row = {"id": f["id"], "fingerprint": f["fingerprint"], "decision": "edit", "edited": "Annuleren"}
    status, _, out = call(server, "POST", "/api/decisions", {"rows": [row]})
    assert status == 200
    report = json.loads(out)
    assert report["written"] is True
    assert [(o["id"], o["status"]) for o in report["outcomes"]] == [(f["id"], "applied")]
    assert 'msgstr "Annuleren"' in target.read_text(encoding="utf-8")
    assert "nl:cancel:untranslated" not in findings(server)


def test_dry_run_writes_nothing(server: ReviewServer, target: Path):
    before = target.read_bytes()
    f = findings(server)["nl:cancel:untranslated"]
    row = {"id": f["id"], "fingerprint": f["fingerprint"], "decision": "edit", "edited": "Annuleren"}
    _, _, out = call(server, "POST", "/api/decisions", {"rows": [row], "dry_run": True})
    assert json.loads(out)["outcomes"][0]["status"] == "applied"
    assert target.read_bytes() == before


def test_stale_fingerprint_is_rejected(server: ReviewServer, target: Path):
    before = target.read_bytes()
    f = findings(server)["nl:cancel:untranslated"]
    row = {"id": f["id"], "fingerprint": "0" * 16, "decision": "edit", "edited": "Annuleren"}
    _, _, out = call(server, "POST", "/api/decisions", {"rows": [row]})
    outcome = json.loads(out)["outcomes"][0]
    assert (outcome["status"], outcome["reason"]) == ("rejected", "stale")
    assert target.read_bytes() == before


def test_unknown_locale_and_bad_rows(server: ReviewServer):
    row = {"id": "de:cancel:untranslated", "fingerprint": "", "decision": "edit", "edited": "x"}
    _, _, out = call(server, "POST", "/api/decisions", {"rows": [row]})
    assert json.loads(out)["outcomes"][0]["reason"] == "unknown"
    assert call(server, "POST", "/api/decisions", {"rows": [{"id": 3}]})[0] == 400
    assert call(server, "POST", "/api/decisions", raw=b"not json")[0] == 400


@pytest.mark.parametrize(("method", "path"), [("GET", "/api/findings"), ("POST", "/api/decisions")])
def test_missing_or_wrong_token_is_forbidden(server: ReviewServer, target: Path, method: str, path: str):
    before = target.read_bytes()
    assert call(server, method, path, {"rows": []}, token=None)[0] == 403
    assert call(server, method, path, {"rows": []}, token="wrong")[0] == 403
    assert target.read_bytes() == before


@pytest.mark.parametrize("host", ["evil.example", "evil.example:80", "127.0.0.1:1", "localhost"])
def test_bad_host_is_forbidden(server: ReviewServer, host: str):
    assert call(server, "GET", "/api/findings", host=host)[0] == 403
    assert call(server, "GET", "/", host=host, token=None)[0] == 403


def test_localhost_host_is_allowed(server: ReviewServer):
    assert call(server, "GET", "/api/findings", host=f"localhost:{server.server_port}")[0] == 200


def test_oversized_body_is_413(server: ReviewServer, target: Path):
    before = target.read_bytes()
    assert call(server, "POST", "/api/decisions", raw=b"x" * (2 * 1024 * 1024))[0] == 413
    assert target.read_bytes() == before


def test_static_files_and_no_cors(server: ReviewServer):
    status, headers, body = call(server, "GET", "/", token=None)
    assert status == 200
    assert b"<title>" in body
    assert "access-control-allow-origin" not in {k.lower() for k in headers}
    assert call(server, "GET", "/app.js", token=None)[0] == 200
    assert call(server, "GET", "/style.css", token=None)[0] == 200
    assert call(server, "GET", "/../pyproject.toml", token=None)[0] == 404
    assert call(server, "GET", "/__init__.py", token=None)[0] == 404


def test_csv_export_then_import_round_trip(server: ReviewServer):
    f = findings(server)["nl:cancel:untranslated"]
    decisions = [{"id": f["id"], "decision": "edit", "edited": "=Annuleren", "note": "n"}]
    status, headers, out = call(server, "POST", "/api/export", {"decisions": decisions})
    assert status == 200
    assert headers["Content-Type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(out.decode())))
    assert len(rows) == 10
    mine = next(r for r in rows if r["id"] == f["id"])
    assert (mine["decision"], mine["edited"]) == ("edit", "'=Annuleren")
    _, _, imported = call(server, "POST", "/api/import", {"csv": out.decode()})
    back = {r["id"]: r for r in json.loads(imported)["rows"]}
    assert back[f["id"]]["edited"] == "=Annuleren"
    assert back[f["id"]]["decision"] == "edit"
    assert call(server, "POST", "/api/import", {"csv": "a,b\n1,2\n"})[0] == 400


def test_review_command_serves_and_prints_url(target: Path):
    cmd = [sys.executable, "-m", "veriglot", "review", str(EN), str(target), "--no-browser"]
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True) as proc:  # noqa: S603
        assert proc.stdout
        url = proc.stdout.readline().strip()
        try:
            assert url.startswith("http://127.0.0.1:")
            assert "#token=" in url
            host_port = url.split("/")[2]
            conn = http.client.HTTPConnection(host_port)
            conn.request("GET", "/")
            assert conn.getresponse().status == 200
        finally:
            proc.terminate()
