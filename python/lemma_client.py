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
# Capabilities & limits:  the :welcome surface  ->  ServerInfo
#
# Every session opens with a (hello) whose :welcome reply advertises what the
# server can do (SPEC §10): a :capabilities set of namespaced flag keywords, a
# :limits map of resource caps, and the :verbs / :predicates the world exposes.
# A well-behaved client reads this once and tailors itself to it -- skipping
# features the server doesn't advertise and staying under the byte caps it
# enforces. ServerInfo is the parsed, queryable form of that surface; it is a
# plain data record (not a new abstraction layer), so the round-trip code below
# can ask "does this server paginate?" or "is my message small enough?" in one
# readable call.
# ---------------------------------------------------------------------------

from typing import NamedTuple


class ServerInfo(NamedTuple):
    """The parsed :welcome surface: what this server advertises (SPEC §10).

    Fields mirror the welcome map. ``capabilities`` is a frozenset of Keyword
    flags (e.g. ``Keyword("lemma/cursor-pagination")``); ``limits`` is a plain
    dict of Keyword -> value resource caps; ``verbs`` and ``predicates`` are
    flat sets of Symbol names with the :core and :extensions surfaces merged.
    """

    version: object
    capabilities: frozenset
    limits: dict
    verbs: set
    predicates: set

    def supports(self, capability):
        """True iff ``capability`` (a Keyword) is in the advertised set."""
        return capability in self.capabilities

    @property
    def max_message_bytes(self):
        """The :max-message-bytes limit, or None if the server didn't advertise one.

        A ``None`` value means "unadvertised" -- treated as unlimited by
        :func:`within_message_limit`.
        """
        return self.limits.get(Keyword("max-message-bytes"))


def _flatten_surface(surface):
    """Merge a ``{:core #{…} :extensions {pack #{…}}}`` map into one flat set.

    The :verbs and :predicates entries of a welcome split names into a :core
    set plus per-pack :extensions sets (SPEC §10). A client mostly just wants
    "every name this server understands", so we union :core with all the
    extension sets. Missing keys default to empty -- a minimal welcome need not
    carry every section.
    """
    if not isinstance(surface, _MAPPING_TYPES):
        return set()
    names = set(surface.get(Keyword("core")) or ())
    extensions = surface.get(Keyword("extensions"))
    if isinstance(extensions, _MAPPING_TYPES):
        for pack_names in extensions.values():
            names.update(pack_names or ())
    return names


def read_welcome(body):
    """Parse a :welcome envelope into a :class:`ServerInfo`.

    ``body`` is the parsed welcome map (an ImmutableDict from
    ``edn_format.loads``). We pull :version, :capabilities (a set of Keyword),
    :limits (an ImmutableDict, copied into a plain dict so callers can treat it
    as ordinary data), and the flattened :verbs / :predicates surfaces. Every
    key is optional: a server that omits a section yields an empty default
    rather than an error, so this stays robust against minimal welcomes.
    """
    capabilities = frozenset(body.get(Keyword("capabilities")) or ())
    limits = dict(body.get(Keyword("limits")) or {})
    return ServerInfo(
        version=body.get(Keyword("version")),
        capabilities=capabilities,
        limits=limits,
        verbs=_flatten_surface(body.get(Keyword("verbs"))),
        predicates=_flatten_surface(body.get(Keyword("predicates"))),
    )


def within_message_limit(info, edn_text):
    """True iff ``edn_text`` fits under the server's :max-message-bytes cap.

    The limit is measured in UTF-8 bytes (SPEC §10). An unadvertised limit
    (``max_message_bytes is None``) means unlimited, so any message passes.
    """
    cap = info.max_message_bytes
    if cap is None:
        return True
    return len(edn_text.encode("utf-8")) <= cap


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
import urllib.parse
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


def _uds_await_watch_event(sock, max_frames=8):
    """Read framed replies until a ``:watch-event`` arrives; return it (or None).

    Over UDS there is no separate event channel: watch pushes interleave with
    ordinary command responses on the *same* frame stream (uds.clj fans both
    onto the one connection). So after triggering a change we read frames in a
    loop, skipping command replies (the ``:asserted`` echo, etc.) until we see
    the ``:watch-event`` envelope. The loop is bounded by ``max_frames`` and the
    socket's timeout so a missing push can never hang -- it returns ``None``,
    which the caller reports as "no event observed".
    """
    for _ in range(max_frames):
        try:
            body = loads(uds_recv_frame(sock))
        except (ConnectionError, socket.timeout):
            return None
        if (isinstance(body, _MAPPING_TYPES)
                and body.get(Keyword("event")) == Keyword("watch-event")):
            return body
    return None


# ---------------------------------------------------------------------------
# Watch over HTTP:  the SSE event stream  ->  parsed :watch-event envelopes
#
# A (watch-pattern ...) call registers a standing query; matching changes are
# then *pushed* to the session rather than polled. Over HTTP those pushes
# arrive on a separate Server-Sent-Events stream, GET /v1/sessions/{id}/events
# (SPEC §9). SSE is a one-way text stream: each event is one or more
# ``data:`` lines terminated by a blank line; ``:``-prefixed lines are
# keep-alive comments to be ignored.
#
# Why a raw socket instead of urllib? Dianoia (http-kit) serves the stream with
# ``Transfer-Encoding: chunked`` and writes an immediate size-0 chunk to flush
# the response headers before any event exists. A standard chunked reader
# (urllib / http.client) treats that size-0 chunk as end-of-body and reports
# EOF, closing the stream before the first event ever arrives. So we speak HTTP
# by hand over a raw socket -- the same plumbing the UDS transport already uses
# -- and treat a size-0 chunk as a keep-alive flush (skip it, keep reading)
# rather than as the end of the stream. Every read is bounded by ``timeout`` so
# a quiet stream can never hang the demo.
#
# ORDERING IS LOAD-BEARING. Dianoia registers the per-session SSE sink LAZILY,
# at the moment the GET /events connection's headers are written -- and the
# watch dispatcher delivers a :watch-event only to sinks present at emit time,
# with NO backlog replay. So the stream must be OPENED (sink registered) BEFORE
# the change that triggers the event, or the push races ahead of the sink and
# is lost. We therefore split the work in two:
#
#   * open_sse_stream -- connect, send the GET, read PAST the status line and
#     headers (writing the request + draining headers is what makes Dianoia
#     register the sink), and hand back an open handle. Call this BEFORE the
#     trigger.
#   * read_sse_events -- drain parsed events from an already-open handle, AFTER
#     the trigger. Bounded by the handle's socket timeout.
#
# This is read-only and single-threaded by design. Stdlib only (socket +
# urllib.parse); the EDN codec is edn_format.
# ---------------------------------------------------------------------------


class _SSEStream:
    """An open SSE connection: the socket plus the byte buffer carried across
    the header read into the body decode.

    ``open_sse_stream`` builds one (connection live, headers consumed, server
    sink registered); ``read_sse_events`` drains events from it; ``close``
    releases the socket so the server drops the stream. ``buf`` holds any body
    bytes already read past the header terminator so the chunked decoder does
    not lose them.
    """

    def __init__(self, sock, buf):
        self.sock = sock
        self.buf = buf

    def close(self):
        """Release the socket, letting the server tear down the stream."""
        self.sock.close()


def open_sse_stream(base, session_id, timeout=10.0):
    """Open the SSE event stream for ``session_id`` and return an :class:`_SSEStream`.

    Connects a raw socket to the host/port parsed from ``base`` (e.g.
    ``"http://127.0.0.1:8080"``), issues ``GET /v1/sessions/{id}/events`` with
    an ``Accept: text/event-stream`` header, and reads PAST the status line and
    response headers -- stopping at the blank line that begins the body. It does
    NOT read any event bodies; that is :func:`read_sse_events`'s job.

    The split matters because writing the GET and draining its headers is what
    makes Dianoia register this session's SSE sink, and the watch dispatcher
    only delivers to sinks that exist when an event is emitted (no replay). So a
    caller must open the stream BEFORE triggering the change it wants to observe,
    then read AFTER -- otherwise the push races ahead of the sink and is lost.

    Parameters
    ----------
    base : str
        Scheme + host + port, as passed to :func:`post_edn`.
    session_id : str
        The session whose event stream to open (its watches' pushes land here).
    timeout : float
        Per-read socket timeout in seconds, stored on the socket so subsequent
        :func:`read_sse_events` calls inherit it.

    Returns
    -------
    _SSEStream
        An open handle. The caller must :meth:`_SSEStream.close` it when done
        (or pass it to :func:`read_sse_events`, which closes on its own errors
        only -- normal teardown stays with the caller).

    Notes
    -----
    If the server closes the connection before the header terminator arrives,
    the returned handle is still valid but its buffer is empty; the subsequent
    read will see EOF and yield no events.
    """
    parts = urllib.parse.urlsplit(base)
    host = parts.hostname
    port = parts.port or 80

    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)

    request = (
        f"GET /v1/sessions/{session_id}/events HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Accept: text/event-stream\r\n"
        f"X-Lemma-Session: {session_id}\r\n"
        f"Connection: keep-alive\r\n\r\n"
    )
    sock.sendall(request.encode("utf-8"))

    # Consume the status line and headers; the body starts after the blank
    # line. Anything already read past it is retained on the handle so the
    # chunked decoder in read_sse_events does not lose those bytes. Draining
    # the headers here is the act that registers the server-side sink.
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                # Server closed before headers completed: hand back an empty
                # buffer; the read will see EOF and report no events.
                return _SSEStream(sock, b"")
            buf += chunk
    except (socket.timeout, ConnectionError):
        # Quiet or broken connection during the header read: still hand back a
        # handle so the caller's try/finally can close it uniformly.
        return _SSEStream(sock, b"")
    _, _, body = buf.partition(b"\r\n\r\n")
    return _SSEStream(sock, body)


def read_sse_events(stream, max_events=1):
    """Drain up to ``max_events`` parsed envelopes from an open :class:`_SSEStream`.

    ``stream`` is the handle returned by :func:`open_sse_stream` (its socket is
    live and its headers already consumed). This transfer-decodes the chunked
    body and parses Server-Sent Events out of it: each event's ``data:`` lines
    are concatenated and run through ``edn_format.loads``, so the return value
    is a list of parsed envelopes (typically ``:watch-event`` maps).

    Parameters
    ----------
    stream : _SSEStream
        An open stream handle from :func:`open_sse_stream`.
    max_events : int
        Stop and return after this many SSE events have been parsed.

    Returns
    -------
    list
        The parsed envelopes. A ``socket.timeout`` (the per-read timeout set on
        the socket by :func:`open_sse_stream`) or end of stream ends the read
        and returns whatever arrived so far, so a quiet stream degrades to an
        empty list rather than hanging.

    Notes
    -----
    A size-0 chunk is http-kit's header-flush keep-alive, NOT end-of-stream, so
    we skip it and keep reading. Genuine connection close (``recv`` returns
    ``b""``) ends the read. The caller owns the socket and closes it; this only
    reads. Errors propagate after the gathered events are returned.
    """
    sock = stream.sock

    def read_line():
        """Pull one CRLF-delimited line off the wire (for chunk-size lines)."""
        while b"\r\n" not in stream.buf:
            more = sock.recv(4096)
            if not more:
                raise EOFError
            stream.buf += more
        line, _, stream.buf = stream.buf.partition(b"\r\n")
        return line

    def read_n(n):
        """Pull exactly ``n`` bytes off the wire (for chunk bodies)."""
        while len(stream.buf) < n:
            more = sock.recv(4096)
            if not more:
                raise EOFError
            stream.buf += more
        out, stream.buf = stream.buf[:n], stream.buf[n:]
        return out

    events = []
    text = ""  # decoded body bytes awaiting SSE framing
    try:
        while len(events) < max_events:
            size_line = read_line().strip()
            if size_line == b"":
                continue  # stray blank line between chunks -- ignore
            size = int(size_line, 16)
            if size == 0:
                continue  # header-flush keep-alive, not end-of-stream
            text += read_n(size).decode("utf-8")
            read_n(2)  # the CRLF that trails every chunk body

            # An SSE event is the run of lines up to the next blank line.
            # Concatenate its ``data:`` payloads (dropping ``:`` comments)
            # and parse the result as one EDN envelope.
            while "\n\n" in text:
                block, _, text = text.partition("\n\n")
                data = [
                    line[len("data:"):].lstrip()
                    for line in block.splitlines()
                    if line.startswith("data:")
                ]
                if data:
                    events.append(loads("\n".join(data)))
                    if len(events) >= max_events:
                        break
    except (EOFError, socket.timeout):
        # End of stream or a quiet period: return what we gathered. The
        # caller treats an empty list as "no event observed in time".
        pass
    return events


# ---------------------------------------------------------------------------
# Runnable recipe:  the full Lemma round-trip
#
# A flat, linear retelling of examples/hello-http.clj: say hello, enter a
# world, propose a fact, assert it, query it back. Each step prints one
# human-readable line so a reader can follow the wire conversation by running
# the file. Everything network-y lives here (or in the __main__ guard) --
# importing the module performs no I/O.
# ---------------------------------------------------------------------------

import os
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

    # 1a. Read the advertised capabilities and limits once, up front, so the
    #     rest of the round-trip can tailor itself to this server (SPEC §10).
    info = read_welcome(body)
    caps = ", ".join(sorted(c.name for c in info.capabilities))
    print(f"server: caps={{{caps}}} max-message-bytes={info.max_message_bytes}")

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

    # 7. The paginated section is gated on the server advertising cursor
    #    pagination -- without it, draining pages via (continue #cursor ...)
    #    is unsupported, so we skip the whole block rather than guess.
    if info.supports(Keyword("lemma/cursor-pagination")):
        # Seed three subset-of facts in one propose, then assert the batch, so
        # the paginated query below has more rows than a single page holds.
        # subset-of is a pure-EDB (stored-fact) predicate, so a query over it
        # has stable (tx-id, ref-id) ordering and can be paginated; a rule-headed
        # predicate like member-of cannot be the sole outer :where pattern (the
        # server rejects it :bad-args :unsupported-rule-call-ordering).
        f1 = fact(Symbol("subset-of"), entity("sub-a"), entity("group"))
        f2 = fact(Symbol("subset-of"), entity("sub-b"), entity("group"))
        f3 = fact(Symbol("subset-of"), entity("sub-c"), entity("group"))
        propose_form = (Symbol("propose"), f1, f2, f3)
        # The batch propose is the largest representative message we send, so
        # it is the one worth checking against :max-message-bytes. A real
        # client checks every outbound message; this demo checks this one.
        if not within_message_limit(info, dumps(propose_form)):
            print("limit-exceeded: message exceeds max-message-bytes; skipping")
            return
        body, _ = named(propose_form)
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
    else:
        print("server does not advertise cursor pagination; skipping paged query")

    # 9. Watch: register a standing pattern and observe a matching change pushed
    #    back on the SSE event stream. Gated on the server advertising
    #    :lemma/watch -- without it the (watch-pattern ...) verb is unsupported.
    if info.supports(Keyword("lemma/watch")):
        # (watch-pattern :pattern [[subset-of ?x #entity "group"]]) -- the args
        # are FLAT keyword args (the :pattern keyword then the where-vector), not
        # a wrapping map. The reply hands back a #watch handle to unwatch with.
        pattern = [[Symbol("subset-of"), Symbol("?x"), entity("group")]]
        body, _ = named((Symbol("watch-pattern"), Keyword("pattern"), pattern))
        if _is_failure(body):
            print(f"watch-pattern refused: {_describe_failure(body)}")
            return
        watch = body.get(Keyword("watch"))
        print(f"watch (subset-of ? group) -> {dumps(body.get(_EVENT))}"
              f"  watch={dumps(watch)}")

        # Ordering is load-bearing: Dianoia registers this session's SSE sink
        # lazily, when the GET /events headers are written, and delivers a
        # :watch-event only to sinks present at emit time (no backlog replay).
        # So OPEN the stream first (registering the sink), THEN trigger the
        # change, THEN drain -- otherwise the push can fire within milliseconds
        # of the assert, before our sink exists, and be silently lost.
        stream = open_sse_stream(base, sid, timeout=10.0)
        try:
            # The server pushes only DELTAS, so the change must be new: a fact
            # re-asserted verbatim is a no-op and fires nothing. We key the probe
            # entity to this process so each run asserts a genuinely fresh fact.
            probe = entity(f"watch-probe-{os.getpid()}")
            body, _ = named((Symbol("propose"),
                             fact(Symbol("subset-of"), probe, entity("group"))))
            if _is_failure(body):
                print(f"watch-probe propose refused: {_describe_failure(body)}")
                return
            body, _ = named((Symbol("assert"), body.get(Keyword("proposal"))))
            if _is_failure(body):
                print(f"watch-probe assert refused: {_describe_failure(body)}")
                return

            events = read_sse_events(stream, max_events=1)
            if events:
                evt = events[0]
                print(f"watch (subset-of ? group) -> {dumps(evt.get(_EVENT))}"
                      f" type={dumps(evt.get(Keyword('type')))}"
                      f" data={dumps(evt.get(Keyword('data')))}")
            else:
                print("watch: no event observed before timeout")
        finally:
            # Release the SSE socket so the server drops the stream, whether or
            # not an event arrived.
            stream.close()

        # Tear the watch down. (unwatch #watch "w-N") -> :ok.
        body, _ = named((Symbol("unwatch"), watch))
        if _is_failure(body):
            print(f"unwatch refused: {_describe_failure(body)}")
            return
        print(f"unwatch {dumps(watch)} -> {dumps(body.get(_EVENT))}")
    else:
        print("server does not advertise watch; skipping watch demo")


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

        # 1a. Read the advertised capabilities and limits once, up front, so the
        #     rest of the round-trip can tailor itself to this server (SPEC §10).
        info = read_welcome(body)
        caps = ", ".join(sorted(c.name for c in info.capabilities))
        print(f"server: caps={{{caps}}} max-message-bytes={info.max_message_bytes}")

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

        # 6. The paginated section is gated on the server advertising cursor
        #    pagination -- without it, draining pages via (continue #cursor ...)
        #    is unsupported, so we skip the whole block rather than guess.
        if info.supports(Keyword("lemma/cursor-pagination")):
            # Seed three subset-of facts in one propose, then assert the batch,
            # so the paginated query below spans more than one page. subset-of is
            # a pure-EDB predicate (stable tx-id/ref-id ordering), so it can be
            # paginated; a rule-headed predicate like member-of cannot be the sole
            # outer :where pattern (server :bad-args :unsupported-rule-call-ordering).
            f1 = fact(Symbol("subset-of"), entity("sub-a"), entity("group"))
            f2 = fact(Symbol("subset-of"), entity("sub-b"), entity("group"))
            f3 = fact(Symbol("subset-of"), entity("sub-c"), entity("group"))
            propose_form = (Symbol("propose"), f1, f2, f3)
            # The batch propose is the largest representative message we send, so
            # it is the one worth checking against :max-message-bytes. A real
            # client checks every outbound message; this demo checks this one.
            if not within_message_limit(info, dumps(propose_form)):
                print("limit-exceeded: message exceeds max-message-bytes; skipping")
                return
            body = call(propose_form)
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
        else:
            print("server does not advertise cursor pagination; skipping paged query")

        # 8. Watch over UDS. Same standing-pattern idea as the HTTP path, but the
        #    push has nowhere separate to go: it interleaves with command replies
        #    on this one socket. Gated on the server advertising :lemma/watch.
        if info.supports(Keyword("lemma/watch")):
            # Bound every subsequent read so a missing push cannot hang the demo.
            sock.settimeout(10.0)
            # (watch-pattern :pattern [[subset-of ?x #entity "group"]]) -- flat
            # keyword args, as on HTTP. The reply carries the #watch handle.
            pattern = [[Symbol("subset-of"), Symbol("?x"), entity("group")]]
            body = call((Symbol("watch-pattern"), Keyword("pattern"), pattern))
            if _is_failure(body):
                print(f"watch-pattern refused: {_describe_failure(body)}")
                return
            watch = body.get(Keyword("watch"))
            print(f"watch (subset-of ? group) -> {dumps(body.get(_EVENT))}"
                  f"  watch={dumps(watch)}")

            # Trigger a fresh delta (a verbatim re-assert is a no-op and fires
            # nothing), keyed to this process so each run is genuinely new. The
            # :asserted reply and the :watch-event push both land on this socket;
            # we read the assert reply here, then demux the push below.
            probe = entity(f"watch-probe-{os.getpid()}")
            body = call((Symbol("propose"),
                         fact(Symbol("subset-of"), probe, entity("group"))))
            if _is_failure(body):
                print(f"watch-probe propose refused: {_describe_failure(body)}")
                return
            body = call((Symbol("assert"), body.get(Keyword("proposal"))))
            if _is_failure(body):
                print(f"watch-probe assert refused: {_describe_failure(body)}")
                return

            evt = _uds_await_watch_event(sock)
            if evt is not None:
                print(f"watch (subset-of ? group) -> {dumps(evt.get(_EVENT))}"
                      f" type={dumps(evt.get(Keyword('type')))}"
                      f" data={dumps(evt.get(Keyword('data')))}")
            else:
                print("watch: no event observed before timeout")

            # Tear the watch down. (unwatch #watch "w-N") -> :ok.
            body = call((Symbol("unwatch"), watch))
            if _is_failure(body):
                print(f"unwatch refused: {_describe_failure(body)}")
                return
            print(f"unwatch {dumps(watch)} -> {dumps(body.get(_EVENT))}")
        else:
            print("server does not advertise watch; skipping watch demo")
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
