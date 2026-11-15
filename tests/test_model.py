import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest

from veriglot import model as model_mod
from veriglot.cli import main
from veriglot.model import (
    ModelClient,
    ModelReview,
    Settings,
    check_endpoint,
    judge_entry,
    load_prompt,
    review_entry,
)
from veriglot.review import Kind

FIXTURES = Path(__file__).parent / "fixtures"
EN = str(FIXTURES / "credit-matrix/en/messages.po")
NL = str(FIXTURES / "credit-matrix/nl/messages.po")
BASE = "http://127.0.0.1:8000/v1"


def answer(content: object, finish: str = "stop") -> dict:
    text = content if isinstance(content, str) else json.dumps(content)
    return {
        "choices": [{"finish_reason": finish, "message": {"content": text}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


class Fake:
    """A transport that returns queued replies and keeps the requests."""

    def __init__(self, *replies: dict | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []

    def __call__(self, url: str, body: dict | None) -> dict:
        self.requests.append({"url": url, "body": body})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def client(fake: Fake, tmp_path: Path, **settings: Any) -> ModelClient:  # noqa: ANN401
    c = ModelClient(BASE, "m", settings=Settings(**settings), log_path=tmp_path / "log.jsonl", transport=fake)
    c.server_version = "0.33.0"
    return c


FINDING = {"kind": "meaning", "severity": "error", "message": "wrong word", "proposed": "Verwijderen"}
GOOD = {"rationale": "The verb is wrong.", "findings": [FINDING]}


def review(c: ModelClient, target: str = "Annuleren"):
    return review_entry(c, load_prompt("review"), model_mod.Glossary(), "nl", "cancel", "Cancel", target)


def test_review_returns_findings_in_the_shared_type(tmp_path: Path):
    r = review(client(Fake(answer(GOOD)), tmp_path))
    (f,) = r.findings
    assert (f.id, f.kind, f.proposed, f.locale) == ("nl:cancel:meaning", Kind.MEANING, "Verwijderen", "nl")
    assert f.fingerprint
    assert r.failure is None
    assert r.rationale == "The verb is wrong."


def test_request_is_constrained_pinned_and_logged(tmp_path: Path):
    fake = Fake(answer(GOOD))
    review(client(fake, tmp_path, seed=5, extra_body={"chat_template_kwargs": {"enable_thinking": False}}))
    body = fake.requests[0]["body"]
    assert fake.requests[0]["url"] == f"{BASE}/chat/completions"
    assert body["structured_outputs"]["json"] == ModelReview.model_json_schema()
    assert not any(k.startswith("guided_") for k in body)
    assert (body["model"], body["seed"], body["temperature"]) == ("m", 5, 0.0)
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    (line,) = (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()
    logged = json.loads(line)
    assert logged["model"] == "m"
    assert logged["server_version"] == "0.33.0"
    assert logged["prompt"] == "review"
    assert logged["prompt_sha256"] == load_prompt("review").sha256
    assert logged["usage"] == {"prompt_tokens": 11, "completion_tokens": 7}
    assert logged["finish_reason"] == "stop"
    assert logged["settings"]["seed"] == 5
    assert logged["tag"] == "nl:cancel"
    assert logged["outcome"] == "ok"
    assert logged["ts"]


def test_response_format_constraint(tmp_path: Path):
    fake = Fake(answer(GOOD))
    review(client(fake, tmp_path, constraint="response_format"))
    body = fake.requests[0]["body"]
    assert body["response_format"]["type"] == "json_schema"
    assert "structured_outputs" not in body


def test_schema_failure_retries_once_then_succeeds(tmp_path: Path):
    fake = Fake(answer({"rationale": "x", "findings": [{"kind": "spelling"}]}), answer(GOOD))
    r = review(client(fake, tmp_path, seed=1))
    assert r.failure is None
    assert [req["body"]["seed"] for req in fake.requests] == [1, 2]
    lines = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(entry["attempt"], entry["outcome"]) for entry in lines] == [(0, "failed"), (1, "ok")]
    assert "schema" in lines[0]["error"]


def test_two_schema_failures_are_recorded_not_raised(tmp_path: Path):
    r = review(client(Fake(answer("not json"), answer("{}")), tmp_path))
    assert r.findings == ()
    assert r.failure
    assert "schema" in r.failure


def test_finish_reason_length_is_a_failure_even_if_the_json_parses(tmp_path: Path):
    fake = Fake(answer(GOOD, finish="length"), answer(GOOD, finish="length"))
    r = review(client(fake, tmp_path))
    assert r.findings == ()
    assert "finish_reason" in (r.failure or "")
    assert len(fake.requests) == 2


def test_transport_error_is_retried_and_recorded(tmp_path: Path):
    r = review(client(Fake(OSError("boom"), OSError("boom")), tmp_path))
    assert "boom" in (r.failure or "")


def test_proposal_that_fails_the_checks_or_repeats_the_target_is_dropped(tmp_path: Path):
    bad = {**FINDING, "proposed": "{name} weg"}  # adds a placeholder the source lacks: apply would reject it
    same = {**FINDING, "kind": "grammar", "proposed": "Annuleren"}
    r = review(client(Fake(answer({"rationale": "r", "findings": [bad, same]})), tmp_path))
    assert [f.proposed for f in r.findings] == [None, None]


def test_findings_of_one_kind_merge_so_ids_stay_unique(tmp_path: Path):
    two = [
        {"kind": "meaning", "severity": "warning", "message": "a", "proposed": None},
        {"kind": "meaning", "severity": "error", "message": "b", "proposed": "Verwijderen"},
    ]
    (f,) = review(client(Fake(answer({"rationale": "r", "findings": two})), tmp_path)).findings
    assert (f.message, f.severity.value, f.proposed) == ("a; b", "error", "Verwijderen")


def test_judge_flags_incorrect_translations(tmp_path: Path):
    c = client(Fake(answer({"rationale": "r", "correct": False})), tmp_path)
    assert judge_entry(c, load_prompt("direct-judge"), "nl", "cancel", "Cancel", "Annuleren") == (False, "r", None)


def test_prompt_is_a_file_with_a_stable_hash_and_values_are_not_rescanned():
    prompt = load_prompt("review")
    assert prompt.sha256 == load_prompt("review").sha256
    text = prompt.render(
        language="Dutch", locale="nl", key="k", context="c", style="s", glossary="g", source="$target", target="t"
    )
    assert "$target" in text


def test_remote_endpoint_needs_a_flag():
    check_endpoint("http://localhost:9/v1", allow_remote=False)
    with pytest.raises(ValueError, match="not a local server"):
        check_endpoint("https://api.example.com/v1", allow_remote=False)
    check_endpoint("https://api.example.com/v1", allow_remote=True)


def test_preflight_names_a_missing_model_and_reads_the_version():
    c = ModelClient(BASE, "m", transport=Fake({"data": [{"id": "other"}]}))
    with pytest.raises(OSError, match="does not serve 'm'"):
        c.preflight()
    ok = ModelClient(BASE, "m", transport=Fake({"data": [{"id": "m"}]}, {"version": "0.33.0"}))
    ok.preflight()
    assert ok.server_version == "0.33.0"


def test_glossary_lists_only_terms_in_the_source(tmp_path: Path):
    path = tmp_path / "g.toml"
    path.write_text('[style]\nnl = "formal"\n[[term]]\nen = "draft"\nnl = "concept"\nnote = "n"\n', encoding="utf-8")
    g = model_mod.Glossary.load(path)
    assert g.for_entry("nl", "Save the Draft") == ("formal", "- draft -> concept (n)")
    assert g.for_entry("de", "Save the draft") == ("none", "- none apply")
    path.write_text('[[term]]\nnl = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="no 'en'"):
        model_mod.Glossary.load(path)


def fake_http(monkeypatch: pytest.MonkeyPatch, *replies: dict) -> Fake:
    fake = Fake({"data": [{"id": "m"}]}, {"version": "9"}, *replies)
    monkeypatch.setattr(model_mod, "_http", lambda url, body, _timeout: fake(url, body))
    return fake


def test_cli_review_model_csv_round_trips_through_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    fake_http(monkeypatch, *[answer(GOOD)] * 20)
    log = tmp_path / "calls.jsonl"
    code = main(["review-model", EN, NL, "--base-url", BASE, "--model", "m", "--log", str(log), "--format", "csv"])
    out = capsys.readouterr().out
    assert code == 1  # error-severity findings
    assert "nl:cancel:meaning" in out
    assert log.read_text(encoding="utf-8")
    sheet = tmp_path / "review.csv"
    target = tmp_path / "messages.po"
    target.write_text(Path(NL).read_text(encoding="utf-8"), encoding="utf-8")
    rows = list(csv.DictReader(io.StringIO(out)))
    keep = [r for r in rows if r["id"] == "nl:cancel:meaning"]
    keep[0]["decision"] = "accept"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, list(keep[0]))
    writer.writeheader()
    writer.writerows(keep)
    sheet.write_text(buf.getvalue(), encoding="utf-8")
    assert main(["apply", str(target), str(sheet), "--source", EN]) == 0
    assert 'msgstr "Verwijderen"' in target.read_text(encoding="utf-8")


def test_cli_schema_failure_exits_three_and_still_prints_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    fake_http(monkeypatch, *[answer("nope")] * 40)
    argv = ["review-model", EN, NL, "--base-url", BASE, "--model", "m", "--log", str(tmp_path / "l.jsonl")]
    assert main([*argv, "--format", "json"]) == 3
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["errors"]
    assert "schema" in captured.err


def test_cli_refuses_a_remote_server_without_the_flag(capsys: pytest.CaptureFixture[str]):
    assert main(["review-model", EN, NL, "--base-url", "https://api.example.com/v1", "--model", "m"]) == 3
    assert "not a local server" in capsys.readouterr().err
