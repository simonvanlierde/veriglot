"""The brace structure of ICU MessageFormat messages.

Only what the checks compare is parsed: argument names, plural/select/selectordinal
branch keys, ``#`` and apostrophe quoting. Rich-text tags such as ``<contrib>`` or
``<0>`` stay literal text and are collected, never matched as markup, because catalogs
use them both as markup and as literal XML names.
"""

import re
from dataclasses import dataclass, field

_NAME = re.compile(r"[^\s{}(),#'<>:]+")
_TYPE = re.compile(r"[A-Za-z]+")
_KEY = re.compile(r"=\d+(?:\.\d+)?|[^\s{}]+")
_TAG = re.compile(r"</?[A-Za-z0-9][\w.-]*\s*/?>")
_BRANCHING = frozenset({"plural", "select", "selectordinal"})


class IcuSyntaxError(ValueError):
    """The message does not follow ICU MessageFormat syntax."""


@dataclass(frozen=True)
class Shape:
    """What a message contains, ignoring the literal wording."""

    names: frozenset[str]
    """Every argument name, nested ones included."""
    simple: frozenset[str]
    """Names used as a plain or formatted argument, such as ``{name}`` or ``{n, number}``."""
    complex: dict[str, tuple[str, frozenset[str]]]
    """Branching arguments: name to (type, branch keys), from the first occurrence."""
    tags: tuple[str, ...]
    """Tag tokens in unquoted literal text, sorted, with repeats."""
    text: str
    """The literal text, quotes resolved, with arguments and ``#`` removed."""


@dataclass
class _Parser:
    src: str
    i: int = 0
    names: set[str] = field(default_factory=set)
    simple: set[str] = field(default_factory=set)
    complex: dict[str, tuple[str, frozenset[str]]] = field(default_factory=dict)
    text: list[str] = field(default_factory=list)
    unquoted: list[str] = field(default_factory=list)

    def fail(self, what: str) -> IcuSyntaxError:
        return IcuSyntaxError(f"{what} at offset {self.i}")

    def peek(self) -> str:
        return self.src[self.i] if self.i < len(self.src) else ""

    def skip_ws(self) -> None:
        while self.peek().isspace():
            self.i += 1

    def expect(self, char: str, what: str) -> None:
        if self.peek() != char:
            raise self.fail(f"expected {what}")
        self.i += 1

    def match(self, pattern: re.Pattern[str]) -> str:
        m = pattern.match(self.src, self.i)
        if not m:
            return ""
        self.i = m.end()
        return m.group()

    def message(self, *, in_plural: bool, nested: bool) -> None:
        """Parse until a closing brace (left unconsumed) or the end."""
        run: list[str] = []  # unquoted literal run, scanned for tags as one string
        while self.i < len(self.src):
            c = self.src[self.i]
            if c == "'":
                nxt = self.src[self.i + 1 : self.i + 2]
                if nxt == "'":
                    run.append("'")
                    self.text.append("'")
                    self.i += 2
                elif nxt and (nxt in "{}" or (in_plural and nxt == "#")):
                    # Not before "<": French "l'<cli>" is an apostrophe and a tag, as in ICU.
                    self.unquoted.append("".join(run))
                    run = []
                    self.quoted()
                else:
                    run.append(c)
                    self.text.append(c)
                    self.i += 1
            elif c == "{":
                self.unquoted.append("".join(run))
                run = []
                self.argument(in_plural=in_plural)
            elif c == "}":
                if not nested:
                    raise self.fail("unmatched '}'")
                break
            elif c == "#" and in_plural:
                self.i += 1
            else:
                run.append(c)
                self.text.append(c)
                self.i += 1
        self.unquoted.append("".join(run))
        if nested and self.i >= len(self.src):
            raise self.fail("unclosed '{'")

    def quoted(self) -> None:
        """Consume ``'...'`` starting at the opening apostrophe; ``''`` inside is one apostrophe."""
        self.i += 1
        while self.i < len(self.src):
            c = self.src[self.i]
            if c == "'":
                if self.src[self.i + 1 : self.i + 2] == "'":
                    self.text.append("'")
                    self.i += 2
                    continue
                self.i += 1
                return
            self.text.append(c)
            self.i += 1
        # An unterminated quote runs to the end of the message, as in ICU.

    def argument(self, *, in_plural: bool) -> None:
        self.i += 1
        self.skip_ws()
        name = self.match(_NAME)
        if not name:
            raise self.fail("expected an argument name")
        self.names.add(name)
        self.skip_ws()
        if self.peek() == "}":
            self.i += 1
            self.simple.add(name)
            return
        self.expect(",", "',' or '}' after the argument name")
        self.skip_ws()
        kind = self.match(_TYPE)
        if not kind:
            raise self.fail("expected an argument type")
        self.skip_ws()
        if self.peek() == "}":
            self.i += 1
            self.simple.add(name)
            return
        self.expect(",", "',' or '}' after the argument type")
        if kind in _BRANCHING:
            self.branches(name, kind, in_plural=in_plural)
        else:
            self.style()
            self.simple.add(name)

    def branches(self, name: str, kind: str, *, in_plural: bool) -> None:
        self.skip_ws()
        if kind != "select" and self.src.startswith("offset:", self.i):
            self.i += len("offset:")
            self.skip_ws()
            if not self.match(re.compile(r"\d+")):
                raise self.fail("expected a number after 'offset:'")
        keys: list[str] = []
        while True:
            self.skip_ws()
            if self.peek() == "}":
                self.i += 1
                break
            key = self.match(_KEY)
            if not key:
                raise self.fail("expected a branch key")
            if key in keys:
                raise self.fail(f"duplicate branch key {key!r}")
            keys.append(key)
            self.skip_ws()
            self.expect("{", f"'{{' after branch key {key!r}")
            self.message(in_plural=in_plural or kind != "select", nested=True)
            self.i += 1
        if not keys:
            raise self.fail(f"{kind} argument {name!r} has no branches")
        self.complex.setdefault(name, (kind, frozenset(keys)))

    def style(self) -> None:
        """Skip a format style such as ``::currency/EUR``, balancing braces and quotes."""
        depth = 0
        mark = len(self.text)  # quoted() records literal text; a style is not
        while self.i < len(self.src):
            c = self.src[self.i]
            if c == "'":
                self.quoted()
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0:
                    self.i += 1
                    del self.text[mark:]
                    return
                depth -= 1
            self.i += 1
        raise self.fail("unclosed argument")


def parse(message: str) -> Shape:
    """Parse one message.

    Raises:
        IcuSyntaxError: the braces or branch syntax are malformed.
    """
    p = _Parser(message)
    p.message(in_plural=False, nested=False)
    tags = sorted(t for run in p.unquoted for t in _TAG.findall(run))
    return Shape(
        names=frozenset(p.names),
        simple=frozenset(p.simple),
        complex=p.complex,
        tags=tuple(tags),
        text="".join(p.text),
    )
