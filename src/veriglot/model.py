"""Model-assisted review through any chat-completions server.

The module imports no model library: it posts JSON with the standard library, so a local
vLLM or llama.cpp server and a hosted endpoint work the same. Model output is a suggestion:
every answer is constrained at the server, validated again here, and turned into the same
``Finding`` the deterministic checks produce, so ``apply`` and the CSV need no changes.
Every call is logged with the model, server version, prompt hash, settings and token counts.
"""

import hashlib
import http.client
import json
import re
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from string import Template
from typing import TYPE_CHECKING, Any, Literal

from babel import Locale
from pydantic import BaseModel, ValidationError

from veriglot.canary import append_jsonl, json_object
from veriglot.review import Finding, Kind, Severity, check_entry, fingerprint

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from pathlib import Path

MODEL_KINDS = (Kind.MEANING, Kind.OMISSION, Kind.ADDITION, Kind.TERMINOLOGY, Kind.REGISTER, Kind.GRAMMAR)
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_SEVERITY_ORDER = [Severity.INFO, Severity.WARNING, Severity.ERROR]
CONSTRAINTS = ("structured_outputs", "response_format", "none")

type Transport = Callable[[str, dict[str, Any] | None], dict[str, Any]]
"""``transport(url, body)``: POST ``body`` as JSON, or GET when ``body`` is None; returns the parsed JSON."""


class ModelFinding(BaseModel):
    """One problem a model reports."""

    kind: Literal["meaning", "omission", "addition", "terminology", "register", "grammar", "untranslated"]
    severity: Literal["error", "warning", "info"]
    message: str
    proposed: str | None = None


class ModelReview(BaseModel):
    """The review answer. ``rationale`` comes first so the model reasons before it labels."""

    rationale: str
    findings: list[ModelFinding]


class Verdict(BaseModel):
    """The direct-judge baseline: is the translation correct?"""

    rationale: str
    correct: bool


@dataclass(frozen=True)
class Prompt:
    """A prompt file of the package; the hash identifies the exact text a call used."""

    name: str
    text: str

    @property
    def sha256(self) -> str:
        """Hash of the file's bytes."""
        return hashlib.sha256(self.text.encode()).hexdigest()

    def render(self, **values: str) -> str:
        """Fill ``$name`` fields; values are inserted as they are, never re-scanned."""
        return Template(self.text).substitute(values)


def load_prompt(name: str) -> Prompt:
    """Read ``prompts/<name>.md`` from the package."""
    text = resources.files("veriglot").joinpath("prompts", f"{name}.md").read_text(encoding="utf-8")
    return Prompt(name, text)


@dataclass(frozen=True)
class Settings:
    """Everything besides the prompt that shapes a call; logged with every call."""

    temperature: float = 0.0
    seed: int = 0
    max_tokens: int = 1024
    constraint: str = "structured_outputs"
    """``structured_outputs`` (vLLM), ``response_format`` (json_schema) or ``none``."""
    extra_body: dict[str, Any] = field(default_factory=dict)
    """Merged into the request, e.g. ``chat_template_kwargs`` that turn reasoning off."""


@dataclass(frozen=True)
class CallResult:
    """The outcome of one logical call (one or two attempts)."""

    parsed: BaseModel | None
    error: str | None
    attempts: int


def check_endpoint(base_url: str, *, allow_remote: bool) -> None:
    """Refuse a non-local endpoint unless the caller opted in: nothing goes to a hosted API silently.

    Raises:
        ValueError: the host is not loopback and ``allow_remote`` is false.
    """
    host = urllib.parse.urlsplit(base_url).hostname or ""
    if host not in LOOPBACK and not allow_remote:
        msg = f"{host or base_url!r} is not a local server"
        raise ValueError(msg)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None  # a redirect is an error: it could lead off this machine


def _http(url: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})  # noqa: S310 - scheme checked by the caller's base URL
    if (urllib.parse.urlsplit(url).hostname or "") in LOOPBACK:
        # A proxy variable must not carry a local request off this machine.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    else:
        opener = urllib.request.build_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        with exc:  # releases the socket
            detail = exc.read(300).decode("utf-8", "replace")
        what = "redirect not followed" if 300 <= exc.code < 400 else "error"
        msg = f"HTTP {exc.code} ({what}) from the model server: {detail}"
        raise OSError(msg) from exc


class ModelClient:
    """Chat completions with a constrained JSON answer, a retry and a call log."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        settings: Settings | None = None,
        log_path: Path | None = None,
        transport: Transport | None = None,
        timeout: float = 300,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            msg = f"base URL {base_url!r} must start with http:// or https://"
            raise ValueError(msg)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.settings = settings or Settings()
        if self.settings.constraint not in CONSTRAINTS:
            msg = f"constraint {self.settings.constraint!r} is not one of {', '.join(CONSTRAINTS)}"
            raise ValueError(msg)
        self.log_path = log_path
        self._transport = transport or (lambda url, body: _http(url, body, timeout))
        self._lock = threading.Lock()
        self.server_version: str | None = None
        self.answers: dict[str, str] = {}
        """Answers of earlier calls by ``request_sha256``, which ``complete`` reuses instead of asking."""
        self.reused = 0

    def preflight(self) -> None:
        """Check that the server answers and the model is served; record the server version.

        Raises:
            OSError: the server is unreachable or does not serve ``model``.
        """
        listed = self._transport(f"{self.base_url}/models", None)
        names = {m.get("id") for m in listed.get("data", [])}
        if self.model not in names:
            served = ", ".join(sorted(map(str, names))) or "nothing"
            msg = f"the server does not serve {self.model!r} (it serves {served})"
            raise OSError(msg)
        root = self.base_url.removesuffix("/v1")
        try:
            self.server_version = str(self._transport(f"{root}/version", None).get("version"))
        except OSError, ValueError:
            self.server_version = None  # not every server has /version

    def load_answers(self) -> None:
        """Reuse the successful answers already in the call log, so a stopped run resumes.

        Only a call with the same server, server version, model, settings, schema and rendered
        prompt is reused, so a changed catalog entry, glossary or prompt is asked again. Lines that
        are not a JSON object, such as a line cut off when the run stopped, are skipped.
        """
        if self.log_path is None or not self.log_path.exists():
            return
        for line in self.log_path.read_bytes().splitlines():
            entry = json_object(line) or {}
            if entry.get("outcome") == "ok" and entry.get("request_sha256") and isinstance(entry.get("content"), str):
                self.answers[entry["request_sha256"]] = entry["content"]

    def _request_sha256(self, text: str, schema: type[BaseModel]) -> str:
        body = self._body(text, schema, 0, self.settings.max_tokens)
        return hashlib.sha256(
            json.dumps([self.base_url, self.server_version, body], sort_keys=True).encode()
        ).hexdigest()

    def _body(self, prompt_text: str, schema: type[BaseModel], attempt: int, max_tokens: int) -> dict[str, Any]:
        s = self.settings
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt_text}],
            "temperature": s.temperature,
            "seed": s.seed + attempt,
            "max_tokens": max_tokens,
        }
        json_schema = schema.model_json_schema()
        if s.constraint == "structured_outputs":
            body["structured_outputs"] = {"json": json_schema}
        elif s.constraint == "response_format":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": json_schema},
            }
        return body | s.extra_body

    def complete(self, prompt: Prompt, schema: type[BaseModel], tag: str, **values: str) -> CallResult:
        """Render ``prompt``, ask the model, validate the answer; retry once on any failure.

        ``tag`` names the entry in the log. The result holds the validated object or the last error;
        an answer from ``answers`` is reused with ``attempts`` 0.
        """
        text = prompt.render(**values)
        request_sha256 = self._request_sha256(text, schema)
        if (content := self.answers.get(request_sha256)) is not None:
            try:
                parsed = schema.model_validate_json(content)
            except ValidationError:
                pass  # ask again
            else:
                with self._lock:
                    self.reused += 1
                return CallResult(parsed, None, 0)
        error = "no attempt"
        max_tokens = self.settings.max_tokens
        for attempt in (0, 1):
            started = time.monotonic()
            record: dict[str, Any] = {"request_sha256": request_sha256}
            try:
                reply = self._transport(
                    f"{self.base_url}/chat/completions", self._body(text, schema, attempt, max_tokens)
                )
                choice = reply["choices"][0]
                record |= {
                    "finish_reason": choice.get("finish_reason"),
                    "usage": reply.get("usage"),
                    "content": choice["message"].get("content"),
                }
                parsed = self._validate(record, schema)
            except (OSError, http.client.HTTPException, KeyError, IndexError, TypeError, ValueError) as exc:
                # ValidationError is a ValueError; a refusal or a cut-off answer lands here too.
                error = f"{type(exc).__name__}: {exc}"[:300]
                parsed = None
            self._log(prompt, tag, attempt, started, record, error if parsed is None else None, max_tokens)
            if record.get("finish_reason") == "length":
                max_tokens *= 2  # the same budget would cut the answer off again
            if parsed is not None:
                return CallResult(parsed, None, attempt + 1)
        return CallResult(None, error, 2)

    @staticmethod
    def _validate(record: dict[str, Any], schema: type[BaseModel]) -> BaseModel:
        # Check finish_reason before parsing: a length stop can still be valid JSON that lost its tail.
        if record["finish_reason"] != "stop":
            msg = f"finish_reason is {record['finish_reason']!r}, not 'stop'"
            raise ValueError(msg)
        try:
            return schema.model_validate_json(record["content"] or "")
        except ValidationError as exc:
            msg = f"answer does not fit the schema: {exc.error_count()} error(s): {exc.errors()[0]['msg']}"
            raise ValueError(msg) from exc

    def _log(
        self,
        prompt: Prompt,
        tag: str,
        attempt: int,
        started: float,
        record: dict[str, Any],
        error: str | None,
        max_tokens: int,
    ) -> None:
        if self.log_path is None:
            return
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "tag": tag,
            "model": self.model,
            "base_url": self.base_url,
            "server_version": self.server_version,
            "prompt": prompt.name,
            "prompt_sha256": prompt.sha256,
            "settings": {
                "temperature": self.settings.temperature,
                "seed": self.settings.seed + attempt,
                "max_tokens": max_tokens,
                "constraint": self.settings.constraint,
                "extra_body": self.settings.extra_body,
            },
            "attempt": attempt,
            "latency_s": round(time.monotonic() - started, 3),
            "outcome": "ok" if error is None else "failed",
            "error": error,
            **record,
        }
        with self._lock:
            append_jsonl(self.log_path, entry)


@dataclass(frozen=True)
class Glossary:
    """Approved terms per locale and a style rule per locale, from a TOML file.

    ```toml
    [style]
    nl = "Address the user formally (u), never je."

    [[term]]
    en = "contributor"
    nl = "bijdrager"
    de = "Mitwirkende"
    note = "optional, shown to the model"
    ```
    """

    terms: tuple[dict[str, str], ...] = ()
    style: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Glossary:
        """Read a glossary file.

        Raises:
            ValueError: the file is not valid TOML or has terms without an ``en`` entry.
        """
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            msg = f"{path.as_posix()}: {exc}"
            raise ValueError(msg) from exc
        terms = tuple(data.get("term", []))
        if bad := [t for t in terms if "en" not in t]:
            msg = f"{path.as_posix()}: a [[term]] has no 'en' source term: {bad[0]}"
            raise ValueError(msg)
        return cls(terms, dict(data.get("style", {})))

    def for_entry(self, locale: str, source: str) -> tuple[str, str]:
        """The style rule and the glossary lines whose English term occurs in ``source``."""
        lowered = source.lower()
        lines = []
        language = re.split(r"[-_]", locale)[0]  # nl-NL and nl_BE use the nl entries when they have none of their own
        for t in self.terms:
            if t["en"].lower() in lowered and (key := next((k for k in (locale, language) if k in t), None)):
                note = f" ({t['note']})" if t.get("note") else ""
                lines.append(f"- {t['en']} -> {t[key]}{note}")
        style = self.style.get(locale) or self.style.get(language, "none")
        return style, "\n".join(lines) or "- none apply"


def _quote(text: str) -> str:
    """A JSON string literal: catalog text cannot end its field or start a new instruction line."""
    return json.dumps(text, ensure_ascii=False)


def language_name(locale: str) -> str:
    """English name of a locale's language, falling back to the code."""
    try:
        return Locale.parse(locale.replace("-", "_")).get_display_name("en") or locale
    except ValueError, ImportError:
        return locale


@dataclass(frozen=True)
class EntryReview:
    """Model findings for one entry, or why there are none."""

    findings: tuple[Finding, ...]
    rationale: str
    failure: str | None


def _merge(
    findings: Sequence[ModelFinding], locale: str, key: str, source: str, target: str, context: str
) -> list[Finding]:
    """One Finding per kind, as ids are ``<locale>:<key>:<kind>``: messages joined, first valid proposal kept."""
    by_kind: dict[str, list[ModelFinding]] = {}
    for f in findings:
        by_kind.setdefault(f.kind, []).append(f)
    out = []
    for kind, group in by_kind.items():
        proposed = next((g.proposed for g in group if _usable(g.proposed, locale, key, source, target)), None)
        severity = max((Severity(g.severity) for g in group), key=_SEVERITY_ORDER.index)
        out.append(
            Finding(
                id=f"{locale}:{key}:{kind}",
                locale=locale,
                key=key,
                kind=Kind(kind),
                severity=severity,
                source=source,
                target=target,
                context=context,
                message="; ".join(g.message for g in group),
                proposed=proposed,
                fingerprint=fingerprint(source, target),
            )
        )
    return out


def _usable(proposed: str | None, locale: str, key: str, source: str, target: str) -> bool:
    """A proposal must differ from the target and pass the checks, or apply would reject it."""
    if not proposed or not proposed.strip() or proposed == target:
        return False
    return not any(f.severity is Severity.ERROR for f in check_entry(locale, key, source, proposed))


def review_entry(
    client: ModelClient,
    prompt: Prompt,
    glossary: Glossary,
    locale: str,
    key: str,
    source: str,
    target: str,
    context: str = "",
) -> EntryReview:
    """Ask the model to review one translation; model findings come back as ``Finding``s."""
    style, terms = glossary.for_entry(locale, source)
    result = client.complete(
        prompt,
        ModelReview,
        f"{locale}:{key}",
        language=language_name(locale),
        locale=locale,
        key=_quote(key),
        context=_quote(context or "none"),
        style=style,
        glossary=terms,
        source=_quote(source),
        target=_quote(target),
    )
    if not isinstance(result.parsed, ModelReview):
        return EntryReview((), "", result.error)
    return EntryReview(
        tuple(_merge(result.parsed.findings, locale, key, source, target, context)), result.parsed.rationale, None
    )


def judge_entry(
    client: ModelClient, prompt: Prompt, locale: str, key: str, source: str, target: str
) -> tuple[bool | None, str, str | None]:
    """The baseline: ``(correct, rationale, failure)`` from a plain "is this translation correct?" question."""
    result = client.complete(
        prompt,
        Verdict,
        f"{locale}:{key}",
        language=language_name(locale),
        locale=locale,
        source=_quote(source),
        target=_quote(target),
    )
    if not isinstance(result.parsed, Verdict):
        return None, "", result.error
    return result.parsed.correct, result.parsed.rationale, None


def iter_concurrently[T, R](func: Callable[[T], R], items: Sequence[T], concurrency: int) -> Generator[R]:
    """Yield ``func(item)`` as each call finishes (input order when ``concurrency`` is 1).

    When the caller stops early (Ctrl-C, an error), queued calls are cancelled and running
    ones are not waited for.
    """
    if concurrency <= 1:
        for item in items:
            yield func(item)
        return
    pool = ThreadPoolExecutor(max_workers=concurrency)
    try:
        for future in as_completed([pool.submit(func, i) for i in items]):
            yield future.result()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
