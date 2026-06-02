#!/usr/bin/env python3
"""A single-file, stdlib-only Python client for the Lemma wire protocol.

This module is a *recipe*, not a library: it is meant to be read end to end.
The first thing any Lemma client needs is a way to turn Python values into the
EDN text the server speaks, and to turn the server's EDN responses back into
Python values. That codec is all that lives in this file today; later additions
(an HTTP transport and a runnable ``main()``) build directly on top of it.

EDN in a nutshell
-----------------
EDN (Extensible Data Notation) is Clojure's data syntax. Lemma uses a small,
well-defined subset of it (see ``lemma/grammar/lemma.lark``). The pieces we
care about:

    nil true false              -- the three literals
    42  -3  3.14  3.14e2         -- integers and floats
    "a string\n"                -- double-quoted, backslash escapes
    :event  :verbs/core         -- keywords (a leading ``:``)
    equivalent  member-of  ?o   -- symbols (a bare name; ``?``-vars are symbols)
    ( a b c )                   -- a LIST
    [ a b c ]                   -- a VECTOR
    { k v, k v }                -- a MAP (commas are whitespace)
    #{ a b c }                  -- a SET
    #tag payload                -- a TAGGED LITERAL, e.g. #entity "alice"

Lists versus vectors -- the one design decision
-----------------------------------------------
EDN distinguishes lists ``( ... )`` from vectors ``[ ... ]``, and Lemma relies
on the distinction (grammar §3): a *list* appears **only** as the top-level
verb form -- ``(propose ...)``, ``(query ...)``, ``(hello)``. Everywhere inside
the arguments, collections are *vectors* (``:find [?x]``, ``:where [[...]]``),
maps, or sets -- never lists.

Python has no separate list/vector types, so we must choose a mapping:

    * Python ``list``  -> EDN VECTOR ``[ ... ]``   (the overwhelmingly common case)
    * the ``Lst`` wrapper in this module -> EDN LIST ``( ... )``  (verb forms only)

This keeps the two distinct and round-trippable, and makes the rare list
(the verb form) explicit at the call site:

    >>> edn_write(Lst([Symbol('use-world'), Tagged('world', 'default')]))
    '(use-world #world "default")'

On the read side the inverse holds: ``( ... )`` reads back as ``Lst``, and
``[ ... ]`` reads back as a plain Python ``list``.

Unknown tags
------------
The reader is deliberately tolerant of tags. The ten core Lemma tags are
``#fact #violation #entity #proposal #tx #ref #cursor #watch #session
#world``, but responses also carry e.g. ``#inst "..."``. Rather than special-
casing each, *every* ``#tag payload`` is wrapped uniformly as
``Tagged(tag, payload)`` -- the same behaviour as Clojure's
``{:default tagged-literal}``. This means an unknown tag never breaks parsing
and always round-trips clean.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Value types
#
# EDN has a few scalar kinds that Python lacks: keywords, symbols, and tagged
# literals. We model each as a tiny immutable wrapper. Keyword and Symbol must
# be hashable because they show up as map keys and set members.
# ---------------------------------------------------------------------------


class Keyword:
    """An EDN keyword: a ``:``-prefixed name such as ``:event`` or ``:verbs/core``.

    The stored ``name`` always includes the leading colon, so it is exactly the
    text that appears on the wire.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        if not name.startswith(":"):
            raise ValueError(f"keyword name must start with ':', got {name!r}")
        self.name = name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Keyword) and other.name == self.name

    def __hash__(self) -> int:
        # Tag the hash with the type so a Keyword and a Symbol of the "same"
        # spelling never collide in a dict or set.
        return hash(("Keyword", self.name))

    def __repr__(self) -> str:
        return f"Keyword({self.name!r})"


class Symbol:
    """An EDN symbol: a bare name such as ``equivalent``, ``member-of``, or ``?o``.

    Query variables (``?x``) are ordinary symbols whose name happens to start
    with ``?``; no separate type is needed.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        if not name:
            raise ValueError("symbol name must be non-empty")
        self.name = name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Symbol) and other.name == self.name

    def __hash__(self) -> int:
        return hash(("Symbol", self.name))

    def __repr__(self) -> str:
        return f"Symbol({self.name!r})"


class Tagged:
    """An EDN tagged literal: ``#tag payload``.

    ``tag`` is the tag name *without* the leading ``#`` (e.g. ``"entity"``,
    ``"world"``, ``"fact"``). ``value`` is the already-parsed payload, which may
    be any EDN value -- a string for ``#entity "alice"``, a map for
    ``#fact{...}``, and so on.
    """

    __slots__ = ("tag", "value")

    def __init__(self, tag: str, value: Any) -> None:
        if not tag:
            raise ValueError("tag name must be non-empty")
        self.tag = tag
        self.value = value

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Tagged)
            and other.tag == self.tag
            and other.value == self.value
        )

    def __hash__(self) -> int:
        # Hashable when the payload is; mirrors EDN where tagged literals can
        # appear as set members / map keys (e.g. #entity "alice").
        return hash(("Tagged", self.tag, _hashable(self.value)))

    def __repr__(self) -> str:
        return f"Tagged({self.tag!r}, {self.value!r})"


class Lst:
    """An EDN list ``( ... )`` -- used *only* for the top-level verb form.

    Wrap the items of a verb call in this so the writer emits parentheses
    rather than the vector brackets a plain Python ``list`` produces. See the
    module docstring for the full rationale.

        >>> Lst([Symbol('hello')])
        Lst([Symbol('hello')])
    """

    __slots__ = ("items",)

    def __init__(self, items: Any = ()) -> None:
        self.items = list(items)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Lst) and other.items == self.items

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __repr__(self) -> str:
        return f"Lst({self.items!r})"


def _hashable(value: Any) -> Any:
    """Best-effort coercion of an EDN payload into something hashable.

    Only needed so a ``Tagged`` wrapping a non-hashable payload (a map, say)
    can still report *a* hash. Lists/maps become their repr; everything else is
    returned as-is.
    """
    try:
        hash(value)
        return value
    except TypeError:
        return repr(value)


# ---------------------------------------------------------------------------
# Writer:  Python value  ->  EDN text
# ---------------------------------------------------------------------------

# String escapes, per the grammar's STRING terminal. We escape the backslash
# first so we never double-process an escape we just introduced.
_STRING_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\t": "\\t",
    "\r": "\\r",
}


def _write_string(s: str) -> str:
    """Render a Python ``str`` as a double-quoted EDN string with escapes."""
    out = ['"']
    for ch in s:
        out.append(_STRING_ESCAPES.get(ch, ch))
    out.append('"')
    return "".join(out)


def edn_write(value: Any) -> str:
    """Serialize a Python value to canonical EDN text.

    Supported inputs and their renderings:

        None / True / False    -> nil / true / false
        int / float            -> their literal text
        str                    -> "..."  (with \\ " \\n \\t \\r escaped)
        Keyword(':event')      -> :event
        Symbol('equivalent')   -> equivalent
        list  [a, b, c]        -> [a b c]      (EDN VECTOR)
        Lst([a, b, c])         -> (a b c)      (EDN LIST -- verb forms only)
        tuple (a, b, c)        -> [a b c]      (treated as a vector too)
        dict  {k: v}           -> {k v k v}
        set / frozenset        -> #{a b c}
        Tagged(tag, payload)   -> #tag <payload>
    """
    # Order matters: bool is a subclass of int, so test it first.
    if value is None:
        return "nil"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _write_string(value)
    if isinstance(value, Keyword):
        return value.name
    if isinstance(value, Symbol):
        return value.name
    if isinstance(value, Tagged):
        return _write_tagged(value)
    if isinstance(value, Lst):
        return "(" + " ".join(edn_write(v) for v in value.items) + ")"
    if isinstance(value, (list, tuple)):
        return "[" + " ".join(edn_write(v) for v in value) + "]"
    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            parts.append(edn_write(k))
            parts.append(edn_write(v))
        return "{" + " ".join(parts) + "}"
    if isinstance(value, (set, frozenset)):
        return "#{" + " ".join(edn_write(v) for v in value) + "}"
    raise TypeError(f"cannot EDN-encode value of type {type(value).__name__}: {value!r}")


def _write_tagged(t: Tagged) -> str:
    """Render ``#tag payload``.

    EDN wants a separator between the tag and a scalar/symbol payload
    (``#entity "alice"``), but a collection payload may abut the tag directly
    (``#fact{...}``). We emit a space before anything that does not open with
    its own delimiter, which keeps the output both legal and tidy.
    """
    payload = edn_write(t.value)
    sep = "" if payload[:1] in ("{", "[", "(") else " "
    return f"#{t.tag}{sep}{payload}"


# ---------------------------------------------------------------------------
# Reader:  EDN text  ->  Python value
#
# A small recursive-descent reader over the subset above. It keeps a cursor
# (an index into the source) and advances it as it consumes tokens.
# ---------------------------------------------------------------------------

# Characters that may begin or be part of a symbol/keyword name. We are a touch
# more permissive than the grammar's exact class -- enough to read everything
# the server emits without re-deriving the regex character classes here.
_SYMBOL_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "_*!?+-=<>$&./:"
)

# EDN treats commas as whitespace.
_WHITESPACE = set(" \t\r\n,")

# String escapes recognised by the reader, inverse of the writer's table plus
# the extra forms the grammar permits (\b, \f).
_READ_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
}


class EDNReadError(ValueError):
    """Raised when the reader encounters input it cannot parse."""


class _Reader:
    """Stateful cursor over an EDN source string."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.len = len(text)

    # -- low-level cursor helpers ------------------------------------------

    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < self.len else ""

    def _next(self) -> str:
        ch = self.text[self.pos]
        self.pos += 1
        return ch

    def _error(self, msg: str) -> EDNReadError:
        return EDNReadError(f"{msg} at position {self.pos}")

    def _skip_ws(self) -> None:
        """Consume whitespace, commas, and ``;`` line comments."""
        while self.pos < self.len:
            ch = self.text[self.pos]
            if ch in _WHITESPACE:
                self.pos += 1
            elif ch == ";":
                # Line comment: skip to end of line (or end of input).
                while self.pos < self.len and self.text[self.pos] != "\n":
                    self.pos += 1
            else:
                break

    # -- the value dispatcher ----------------------------------------------

    def read_value(self) -> Any:
        """Read and return the next complete EDN value."""
        self._skip_ws()
        if self.pos >= self.len:
            raise self._error("unexpected end of input")

        ch = self._peek()

        if ch == "(":
            return self._read_seq("(", ")", Lst)
        if ch == "[":
            return self._read_seq("[", "]", list)
        if ch == "{":
            return self._read_map()
        if ch == '"':
            return self._read_string()
        if ch == ":":
            return self._read_keyword()
        if ch == "#":
            return self._read_dispatch()
        if ch == "^":
            return self._read_metadata()
        if ch == "\\":
            return self._read_char()
        if ch == "-" or ch == "+" or ch.isdigit():
            return self._read_number_or_symbol()
        if ch in _SYMBOL_CHARS or ch == "?":
            return self._read_symbol_or_literal()

        raise self._error(f"unexpected character {ch!r}")

    def _read_metadata(self) -> Any:
        """Read ``^{...}`` metadata and return the value it adorns.

        The grammar (§3) admits metadata as an optional prefix on any value. It
        is a client-side adornment the server may ignore, so the reader parses
        the metadata map for well-formedness and discards it, returning the
        value that follows.
        """
        assert self._next() == "^"
        self._skip_ws()
        if self._peek() != "{":
            raise self._error("expected '{' after metadata '^'")
        self._read_map()  # parse and discard the metadata map
        return self.read_value()

    def _read_char(self) -> str:
        r"""Read an EDN character literal: ``\newline``, ``\space``, ``\uXXXX``,
        or ``\<single char>``. Returned as a one-character Python ``str``."""
        assert self._next() == "\\"
        named = {
            "newline": "\n",
            "space": " ",
            "tab": "\t",
            "return": "\r",
            "formfeed": "\f",
            "backspace": "\b",
        }
        # Unicode escape: \uXXXX
        if self._peek() == "u" and self.text[self.pos + 1 : self.pos + 5].isalnum() \
                and len(self.text[self.pos + 1 : self.pos + 5]) == 4:
            hexits = self.text[self.pos + 1 : self.pos + 5]
            if all(c in "0123456789abcdefABCDEF" for c in hexits):
                self.pos += 5
                return chr(int(hexits, 16))
        # Named character: read the alphabetic run and look it up.
        start = self.pos
        while self.pos < self.len and self.text[self.pos].isalpha():
            self.pos += 1
        word = self.text[start : self.pos]
        if len(word) > 1 and word in named:
            return named[word]
        # Single-character literal: rewind to just the first char.
        self.pos = start + 1
        if start >= self.len:
            raise self._error("expected a character after '\\'")
        return self.text[start]

    # -- collections --------------------------------------------------------

    def _read_seq(self, open_ch: str, close_ch: str, ctor) -> Any:
        """Read a delimited sequence of values into ``ctor(items)``.

        Used for both lists ``( )`` -> ``Lst`` and vectors ``[ ]`` -> ``list``.
        """
        assert self._next() == open_ch
        items = []
        while True:
            self._skip_ws()
            if self.pos >= self.len:
                raise self._error(f"unterminated sequence, expected {close_ch!r}")
            if self._peek() == close_ch:
                self.pos += 1
                return ctor(items)
            items.append(self.read_value())

    def _read_map(self) -> dict:
        """Read a ``{ k v, k v }`` map. Keys keep their parsed type."""
        assert self._next() == "{"
        result: dict = {}
        while True:
            self._skip_ws()
            if self.pos >= self.len:
                raise self._error("unterminated map, expected '}'")
            if self._peek() == "}":
                self.pos += 1
                return result
            key = self.read_value()
            self._skip_ws()
            if self.pos >= self.len or self._peek() == "}":
                raise self._error("map key without a value")
            result[key] = self.read_value()

    def _read_set(self) -> set:
        """Read a ``#{ a b c }`` set (the leading ``#`` is already consumed)."""
        assert self._next() == "{"
        items = []
        while True:
            self._skip_ws()
            if self.pos >= self.len:
                raise self._error("unterminated set, expected '}'")
            if self._peek() == "}":
                self.pos += 1
                return set(items)
            items.append(self.read_value())

    # -- scalars ------------------------------------------------------------

    def _read_string(self) -> str:
        """Read a double-quoted string, decoding escapes incl. ``\\uXXXX``."""
        assert self._next() == '"'
        out = []
        while True:
            if self.pos >= self.len:
                raise self._error("unterminated string")
            ch = self._next()
            if ch == '"':
                return "".join(out)
            if ch == "\\":
                if self.pos >= self.len:
                    raise self._error("unterminated escape in string")
                esc = self._next()
                if esc == "u":
                    hexits = self.text[self.pos : self.pos + 4]
                    if len(hexits) != 4 or any(
                        c not in "0123456789abcdefABCDEF" for c in hexits
                    ):
                        raise self._error("malformed \\u escape")
                    self.pos += 4
                    out.append(chr(int(hexits, 16)))
                elif esc in _READ_ESCAPES:
                    out.append(_READ_ESCAPES[esc])
                else:
                    raise self._error(f"unknown escape \\{esc}")
            else:
                out.append(ch)

    def _read_keyword(self) -> Keyword:
        """Read a ``:``-prefixed keyword. Keeps the colon in ``name``."""
        start = self.pos
        self.pos += 1  # consume ':'
        while self.pos < self.len and self.text[self.pos] in _SYMBOL_CHARS:
            self.pos += 1
        name = self.text[start : self.pos]
        if name == ":":
            raise self._error("empty keyword")
        return Keyword(name)

    def _read_token(self) -> str:
        """Consume a run of symbol/keyword characters and return it."""
        start = self.pos
        while self.pos < self.len and self.text[self.pos] in _SYMBOL_CHARS:
            self.pos += 1
        return self.text[start : self.pos]

    def _read_symbol_or_literal(self) -> Any:
        """Read a bare token: ``nil`` / ``true`` / ``false`` or a Symbol.

        ``?``-prefixed query variables arrive here too and become Symbols.
        """
        if self._peek() == "?":
            # Variable: the leading '?' then a run of symbol characters.
            start = self.pos
            self.pos += 1  # consume '?'
            while self.pos < self.len and self.text[self.pos] in _SYMBOL_CHARS:
                self.pos += 1
            return Symbol(self.text[start : self.pos])

        token = self._read_token()
        if token == "nil":
            return None
        if token == "true":
            return True
        if token == "false":
            return False
        if token == "":
            raise self._error("expected a symbol")
        return Symbol(token)

    def _read_number_or_symbol(self) -> Any:
        """Read a numeric literal, or a symbol that merely starts with a sign.

        Bare ``-`` / ``+`` (as in the symbol ``member-of``'s head, or a lone
        operator) is not a number; we only treat the token as numeric when it
        parses as one.
        """
        token = self._read_token()
        as_num = _parse_number(token)
        if as_num is not None:
            return as_num
        # Not numeric -- it is a symbol (e.g. a sign-led operator name).
        if token in ("", "+", "-"):
            # A standalone sign is a legal symbol head in EDN operator names,
            # but with nothing following it is just that symbol.
            if token == "":
                raise self._error("expected a number or symbol")
        return Symbol(token)

    # -- dispatch (#) -------------------------------------------------------

    def _read_dispatch(self) -> Any:
        """Handle ``#``-led forms: ``#{ ... }`` sets and ``#tag payload``."""
        assert self._next() == "#"
        if self._peek() == "{":
            return self._read_set()
        # Tagged literal: read the tag name, then its payload value.
        tag = self._read_token()
        if not tag:
            raise self._error("expected a tag name after '#'")
        payload = self.read_value()
        return Tagged(tag, payload)


def _parse_number(token: str):
    """Return an int or float for ``token``, or ``None`` if it is not numeric.

    Tolerates the EDN bigint/bigdecimal suffixes ``N`` and ``M`` by stripping a
    single trailing one before delegating to Python's own parsers.
    """
    if not token:
        return None
    body = token
    if body[-1] in ("N", "M"):
        body = body[:-1]
    # Reject things that are clearly not numbers fast.
    if not any(c.isdigit() for c in body):
        return None
    try:
        return int(body)
    except ValueError:
        pass
    try:
        return float(body)
    except ValueError:
        return None


def edn_read(text: str) -> Any:
    """Parse one EDN value from ``text`` and return the Python representation.

    Trailing whitespace and comments are tolerated; trailing *data* is an
    error (a single message is one value).
    """
    reader = _Reader(text)
    value = reader.read_value()
    reader._skip_ws()
    if reader.pos != reader.len:
        raise reader._error("unexpected trailing data")
    return value


# ---------------------------------------------------------------------------
# HTTP transport:  EDN form  ->  POST  ->  parsed EDN response
#
# With the codec in hand, talking to a Lemma server is just "encode, POST,
# decode". This is the second layer the module docstring promised: a flat
# stdlib-only helper, not an abstraction. The session protocol (SPEC §3) is:
#
#   * The first call is anonymous: POST /v1/messages with (hello). The
#     :welcome response carries the new session id in the X-Lemma-Session
#     response header.
#   * Subsequent calls reuse that id, either on the named endpoint
#     POST /v1/sessions/{id}/messages or simply by echoing it back in the
#     x-lemma-session request header. We do the latter here.
#
# This helper handles one round-trip; the caller threads the returned
# session id into the next call. See examples/hello-http.clj for the full
# propose/assert/query sequence this enables.
# ---------------------------------------------------------------------------

import urllib.error
import urllib.request

# Where a locally booted Dianoia listener lives by default (see the protocol
# examples). Override per call via the ``base`` argument.
DEFAULT_BASE = "http://127.0.0.1:8080"


def post_edn(path, form, session=None, base=DEFAULT_BASE):
    """POST an EDN ``form`` to ``base + path`` and return ``(body, session_id)``.

    ``form`` is any value ``edn_write`` accepts -- typically a ``Lst`` verb
    call such as ``Lst([Symbol('hello')])``. It is encoded to EDN text, sent
    as ``application/edn`` UTF-8 bytes, and the response body is parsed back
    into Python values with ``edn_read``.

    Parameters
    ----------
    path : str
        Request path, e.g. ``"/v1/messages"`` or
        ``"/v1/sessions/s-1/messages"``.
    form : Any
        The EDN value to send as the request body.
    session : str | None
        If given, sent as the ``x-lemma-session`` request header so the
        server attaches the call to an existing session.
    base : str
        Scheme + host + port; defaults to :data:`DEFAULT_BASE`.

    Returns
    -------
    (body, session_id) : tuple
        ``body`` is the parsed EDN response. ``session_id`` is the value of
        the ``X-Lemma-Session`` response header (case-insensitive), or
        ``None`` if absent.

    Error handling
    --------------
    An HTTP error status (4xx/5xx) still carries a valid Lemma EDN *error
    envelope* in its body, so we parse and return that as ``body`` rather
    than discarding it -- the caller inspects ``:event`` to tell a welcome
    from an error. A connection-level failure (server down, refused) is
    re-raised as a ``ConnectionError`` that names the ``base`` URL.
    """
    payload = edn_write(form).encode("utf-8")

    headers = {"content-type": "application/edn"}
    if session is not None:
        headers["x-lemma-session"] = session

    request = urllib.request.Request(
        base + path, data=payload, headers=headers, method="POST"
    )

    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read().decode("utf-8")
            session_id = response.headers.get("X-Lemma-Session")
    except urllib.error.HTTPError as err:
        # A non-2xx status is still a structured Lemma reply: the body is an
        # EDN error envelope. Read and parse it so the caller sees :event,
        # and surface the session header if the server set one.
        raw = err.read().decode("utf-8")
        session_id = err.headers.get("X-Lemma-Session")
        return edn_read(raw), session_id
    except urllib.error.URLError as err:
        # No HTTP status at all -- we never reached a Lemma server. Name the
        # endpoint so the failure is actionable rather than a bare errno.
        raise ConnectionError(
            f"could not reach the Lemma server at {base!r} ({err.reason}); "
            "is the server running?"
        ) from err

    return edn_read(raw), session_id


# ---------------------------------------------------------------------------
# Runnable recipe:  the full Lemma round-trip
#
# This is the third and final layer the module docstring promised. It is a
# flat, linear retelling of examples/hello-http.clj: say hello, enter a world,
# propose a fact, assert it, query it back. Each step prints one human-readable
# line so a reader can follow the wire conversation by running the file.
#
# Everything network-y lives here (or in the __main__ guard) -- importing the
# module performs no I/O.
# ---------------------------------------------------------------------------

import sys

# The fields every reply may carry. Pulled out so the step-by-step code below
# reads as prose rather than a wall of Keyword(...) constructions.
_EVENT = Keyword(":event")
_ERROR = Keyword(":error")
_REJECTED = Keyword(":rejected")


def _is_failure(body):
    """Return the failing event Keyword if ``body`` is an error/rejection, else None.

    Every Lemma reply is a map keyed by ``:event``. Two event values mean the
    server refused the request: ``:error`` (malformed / illegal) and
    ``:rejected`` (well-formed but disallowed, e.g. a consistency violation).
    """
    event = body.get(_EVENT) if isinstance(body, dict) else None
    return event if event in (_ERROR, _REJECTED) else None


def _describe_failure(body):
    """Format the salient parts of an error/rejection envelope for printing.

    Pulls whichever of ``:reason`` / ``:message`` / ``:violations`` the server
    included; these are the fields that explain *why* a call was refused.
    """
    parts = []
    for key in (":reason", ":message", ":violations"):
        value = body.get(Keyword(key)) if isinstance(body, dict) else None
        if value is not None:
            parts.append(f"{key} {edn_write(value)}")
    return "; ".join(parts) if parts else "(no detail provided)"


def main(base=DEFAULT_BASE):
    """Run the full propose/assert/query round-trip against a Lemma server.

    Mirrors examples/hello-http.clj step for step. After every response we
    check ``:event``: an ``:error`` or ``:rejected`` envelope is printed and
    the sequence stops cleanly rather than crashing.
    """
    # 1. Anonymous hello. The welcome reply carries the new session id in the
    #    X-Lemma-Session response header, which post_edn surfaces for us.
    body, sid = post_edn("/v1/messages", Lst([Symbol("hello")]), base=base)
    if not isinstance(body, dict) or body.get(_EVENT) != Keyword(":welcome"):
        event = body.get(_EVENT) if isinstance(body, dict) else None
        print(f"hello: expected :welcome, got {edn_write(event)}"
              f" -- {_describe_failure(body)}")
        return
    print(f"hello -> :welcome  version={edn_write(body.get(Keyword(':version')))}"
          f"  session={sid}  world={edn_write(body.get(Keyword(':world')))}")

    # 2. Every later call rides the same session, on the named endpoint.
    named = lambda form: post_edn(
        f"/v1/sessions/{sid}/messages", form, session=sid, base=base
    )

    # 3. Enter the world. (use-world #world "default")
    body, _ = named(Lst([Symbol("use-world"), Tagged("world", "default")]))
    if _is_failure(body):
        print(f"use-world refused: {_describe_failure(body)}")
        return
    print(f"use-world \"default\" -> {edn_write(body.get(_EVENT))}"
          f"  world={edn_write(body.get(Keyword(':world')))}")

    # 4. Propose a fact: morningstar is equivalent to venus. The reply hands
    #    back a #proposal handle we feed straight into the assert.
    fact = Tagged("fact", {
        Keyword(":predicate"): Symbol("equivalent"),
        Keyword(":subject"): Tagged("entity", "morningstar"),
        Keyword(":object"): Tagged("entity", "venus"),
    })
    body, _ = named(Lst([Symbol("propose"), fact]))
    if _is_failure(body):
        print(f"propose refused: {_describe_failure(body)}")
        return
    proposal = body.get(Keyword(":proposal"))
    print(f"propose (equivalent morningstar venus) -> {edn_write(body.get(_EVENT))}"
          f"  proposal={edn_write(proposal)}")

    # 5. Assert the proposed fact into the world.
    body, _ = named(Lst([Symbol("assert"), proposal]))
    if _is_failure(body):
        print(f"assert refused: {_describe_failure(body)}")
        return
    print(f"assert proposal -> {edn_write(body.get(_EVENT))}")

    # 6. Query it back. Note :find / :where are VECTORS (Python lists), and the
    #    where-clause is a vector of vectors; only the verb head is a Lst.
    body, _ = named(Lst([Symbol("query"), {
        Keyword(":find"): [Symbol("?o")],
        Keyword(":where"): [[Symbol("equivalent"), Tagged("entity", "morningstar"), Symbol("?o")]],
    }]))
    if _is_failure(body):
        print(f"query refused: {_describe_failure(body)}")
        return
    print(f"query (equivalent morningstar ?o) -> rows={edn_write(body.get(Keyword(':rows')))}"
          f"  done?={edn_write(body.get(Keyword(':done?')))}")


if __name__ == "__main__":
    # An optional first argument overrides the server base URL; otherwise we
    # talk to the local default. No network happens at import time -- only here.
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE)
