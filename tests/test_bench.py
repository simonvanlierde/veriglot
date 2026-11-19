import json
import math
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from veriglot import bench
from veriglot import model as model_mod
from veriglot.canary import CANARY_GUID
from veriglot.catalog import Catalog, load_catalog
from veriglot.cli import main

if TYPE_CHECKING:
    import random
    from collections.abc import Callable

BASE = "http://127.0.0.1:8000/v1"


def ok[T](result: T | None) -> T:
    assert result is not None
    return result


def rng() -> random.Random:
    return bench._rng(1, "x")  # noqa: SLF001


SRC = "The file is too large. Choose a file smaller than 14 MB and open it in Word again."


def test_wilson_matches_reference_values():
    lo, hi = bench.wilson(8, 10)
    assert (lo, hi) == pytest.approx((0.4902, 0.9433), abs=1e-4)  # standard Wilson 95% for 8/10
    assert bench.wilson(0, 10) == pytest.approx((0.0, 0.2775), abs=1e-4)
    assert bench.wilson(10, 10)[1] == 1.0
    assert all(math.isnan(x) for x in bench.wilson(0, 0))


@given(st.integers(0, 200).flatmap(lambda n: st.tuples(st.integers(0, n), st.just(n))))
def test_wilson_interval_contains_the_rate(kn: tuple[int, int]):
    k, n = kn
    lo, hi = bench.wilson(k, n)
    if n:
        assert 0 <= lo <= k / n <= hi <= 1


def test_omission_drops_words_but_never_placeholders_or_the_last_word():
    target = "Dit bestand is te groot en {limit} wordt niet geaccepteerd, kies een kleiner bestand."
    new, _ = ok(bench.omission(SRC, target, "nl", rng()))
    assert len(new.split()) < len(target.split())
    assert new.endswith("bestand.")
    assert "{limit}" in new
    assert bench.omission(SRC, "Te kort om weg", "nl", rng()) is None


def test_addition_adds_a_clause_before_the_final_punctuation():
    new, _ = ok(bench.addition(SRC, "Dit bestand is te groot.", "nl", rng()))
    assert new.startswith("Dit bestand is te groot en ")
    assert new.endswith(".")
    assert bench.addition(SRC, "x", "xx", rng()) is None


@pytest.mark.parametrize(
    ("locale", "target", "expected"),
    [
        ("nl", "Die XML kon niet worden gelezen.", "Die XML kon worden gelezen."),
        ("nl", "Deze naam is te lang.", "Deze naam is niet te lang."),
        ("de", "Das ist keine gültige ID. Dies wurde nicht gelesen.", "Das ist keine gültige ID. Dies wurde gelesen."),
        ("de", "Die Datei ist zu groß.", "Die Datei ist nicht zu groß."),
        ("fr", "Ce nom est trop long.", "Ce nom n'est pas trop long."),
    ],
)
def test_negation_flips_a_negation(locale: str, target: str, expected: str):
    assert ok(bench.negation(SRC, target, locale, rng()))[0] == expected
    assert bench.negation(SRC, "Voeg toe.", "nl", rng()) is None


def test_number_changes_a_number_outside_placeholders():
    new, detail = ok(bench.number(SRC, "Maximaal 14 MB voor {count} bestanden", "nl", rng()))
    assert "14" not in new
    assert "{count}" in new
    assert detail.startswith("14 -> ")
    assert bench.number(SRC, "Geen cijfers, alleen {n}", "nl", rng()) is None
    assert bench.number(SRC, "Versie 2.5", "nl", rng()) is None  # decimals are left alone


def test_entity_swaps_only_a_name_both_texts_carry():
    assert ok(bench.entity("Open in Word", "Open in Word", "nl", rng()))[0] == "Open in Excel"
    assert bench.entity("Open it", "Open in Word", "nl", rng()) is None


def test_untranslated_leaves_source_words_in_the_target():
    src = "Copy the statement above into your document before you close this page"
    new, _ = ok(
        bench.untranslated(
            src, "Kopieer de verklaring hierboven naar uw document voordat u deze pagina sluit", "nl", rng()
        )
    )
    assert any(
        w in new.split()
        for w in ("Copy", "the", "statement", "above", "into", "your", "document", "before", "you", "close")
    )
    assert new != src


def test_perturbations_are_deterministic_for_a_seed():
    target = "Dit bestand is te groot. Kies een bestand kleiner dan 14 MB en open het opnieuw in Word."
    for func in bench.PERTURBATIONS.values():
        a = func(SRC, target, "nl", bench._rng(3, "nl:k:x"))  # noqa: SLF001
        b = func(SRC, target, "nl", bench._rng(3, "nl:k:x"))  # noqa: SLF001
        assert a == b


type Cats = tuple[Catalog, Catalog]
PO = 'msgid ""\nmsgstr ""\n"Language: {loc}\\n"\n\n'
EN_PO = PO.format(loc="en") + "".join(
    f'msgid "k{i}"\nmsgstr "This file is too large for the grid number {i}. Open it in Word and try again."\n\n'
    for i in range(8)
)
NL_PO = PO.format(loc="nl") + "".join(
    f'msgid "k{i}"\nmsgstr "Dit bestand is te groot voor het raster {i}. Open het in Word en probeer het opnieuw."\n\n'
    for i in range(8)
)


@pytest.fixture
def catalogs(tmp_path: Path) -> Cats:
    (tmp_path / "en.po").write_text(EN_PO, encoding="utf-8")
    (tmp_path / "nl.po").write_text(NL_PO, encoding="utf-8")
    return load_catalog(tmp_path / "en.po"), load_catalog(tmp_path / "nl.po")


def test_make_writes_canaried_items_with_controls_and_planted_errors(tmp_path: Path, catalogs: Cats):
    en, nl = catalogs
    counts = bench.make(en, [nl], tmp_path / "out", handwritten=None, controls=3, per_kind=2)
    first = (tmp_path / "out/items.jsonl").read_text(encoding="utf-8").splitlines()[0]
    assert CANARY_GUID in first
    items = bench.read_items(tmp_path / "out/items.jsonl")
    assert counts["items"]["nl"]["clean"] == 3
    assert {i.kind for i in items} >= {"clean", "omission", "addition", "negation", "entity"}
    assert all(i.target != i.original for i in items if i.kind != "clean")
    assert all(i.target == i.original for i in items if i.kind == "clean")
    # same seed, same items
    again = bench.make(en, [nl], tmp_path / "out2", handwritten=None, controls=3, per_kind=2)
    assert again == counts
    assert (tmp_path / "out/items.jsonl").read_bytes() == (tmp_path / "out2/items.jsonl").read_bytes()


def test_read_jsonl_demands_the_canary(tmp_path: Path):
    (tmp_path / "x.jsonl").write_text('{"id": 1}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="canary"):
        bench.read_jsonl(tmp_path / "x.jsonl")


def test_handwritten_items_must_match_the_catalog(tmp_path: Path, catalogs: Cats):
    en, nl = catalogs
    good = {"locale": "nl", "key": "k1", "kind": "sense", "original": nl.entries["k1"].text, "target": "x"}
    bench.write_jsonl(tmp_path / "h.jsonl", [good])
    (item,) = bench.handwritten_items(tmp_path / "h.jsonl", {"nl": nl}, en)
    assert (item.id, item.author, item.native_checked) == ("nl:k1:sense", "agent", False)
    bench.write_jsonl(tmp_path / "h.jsonl", [{**good, "original": "changed"}])
    with pytest.raises(ValueError, match="no longer matches"):
        bench.handwritten_items(tmp_path / "h.jsonl", {"nl": nl}, en)


def judge_reply(*, correct: bool) -> dict:
    content = json.dumps({"rationale": "r", "correct": correct})
    return {"choices": [{"finish_reason": "stop", "message": {"content": content}}], "usage": {}}


def review_reply(kinds: list[str]) -> dict:
    findings = [{"kind": k, "severity": "error", "message": "m", "proposed": None} for k in kinds]
    content = json.dumps({"rationale": "r", "findings": findings})
    return {"choices": [{"finish_reason": "stop", "message": {"content": content}}], "usage": {}}


def scripted_client():
    """A fake model that flags exactly the strings it can tell are planted (a longer target means addition)."""

    def transport(url: str, body: dict | None) -> dict:
        if body is None:
            return {"data": [{"id": "m"}]} if url.endswith("/models") else {"version": "9"}
        prompt = body["messages"][0]["content"]
        planted = "en dit geldt ook" in prompt
        if "Is the translation correct" in prompt:
            return judge_reply(correct=not planted)
        return review_reply(["addition"] if planted else [])

    return model_mod.ModelClient(BASE, "m", transport=transport)


def test_run_and_score_per_kind(tmp_path: Path, catalogs: Cats):
    en, nl = catalogs
    bench.make(en, [nl], tmp_path / "data", handwritten=None, controls=4, per_kind=3)
    client = scripted_client()
    client.preflight()
    info = bench.RunInfo(model_repo="org/m", revision="abc", quantisation="awq", image="img:1")
    manifest = bench.run(
        tmp_path / "data/items.jsonl", tmp_path / "run", client, model_mod.Glossary(), info, concurrency=1
    )
    assert manifest["revision"] == "abc"
    assert manifest["prompts"].keys() == {"review", "direct-judge"}
    assert manifest["server_version"] == "9"
    assert manifest["n_items"] == len(bench.read_items(tmp_path / "data/items.jsonl"))
    items = bench.read_items(tmp_path / "data/items.jsonl")
    for mode in ("review", "judge"):
        cells = bench.score_run(items, tmp_path / "run", mode)
        add = cells["addition", "nl"]
        assert (add.k, add.n) == (3, 3)
        assert cells["omission", "nl"].k == 0
        assert cells["clean", "all"].k == 0
        assert cells["clean", "all"].n == 4
    review = bench.score_run(items, tmp_path / "run", "review")
    assert review["addition", "nl"].kind_match == 3
    text = bench.table(items, {"m": tmp_path / "run"})
    assert "flags on unperturbed strings" in text
    assert "3/3 = 100%" in text


def test_rerun_resumes_without_asking_again(tmp_path: Path, catalogs: Cats):
    en, nl = catalogs
    bench.make(en, [nl], tmp_path / "data", handwritten=None, controls=2, per_kind=1)
    calls = []

    def transport(url: str, _body: dict | None) -> dict:
        calls.append(url)
        return judge_reply(correct=True)

    client = model_mod.ModelClient(BASE, "m", transport=transport)
    args = (tmp_path / "data/items.jsonl", tmp_path / "run", client, model_mod.Glossary(), bench.RunInfo())
    bench.run(*args, modes=("judge",), concurrency=1)
    first = len(calls)
    bench.run(*args, modes=("judge",), concurrency=1)
    assert len(calls) == first


def test_failed_items_leave_the_denominator(tmp_path: Path, catalogs: Cats):
    en, nl = catalogs
    bench.make(en, [nl], tmp_path / "data", handwritten=None, controls=2, per_kind=1)

    def transport(_url: str, _body: dict | None) -> dict:
        return {"choices": [{"finish_reason": "length", "message": {"content": "{"}}], "usage": {}}

    client = model_mod.ModelClient(BASE, "m", transport=transport)
    bench.run(
        tmp_path / "data/items.jsonl", tmp_path / "run", client, model_mod.Glossary(), bench.RunInfo(), modes=("judge",)
    )
    cell = bench.score_run(bench.read_items(tmp_path / "data/items.jsonl"), tmp_path / "run", "judge")["clean", "nl"]
    assert (cell.k, cell.n, cell.failed) == (0, 0, 2)


def test_cli_bench_make_and_score(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    (tmp_path / "en.po").write_text(EN_PO, encoding="utf-8")
    (tmp_path / "nl.po").write_text(NL_PO, encoding="utf-8")
    out = tmp_path / "bench"
    assert (
        main(["bench", "make", str(tmp_path / "en.po"), str(tmp_path / "nl.po"), "--out", str(out), "--controls", "2"])
        == 0
    )
    made = json.loads(capsys.readouterr().out)
    assert (made["schema_version"], made["errors"]) == (1, [])
    assert made["items"]["nl"]["clean"] == 2
    assert Path(made["path"]) == out / "items.jsonl"
    assert Path(made["path"]).exists()


def test_untranslated_needs_enough_source_words_for_the_span():
    target = " ".join(f"woord{i}" for i in range(20))  # a span of 6 words
    assert bench.untranslated("one two three four five six", target, "nl", rng()) is None
    new, _ = ok(bench.untranslated(" ".join(f"w{i}" for i in range(12)), target, "nl", rng()))
    assert len(new.split()) == 20


def test_handwritten_item_for_a_key_missing_from_the_target_is_a_clear_error(tmp_path: Path, catalogs: Cats):
    en, nl = catalogs
    en_only = en.entries["k1"]
    bench.write_jsonl(
        tmp_path / "h.jsonl", [{"locale": "nl", "key": "k1", "kind": "sense", "original": "x", "target": "y"}]
    )
    del nl.entries["k1"]
    assert en_only
    with pytest.raises(ValueError, match="no entry"):
        bench.handwritten_items(tmp_path / "h.jsonl", {"nl": nl}, en)


def _make(tmp_path: Path, catalogs: Cats) -> Path:
    en, nl = catalogs
    bench.make(en, [nl], tmp_path / "data", handwritten=None, controls=2, per_kind=1)
    return tmp_path / "data/items.jsonl"


def _client(transport: Callable[[str, dict | None], dict], model: str = "m") -> model_mod.ModelClient:
    return model_mod.ModelClient(BASE, model, transport=transport)


def test_answers_are_written_as_they_arrive(tmp_path: Path, catalogs: Cats):
    items = _make(tmp_path, catalogs)
    calls = []

    def transport(_url: str, _body: dict | None) -> dict:
        calls.append(1)
        if len(calls) > 3:
            raise KeyboardInterrupt
        return judge_reply(correct=True)

    with pytest.raises(KeyboardInterrupt):
        bench.run(
            items,
            tmp_path / "run",
            _client(transport),
            model_mod.Glossary(),
            bench.RunInfo(),
            modes=("judge",),
            concurrency=1,
        )
    assert len(bench.read_jsonl(tmp_path / "run/judge.jsonl")) == 3


def test_resume_retries_failed_items_and_the_run_reports_them(tmp_path: Path, catalogs: Cats):
    items = _make(tmp_path, catalogs)

    def failing(_url: str, _body: dict | None) -> dict:
        return {"choices": [{"finish_reason": "length", "message": {"content": "{"}}], "usage": {}}

    args = (items, tmp_path / "run")
    first = bench.run(*args, _client(failing), model_mod.Glossary(), bench.RunInfo(), modes=("judge",))
    assert first["failed"] == {"judge": first["n_items"]}

    def good(_url: str, _body: dict | None) -> dict:
        return judge_reply(correct=True)

    second = bench.run(*args, _client(good), model_mod.Glossary(), bench.RunInfo(), modes=("judge",))
    assert second["failed"] == {"judge": 0}
    cell = bench.score_run(bench.read_items(items), tmp_path / "run", "judge")["clean", "nl"]
    assert (cell.n, cell.failed) == (2, 0)


def test_resume_refuses_a_different_model_or_items(tmp_path: Path, catalogs: Cats):
    items = _make(tmp_path, catalogs)

    def good(_url: str, _body: dict | None) -> dict:
        return judge_reply(correct=True)

    run_args = (model_mod.Glossary(), bench.RunInfo())
    bench.run(items, tmp_path / "run", _client(good), *run_args, modes=("judge",))
    bench.run(items, tmp_path / "run", _client(good), *run_args, modes=("judge",))  # same run: fine
    with pytest.raises(ValueError, match="model_name"):
        bench.run(items, tmp_path / "run", _client(good, "other"), *run_args, modes=("judge",))
    manifest = json.loads((tmp_path / "run/manifest.json").read_text(encoding="utf-8"))
    assert manifest["model_name"] == "m"  # not overwritten
    items.write_bytes(items.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="items_sha256"):
        bench.run(items, tmp_path / "run", _client(good), *run_args, modes=("judge",))


def test_score_refuses_other_items_and_unknown_ids(tmp_path: Path, catalogs: Cats):
    items = _make(tmp_path, catalogs)

    def good(_url: str, _body: dict | None) -> dict:
        return judge_reply(correct=True)

    bench.run(items, tmp_path / "run", _client(good), model_mod.Glossary(), bench.RunInfo(), modes=("judge",))
    loaded = bench.read_items(items)
    with pytest.raises(ValueError, match="items_sha256"):
        bench.score_run(loaded, tmp_path / "run", "judge", items_sha="0" * 64)
    with pytest.raises(ValueError, match="unknown item"):
        bench.score_run(loaded[:1], tmp_path / "run", "judge")


def test_runs_with_the_same_directory_name_get_distinct_columns(tmp_path: Path, catalogs: Cats):
    items = _make(tmp_path, catalogs)

    def good(_url: str, _body: dict | None) -> dict:
        return judge_reply(correct=True)

    for parent in ("a", "b"):
        bench.run(
            items, tmp_path / parent / "run", _client(good), model_mod.Glossary(), bench.RunInfo(), modes=("judge",)
        )
    labels = bench.run_labels([tmp_path / "a/run", tmp_path / "b/run"])
    assert len(set(labels)) == 2


def test_cli_bench_run_exits_nonzero_when_an_answer_failed(
    tmp_path: Path, catalogs: Cats, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    items = _make(tmp_path, catalogs)
    replies = [{"data": [{"id": "m"}]}, {"version": "1"}]

    def http(_url: str, body: dict | None, _timeout: float) -> dict:
        if replies:
            return replies.pop(0)
        assert body
        return {"choices": [{"finish_reason": "length", "message": {"content": "{"}}], "usage": {}}

    monkeypatch.setattr(model_mod, "_http", http)
    argv = ["bench", "run", str(items), "--out", str(tmp_path / "run"), "--base-url", BASE, "--model", "m"]
    assert main([*argv, "--modes", "judge"]) == 3
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1
    assert data["failed"] == {"judge": data["n_items"]}
    assert data["errors"][0]["message"] == f"{data['n_items']} answers failed the schema twice"
    assert data["run_dir"] == (tmp_path / "run").as_posix()


def test_cli_bench_run_unknown_mode_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    argv = ["bench", "run", str(tmp_path / "i.jsonl"), "--out", str(tmp_path), "--base-url", BASE, "--model", "m"]
    with pytest.raises(SystemExit) as exc:
        main([*argv, "--modes", "review,jduge"])
    assert exc.value.code == 2
    assert "unknown mode 'jduge'" in capsys.readouterr().err


def test_cli_bench_score_without_runs_or_qe_is_a_usage_error(tmp_path: Path):
    with pytest.raises(SystemExit) as exc:
        main(["bench", "score", str(tmp_path / "i.jsonl")])
    assert exc.value.code == 2


def test_cli_bench_score_json(tmp_path: Path, catalogs: Cats, capsys: pytest.CaptureFixture[str]):
    items = _make(tmp_path, catalogs)

    def good(_url: str, _body: dict | None) -> dict:
        return judge_reply(correct=True)

    bench.run(items, tmp_path / "run", _client(good), model_mod.Glossary(), bench.RunInfo(), modes=("judge",))
    qe = tmp_path / "qe"
    bench.write_jsonl(qe / "scores.jsonl", ({"id": i.id, "score": 1.0} for i in bench.read_items(items)))
    argv = ["bench", "score", str(items), str(tmp_path / "run"), "--qe", str(qe), "--qe-rate", "0.5"]
    assert main([*argv, "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["schema_version"], data["errors"], data["locale"]) == (1, [], "all")
    (column,) = data["runs"]
    assert (column["run"], column["mode"]) == ("run", "judge")
    clean = next(c for c in column["cells"] if c["kind"] == "clean")
    assert (clean["k"], clean["n"], clean["rate"]) == (0, 2, 0.0)
    assert clean["interval"][0] == 0.0
    assert data["qe"][0]["rate"] == 0.5
    assert data["qe"][0]["cells"]


def test_bench_run_warns_when_the_output_is_not_git_ignored(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603, S607
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    bench.warn_if_not_ignored(tmp_path / "ignored" / "run")
    assert capsys.readouterr().err == ""
    bench.warn_if_not_ignored(tmp_path / "tracked" / "run")
    assert "not ignored by git" in capsys.readouterr().err


def _qe_items() -> tuple[list[bench.Item], dict[str, float]]:
    items = [bench.Item(f"nl:c{i}:clean", "nl", f"c{i}", "clean", "s", "t", "t") for i in range(10)]
    items += [bench.Item(f"nl:p{i}:omission", "nl", f"p{i}", "omission", "s", "t", "t") for i in range(4)]
    scores = {f"nl:c{i}:clean": float(i) for i in range(10)}  # controls score 0..9
    scores |= {f"nl:p{i}:omission": s for i, s in enumerate([1.0, 5.0, 8.5, 12.0])}
    return items, scores


def test_qe_threshold_flags_the_requested_share_of_controls():
    items, scores = _qe_items()
    t = bench.qe_threshold(items, scores, 0.2)
    assert t == 7.0  # controls scoring 8 and 9 exceed it
    cells = bench.qe_cells(items, scores, t)
    assert (cells["clean", "all"].k, cells["clean", "all"].n) == (2, 10)
    assert (cells["omission", "nl"].k, cells["omission", "nl"].n) == (2, 4)  # 8.5 and 12.0


def test_qe_table_and_cli_read_scores_and_check_the_items_hash(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    items, scores = _qe_items()
    bench.write_jsonl(tmp_path / "items.jsonl", (bench.asdict(i) for i in items))
    qe = tmp_path / "qe"
    bench.write_jsonl(qe / "scores.jsonl", ({"id": k, "score": v} for k, v in scores.items()))
    sha = bench.sha256_file(tmp_path / "items.jsonl")
    (qe / "manifest.json").write_text(json.dumps({"items_sha256": sha}), encoding="utf-8")
    assert bench.read_scores(qe, sha) == scores
    with pytest.raises(ValueError, match="items_sha256"):
        bench.read_scores(qe, "0" * 64)
    assert main(["bench", "score", str(tmp_path / "items.jsonl"), "--qe", str(qe), "--qe-rate", "0.2"]) == 0
    out = capsys.readouterr().out
    assert "score > 7.00 (target 20%)" in out
    assert "2/4 = 50%" in out
