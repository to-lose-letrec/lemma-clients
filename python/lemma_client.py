#!/usr/bin/env python3
"""A single-file Python client for the Lemma wire protocol.

This module is a *recipe*, not a library: it is meant to be read end to end.
The first thing any Lemma client needs is a way to turn Python values into the
EDN text the server speaks, and to turn the server's EDN responses back into
Python values. Rather than hand-roll that codec, this client leans on the
``edn_format`` library (the one third-party dependency); everything else is
stdlib. On top of the codec sit an HTTP transport, a UDS transport, and a
runnable ``main()`` that walks the full propose/assert/query round-trip.

EDN in a nutshell
-----------------
EDN (Extensible Data Notation) is Clojure's data syntax. Lemma uses a small,
well-defined subset of it (see ``lemma/grammar/lemma.lark``). The pieces we
care about and their ``edn_format`` Python mappings:

    nil true false              -- None / True / False
    42  -3  3.14  3.14e2         -- int / float
    "a string\n"                -- str
    :event  :verbs/core         -- edn_format.Keyword
    equivalent  member-of  ?o   -- edn_format.Symbol (``?``-vars are symbols)
    ( a b c )                   -- a Python TUPLE   (an EDN LIST)
    [ a b c ]                   -- a Python LIST    (an EDN VECTOR)
    { k v, k v }                -- a Python dict    (an EDN MAP)
    #{ a b c }                  -- a Python set / frozenset
    #tag payload                -- an edn_format.TaggedElement

Lists versus vectors -- the one design decision
-----------------------------------------------
EDN distinguishes lists ``( ... )`` from vectors ``[ ... ]``, and Lemma relies
on the distinction (grammar §3): a *list* appears **only** as the top-level
verb form -- ``(propose ...)``, ``(query ...)``, ``(hello)``. Everywhere inside
the arguments, collections are *vectors* (``:find [?x]``, ``:where [[...]]``),
maps, or sets -- never lists.

``edn_format`` already encodes this distinction in the Python type system, so
we simply use it rather than inventing a wrapper:

    * Python ``tuple`` -> EDN LIST ``( ... )``   (verb forms only)
    * Python ``list``  -> EDN VECTOR ``[ ... ]`` (the overwhelmingly common case)

    >>> dumps((Symbol("use-world"), world("default")))
    '(use-world #world "default")'

On the read side ``edn_format.loads`` yields the inverse, so a verb form reads
back as a tuple and a vector as a list.

Tagged literals
---------------
The ten core Lemma tags are ``#fact #violation #entity #proposal #tx #ref
#cursor #watch #session #world`` (grammar §5). We register each as a
``TaggedElement`` subclass so it round-trips in BOTH directions: ``loads``
reconstructs the object from wire text, and ``dumps`` re-emits the exact wire
text. ``#entity``, ``#world`` and the rest carry a string payload; ``#fact``
and ``#violation`` carry a map. Tags we do not register (e.g. ``#inst``) are
still parsed by ``edn_format``'s built-in handlers, so an unexpected tag never
breaks a response.

Importing this module performs no network I/O -- only ``main`` / ``main_uds``
(and the ``__main__`` guard) touch the network.
"""

from __future__ import annotations

import edn_format
from edn_format import Keyword, Symbol, TaggedElement, dumps, loads

# ``loads`` returns an ImmutableDict for EDN maps, which is NOT a subclass of
# dict; we test against both so the envelope-inspection helpers work on parsed
# responses as well as on dicts we build ourselves.
_MAPPING_TYPES = (dict, edn_format.ImmutableDict)


# ---------------------------------------------------------------------------
# Tagged literals:  #tag payload  <->  TaggedElement
#
# A TaggedElement subclass needs two things: a ``name`` (the tag, sans ``#``)
# and a ``__str__`` that renders the full ``#tag payload`` wire text. We split
# the ten core tags by payload kind -- string handles versus map-bearing
# #fact / #violation -- and register a reader for each so responses parse back
# into these same objects.
#
# Note the space in ``#fact {...}`` / ``#tag "x"``: it is wire-valid (clojure
# .edn ignores whitespace between a tag and its payload) and keeps the output
# readable. The grammar's ``#fact{...}`` (no space) is equally legal on input.
# ---------------------------------------------------------------------------

# The eight tags whose payload is a single string (grammar §5, §5.3).
_STRING_TAGS = ("entity", "world", "proposal", "tx", "ref", "cursor", "watch", "session")


class _Handle(TaggedElement):
    """A string-payload tag such as ``#entity "alice"`` or ``#world "default"``.

    ``name`` is the tag without the leading ``#``; ``value`` is the string
    payload. The single class backs all eight string-payload tags -- which tag
    a given instance represents is just its ``name``.
    """

    def __init__(self, name, value):
        self.name = name
        self.value = value

    def __str__(self):
        return f"#{self.name} {dumps(self.value)}"

    def __eq__(self, other):
        return (
            isinstance(other, _Handle)
            and other.name == self.name
            and other.value == self.value
        )

    def __hash__(self):
        return hash(("_Handle", self.name, self.value))

    def __repr__(self):
        return f"_Handle({self.name!r}, {self.value!r})"


# Register a reader for each string-payload tag. The inner factory binds the
# tag name per-iteration so every handler builds a _Handle with the right name.
for _tag in _STRING_TAGS:
    edn_format.add_tag(_tag, (lambda t: (lambda v: _Handle(t, v)))(_tag))


class _Fact(TaggedElement):
    """The ``#fact{...}`` tag (grammar §5.1) -- a map payload.

    The map carries some combination of ``:predicate`` / ``:subject`` /
    ``:object`` / ``:args``; we keep it verbatim and let the server enforce the
    legal shapes.
    """

    name = "fact"

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return f"#fact {dumps(self.value)}"

    def __eq__(self, other):
        return isinstance(other, _Fact) and other.value == self.value

    def __repr__(self):
        return f"_Fact({self.value!r})"


class _Violation(TaggedElement):
    """The ``#violation{...}`` tag (grammar §5.2) -- a server-emitted map payload.

    Registered so a violation in a response parses into an object that
    round-trips cleanly back onto the wire if an agent feeds it into a later
    query or argument position.
    """

    name = "violation"

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return f"#violation {dumps(self.value)}"

    def __eq__(self, other):
        return isinstance(other, _Violation) and other.value == self.value

    def __repr__(self):
        return f"_Violation({self.value!r})"


edn_format.add_tag("fact", _Fact)
edn_format.add_tag("violation", _Violation)


# ---------------------------------------------------------------------------
# Constructor helpers
#
# Thin wrappers so the round-trip code below reads as prose rather than as a
# wall of tag-class constructions. They mirror the grammar's payload shapes.
# ---------------------------------------------------------------------------


def entity(name):
    """Build an ``#entity "<name>"`` handle (grammar §5.3)."""
    return _Handle("entity", name)


def world(name):
    """Build a ``#world "<name>"`` handle (grammar §5)."""
    return _Handle("world", name)


def fact(predicate, subject, object):
    """Build a ``#fact{...}`` binary fact: ``(predicate subject object)``.

    ``predicate`` is a Symbol; ``subject`` / ``object`` are typically
    ``#entity`` handles. The keys are the grammar's reserved fact keys.
    """
    return _Fact({
        Keyword("predicate"): predicate,
        Keyword("subject"): subject,
        Keyword("object"): object,
    })


# ---------------------------------------------------------------------------
# HTTP transport:  EDN form  ->  POST  ->  parsed EDN response
#
# With the codec in hand, talking to a Lemma server is just "encode, POST,
# decode". A flat stdlib-only helper, not an abstraction. The session protocol
# (SPEC §3) is:
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

    ``form`` is any value ``edn_format.dumps`` accepts -- typically a tuple
    verb call such as ``(Symbol("hello"),)``. It is encoded to EDN text, sent
    as ``application/edn`` UTF-8 bytes, and the response body is parsed back
    into Python values with ``edn_format.loads``.

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
    payload = dumps(form).encode("utf-8")

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
        return loads(raw), session_id
    except urllib.error.URLError as err:
        # No HTTP status at all -- we never reached a Lemma server. Name the
        # endpoint so the failure is actionable rather than a bare errno.
        raise ConnectionError(
            f"could not reach the Lemma server at {base!r} ({err.reason}); "
            "is the server running?"
        ) from err

    return loads(raw), session_id


# ---------------------------------------------------------------------------
# UDS transport:  EDN form  ->  length-prefixed frame  ->  parsed EDN response
#
# A second transport that speaks the same EDN codec over a Unix domain socket
# instead of HTTP. It sits alongside post_edn rather than replacing it -- same
# "encode, send, decode" shape, different plumbing. Two things differ from HTTP:
#
#   * Framing. There is no HTTP envelope, so each message is delimited
#     explicitly: a 4-byte big-endian UNSIGNED length prefix followed by that
#     many UTF-8 bytes of EDN. This matches Dianoia's transport/uds.clj
#     write-frame / read-frame exactly (DataOutputStream.writeInt is a 4-byte
#     big-endian int).
#   * Session binding. Over HTTP the client threads the session id back into
#     each request header. Over UDS the server binds the session to the
#     *connection*: it captures the id from the welcome envelope and attaches
#     it to the socket (see uds.clj handle-frame / build-ctx). So the client
#     must NOT echo the session id into later frames -- it just keeps sending
#     on the same socket, and the server already knows who it is.
#
# Stdlib only for the plumbing: socket for the connection, struct for the
# length prefix; the EDN codec is edn_format.
# ---------------------------------------------------------------------------

import socket
import struct

# Where a locally booted Dianoia UDS listener binds by default (see uds.clj
# start! :socket-path). Override per call via the ``socket_path`` argument.
DEFAULT_SOCKET = "/tmp/dianoia.sock"


def _recv_exactly(sock, n):
    """Read exactly ``n`` bytes from ``sock``, looping until satisfied.

    ``socket.recv`` may return fewer bytes than requested, so we accumulate
    until we have all ``n``. A return of ``b""`` means the peer closed the
    connection; getting that before ``n`` bytes is a truncated frame, which we
    surface as a ``ConnectionError`` rather than a short read.
    """
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if chunk == b"":
            raise ConnectionError(
                f"connection closed with {remaining} of {n} bytes still expected"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def uds_send_frame(sock, edn_str):
    """Frame ``edn_str`` and send it: 4-byte big-endian length, then the body.

    The body is the UTF-8 encoding of ``edn_str``; the prefix is its byte
    length packed as ``>I`` (big-endian unsigned 32-bit). ``sendall`` keeps
    writing until every byte is on the wire. Mirrors uds.clj write-frame.
    """
    body = edn_str.encode("utf-8")
    sock.sendall(struct.pack(">I", len(body)) + body)


def uds_recv_frame(sock):
    """Read one length-prefixed frame and return its body as a ``str``.

    The inverse of :func:`uds_send_frame`: read the 4-byte big-endian length,
    then read exactly that many body bytes, then decode UTF-8. Mirrors uds.clj
    read-frame.
    """
    (length,) = struct.unpack(">I", _recv_exactly(sock, 4))
    return _recv_exactly(sock, length).decode("utf-8")


# ---------------------------------------------------------------------------
# Runnable recipe:  the full Lemma round-trip
#
# A flat, linear retelling of examples/hello-http.clj: say hello, enter a
# world, propose a fact, assert it, query it back. Each step prints one
# human-readable line so a reader can follow the wire conversation by running
# the file. Everything network-y lives here (or in the __main__ guard) --
# importing the module performs no I/O.
# ---------------------------------------------------------------------------

import sys

# The fields every reply may carry. Pulled out so the step-by-step code below
# reads as prose rather than a wall of Keyword(...) constructions.
_EVENT = Keyword("event")
_ERROR = Keyword("error")
_REJECTED = Keyword("rejected")


def _is_failure(body):
    """Return the failing event Keyword if ``body`` is an error/rejection, else None.

    Every Lemma reply is a map keyed by ``:event``. Two event values mean the
    server refused the request: ``:error`` (malformed / illegal) and
    ``:rejected`` (well-formed but disallowed, e.g. a consistency violation).
    """
    event = body.get(_EVENT) if isinstance(body, _MAPPING_TYPES) else None
    return event if event in (_ERROR, _REJECTED) else None


def _describe_failure(body):
    """Format the salient parts of an error/rejection envelope for printing.

    Pulls whichever of ``:reason`` / ``:message`` / ``:violations`` the server
    included; these are the fields that explain *why* a call was refused.
    """
    parts = []
    for key in ("reason", "message", "violations"):
        value = body.get(Keyword(key)) if isinstance(body, _MAPPING_TYPES) else None
        if value is not None:
            parts.append(f":{key} {dumps(value)}")
    return "; ".join(parts) if parts else "(no detail provided)"


# The pagination keys a (query …)/(continue …) result envelope carries
# (SPEC §8). A query with :limit returns a full first page with :done? false
# plus a #cursor; continue carries :rows/:cursor/:done? until :done? is true.
_ROWS = Keyword("rows")
_DONE = Keyword("done?")
_CURSOR = Keyword("cursor")


def query_all(send, query_form):
    """Run a (query ...) and drain every page via (continue #cursor ...).

    `send` is a `form -> body` callable (the per-transport closure). Returns
    (rows, pages, failure) where `failure` is None on success or the offending
    error/rejection body. A query with :limit returns a full first page with
    :done? false and a #cursor; we (continue #cursor) until :done? is true.
    """
    body = send(query_form)
    if _is_failure(body):
        return ([], 0, body)

    rows = list(body[_ROWS])
    pages = 1
    while not body[_DONE]:
        # :cursor is present exactly when :done? is false -- the server omits
        # it on a single-page (already-done) result, so we only read it here.
        # An expired cursor (server idle TTL ~300s, SPEC §8) comes back as
        # :error :unknown-handle; this demo propagates that failure, whereas a
        # real client would re-issue the original query to start a fresh page.
        cursor = body[_CURSOR]
        body = send((Symbol("continue"), cursor))
        if _is_failure(body):
            return (rows, pages, body)
        rows.extend(body[_ROWS])
        pages += 1
    return (rows, pages, None)


def main(base=DEFAULT_BASE):
    """Run the full propose/assert/query round-trip against a Lemma server.

    Mirrors examples/hello-http.clj step for step. After every response we
    check ``:event``: an ``:error`` or ``:rejected`` envelope is printed and
    the sequence stops cleanly rather than crashing.
    """
    # 1. Anonymous hello. The welcome reply carries the new session id in the
    #    X-Lemma-Session response header, which post_edn surfaces for us.
    body, sid = post_edn("/v1/messages", (Symbol("hello"),), base=base)
    if not isinstance(body, _MAPPING_TYPES) or body.get(_EVENT) != Keyword("welcome"):
        event = body.get(_EVENT) if isinstance(body, _MAPPING_TYPES) else None
        print(f"hello: expected :welcome, got {dumps(event)}"
              f" -- {_describe_failure(body)}")
        return
    print(f"hello -> :welcome  version={dumps(body.get(Keyword('version')))}"
          f"  session={sid}  world={dumps(body.get(Keyword('world')))}")

    # 2. Every later call rides the same session, on the named endpoint.
    named = lambda form: post_edn(
        f"/v1/sessions/{sid}/messages", form, session=sid, base=base
    )

    # 3. Enter the world. (use-world #world "default")
    body, _ = named((Symbol("use-world"), world("default")))
    if _is_failure(body):
        print(f"use-world refused: {_describe_failure(body)}")
        return
    print(f"use-world \"default\" -> {dumps(body.get(_EVENT))}"
          f"  world={dumps(body.get(Keyword('world')))}")

    # 4. Propose a fact: morningstar is equivalent to venus. The reply hands
    #    back a #proposal handle we feed straight into the assert.
    f = fact(Symbol("equivalent"), entity("morningstar"), entity("venus"))
    body, _ = named((Symbol("propose"), f))
    if _is_failure(body):
        print(f"propose refused: {_describe_failure(body)}")
        return
    proposal = body.get(Keyword("proposal"))
    print(f"propose (equivalent morningstar venus) -> {dumps(body.get(_EVENT))}"
          f"  proposal={dumps(proposal)}")

    # 5. Assert the proposed fact into the world.
    body, _ = named((Symbol("assert"), proposal))
    if _is_failure(body):
        print(f"assert refused: {_describe_failure(body)}")
        return
    print(f"assert proposal -> {dumps(body.get(_EVENT))}")

    # 6. Query it back. Note :find / :where are VECTORS (Python lists), and the
    #    where-clause is a vector of vectors; only the verb head is a tuple.
    body, _ = named((Symbol("query"), {
        Keyword("find"): [Symbol("?o")],
        Keyword("where"): [[Symbol("equivalent"), entity("morningstar"), Symbol("?o")]],
    }))
    if _is_failure(body):
        print(f"query refused: {_describe_failure(body)}")
        return
    print(f"query (equivalent morningstar ?o) -> rows={dumps(body.get(Keyword('rows')))}"
          f"  done?={dumps(body.get(Keyword('done?')))}")

    # 7. Seed three subset-of facts in one propose, then assert the batch, so
    #    the paginated query below has more rows than a single page holds.
    #    subset-of is a pure-EDB (stored-fact) predicate, so a query over it
    #    has stable (tx-id, ref-id) ordering and can be paginated; a rule-headed
    #    predicate like member-of cannot be the sole outer :where pattern (the
    #    server rejects it :bad-args :unsupported-rule-call-ordering).
    f1 = fact(Symbol("subset-of"), entity("sub-a"), entity("group"))
    f2 = fact(Symbol("subset-of"), entity("sub-b"), entity("group"))
    f3 = fact(Symbol("subset-of"), entity("sub-c"), entity("group"))
    body, _ = named((Symbol("propose"), f1, f2, f3))
    if _is_failure(body):
        print(f"propose (3x subset-of) refused: {_describe_failure(body)}")
        return
    proposal = body.get(Keyword("proposal"))
    print(f"propose (3x subset-of ? group) -> {dumps(body.get(_EVENT))}"
          f"  proposal={dumps(proposal)}")
    body, _ = named((Symbol("assert"), proposal))
    if _is_failure(body):
        print(f"assert (3x subset-of) refused: {_describe_failure(body)}")
        return
    print(f"assert proposal -> {dumps(body.get(_EVENT))}")

    # 8. Paginated query: :limit 2 over 3 matching rows yields two pages
    #    (2 + 1). query_all drains them via (continue #cursor ...).
    qform = (Symbol("query"), {
        Keyword("find"): [Symbol("?x")],
        Keyword("where"): [[Symbol("subset-of"), Symbol("?x"), entity("group")]],
        Keyword("limit"): 2,
    })
    # query_all wants a form -> body closure; named returns (body, sid), so we
    # adapt it by dropping the (connection-stable) session id.
    rows, pages, failure = query_all(lambda form: named(form)[0], qform)
    if failure:
        print(f"paged query refused: {_describe_failure(failure)}")
        return
    print(f"paged query (subset-of ? group), limit 2 -> {len(rows)} rows over "
          f"{pages} page(s): {dumps(rows)}")


def main_uds(socket_path=DEFAULT_SOCKET):
    """Run the same propose/assert/query round-trip over a Unix domain socket.

    Step for step this is the HTTP :func:`main` -- hello, enter a world,
    propose a fact, assert it, query it back -- but spoken over a UDS frame
    stream. The one protocol difference is session handling: the server binds
    the session to the connection from the welcome envelope (uds.clj
    handle-frame), so we do NOT thread the session id into later frames. Every
    call after hello simply rides the same open socket.

    After each response we check ``:event``; an ``:error`` or ``:rejected``
    envelope is printed and the sequence stops cleanly rather than crashing.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            sock.connect(socket_path)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as err:
            # No listener at the path: the socket file is missing, or nothing
            # is accepting on it. Name the path so the failure is actionable.
            raise ConnectionError(
                f"could not connect to the Lemma UDS server at {socket_path!r} "
                f"({err}); is the server running?"
            ) from err

        # One round-trip: frame out, frame in, decode. The session lives on the
        # connection -- no id is echoed back, unlike the HTTP transport.
        def call(form):
            uds_send_frame(sock, dumps(form))
            return loads(uds_recv_frame(sock))

        # 1. Anonymous hello. The welcome reply carries the session id, which
        #    the server has already pinned to this connection for us.
        body = call((Symbol("hello"),))
        if not isinstance(body, _MAPPING_TYPES) or body.get(_EVENT) != Keyword("welcome"):
            event = body.get(_EVENT) if isinstance(body, _MAPPING_TYPES) else None
            print(f"hello: expected :welcome, got {dumps(event)}"
                  f" -- {_describe_failure(body)}")
            return
        print(f"hello -> :welcome  version={dumps(body.get(Keyword('version')))}"
              f"  session={dumps(body.get(Keyword('session')))}"
              f"  world={dumps(body.get(Keyword('world')))}")

        # 2. Enter the world. (use-world #world "default")
        body = call((Symbol("use-world"), world("default")))
        if _is_failure(body):
            print(f"use-world refused: {_describe_failure(body)}")
            return
        print(f"use-world \"default\" -> {dumps(body.get(_EVENT))}"
              f"  world={dumps(body.get(Keyword('world')))}")

        # 3. Propose a fact: morningstar is equivalent to venus. The reply hands
        #    back a #proposal handle we feed straight into the assert.
        f = fact(Symbol("equivalent"), entity("morningstar"), entity("venus"))
        body = call((Symbol("propose"), f))
        if _is_failure(body):
            print(f"propose refused: {_describe_failure(body)}")
            return
        proposal = body.get(Keyword("proposal"))
        print(f"propose (equivalent morningstar venus) -> {dumps(body.get(_EVENT))}"
              f"  proposal={dumps(proposal)}")

        # 4. Assert the proposed fact into the world.
        body = call((Symbol("assert"), proposal))
        if _is_failure(body):
            print(f"assert refused: {_describe_failure(body)}")
            return
        print(f"assert proposal -> {dumps(body.get(_EVENT))}")

        # 5. Query it back. As in the HTTP path, :find / :where are VECTORS
        #    (Python lists); only the verb head is a tuple.
        body = call((Symbol("query"), {
            Keyword("find"): [Symbol("?o")],
            Keyword("where"): [[Symbol("equivalent"), entity("morningstar"), Symbol("?o")]],
        }))
        if _is_failure(body):
            print(f"query refused: {_describe_failure(body)}")
            return
        print(f"query (equivalent morningstar ?o) -> rows={dumps(body.get(Keyword('rows')))}"
              f"  done?={dumps(body.get(Keyword('done?')))}")

        # 6. Seed three subset-of facts in one propose, then assert the batch,
        #    so the paginated query below spans more than one page. subset-of is
        #    a pure-EDB predicate (stable tx-id/ref-id ordering), so it can be
        #    paginated; a rule-headed predicate like member-of cannot be the sole
        #    outer :where pattern (server :bad-args :unsupported-rule-call-ordering).
        f1 = fact(Symbol("subset-of"), entity("sub-a"), entity("group"))
        f2 = fact(Symbol("subset-of"), entity("sub-b"), entity("group"))
        f3 = fact(Symbol("subset-of"), entity("sub-c"), entity("group"))
        body = call((Symbol("propose"), f1, f2, f3))
        if _is_failure(body):
            print(f"propose (3x subset-of) refused: {_describe_failure(body)}")
            return
        proposal = body.get(Keyword("proposal"))
        print(f"propose (3x subset-of ? group) -> {dumps(body.get(_EVENT))}"
              f"  proposal={dumps(proposal)}")
        body = call((Symbol("assert"), proposal))
        if _is_failure(body):
            print(f"assert (3x subset-of) refused: {_describe_failure(body)}")
            return
        print(f"assert proposal -> {dumps(body.get(_EVENT))}")

        # 7. Paginated query: :limit 2 over 3 matching rows yields two pages
        #    (2 + 1). query_all drains them via (continue #cursor ...). The UDS
        #    call closure is already form -> body, so it is passed directly.
        qform = (Symbol("query"), {
            Keyword("find"): [Symbol("?x")],
            Keyword("where"): [[Symbol("subset-of"), Symbol("?x"), entity("group")]],
            Keyword("limit"): 2,
        })
        rows, pages, failure = query_all(call, qform)
        if failure:
            print(f"paged query refused: {_describe_failure(failure)}")
            return
        print(f"paged query (subset-of ? group), limit 2 -> {len(rows)} rows over "
              f"{pages} page(s): {dumps(rows)}")
    finally:
        # Always release the socket, success or failure. Closing it also lets
        # the server's reader thread observe EOF and drop the session.
        sock.close()


def _dispatch(argv):
    """Route CLI arguments (``sys.argv[1:]``) to a transport.

    With no arguments we keep the original HTTP behaviour against the local
    default. A leading ``"uds"`` selects the Unix-domain-socket transport (with
    an optional socket path). Any other leading argument is an HTTP base URL.
    """
    if argv and argv[0] == "uds":
        main_uds(argv[1] if len(argv) > 1 else DEFAULT_SOCKET)
    elif argv:
        main(argv[0])
    else:
        main(DEFAULT_BASE)


if __name__ == "__main__":
    # No network happens at import time -- only here.
    _dispatch(sys.argv[1:])
