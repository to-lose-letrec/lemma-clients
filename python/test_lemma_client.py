#!/usr/bin/env python3
"""Unit tests for the Lemma client's transports, recipe, and tagged literals.

Run from the ``python/`` directory:

    python3 -m unittest test_lemma_client

or from the repo root:

    python3 -m unittest python.test_lemma_client

The client now leans on ``edn_format`` for the codec, so we do NOT re-test the
third-party parser. Instead the suite covers the surface this client *owns*:

  (A) the Lemma tagged literals -- that they serialize to the exact wire text
      and round-trip through ``edn_format`` back into the client's own types.
  (B) the HTTP transport (``post_edn``) -- outbound Request shape, the happy
      2xx path, the HTTPError -> parsed-error-envelope recovery, and the
      URLError -> ConnectionError translation.
  (C) the HTTP handshake (``main``) -- the full propose/assert/query sequence,
      driven with ``post_edn`` monkeypatched so no socket is opened.
  (D) the UDS framing primitives (``uds_send_frame`` / ``uds_recv_frame`` /
      ``_recv_exactly``) -- exact 4-byte big-endian framing, multi-chunk
      reassembly, premature-EOF handling.
  (E) the UDS handshake (``main_uds``) -- driven against a scripted in-memory
      socket, including the connection-bound NO-session-echo invariant and the
      connect-failure -> ConnectionError translation.
  (F) the CLI dispatcher (``_dispatch``) -- argv -> transport selection.

Everything is deterministic: no network, no sleeps, no shared mutable state
that leaks between tests. The HTTP seam (``urllib.request.urlopen``) and the
UDS seam (``socket.socket``) are monkeypatched and restored via ``addCleanup``.
"""

import io
import socket
import struct
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout

import edn_format
from edn_format import Keyword, Symbol, dumps, loads

import lemma_client as lc
from lemma_client import (
    DEFAULT_BASE,
    DEFAULT_SOCKET,
    ServerInfo,
    entity,
    fact,
    main,
    main_uds,
    post_edn,
    read_welcome,
    uds_recv_frame,
    uds_send_frame,
    within_message_limit,
    world,
)


# ===========================================================================
# (A) Tagged literals:  Lemma value  <->  exact EDN wire text (via edn_format)
#
# We do not test edn_format's parser; we test that the client's tag classes and
# constructor helpers serialize to the wire text Lemma expects and reconstruct
# the same objects on the way back in.
# ===========================================================================


class TagRoundTripTests(unittest.TestCase):
    def test_use_world_verb_form_serializes_to_exact_wire_text(self):
        self.assertEqual(
            dumps((Symbol("use-world"), world("default"))),
            '(use-world #world "default")',
        )

    def test_world_handle_round_trips_through_edn_format(self):
        self.assertEqual(loads(dumps(world("default"))), world("default"))

    def test_entity_handle_round_trips_through_edn_format(self):
        self.assertEqual(loads(dumps(entity("alice"))), entity("alice"))

    def test_fact_round_trips_through_edn_format(self):
        f = fact(Symbol("member-of"), entity("alice"), entity("managers"))
        self.assertEqual(loads(dumps(f)), f)

    def test_result_envelope_parses_to_expected_mapping(self):
        body = loads('{:event :result :rows [["venus"]] :done? true}')
        self.assertIsInstance(body, edn_format.ImmutableDict)
        self.assertEqual(body[Keyword("event")], Keyword("result"))
        self.assertEqual(body[Keyword("rows")], [["venus"]])
        self.assertIs(body[Keyword("done?")], True)


# ===========================================================================
# (B) HTTP transport: exercise post_edn's OWN urlopen call, header
#     construction, and its two except branches by patching the urlopen seam.
#
# The handshake tests further down monkeypatch post_edn itself, so post_edn's
# transport body would otherwise be untested. Here we replace the lower seam --
# urllib.request.urlopen (the code calls it fully-qualified) -- so the real
# Request construction and except branches run.
#
#   (a) happy path           -> the success branch
#   (b) recovered HTTPError   -> parsed back into an error envelope
#   (c) URLError              -> re-raised as a ConnectionError naming base
# ===========================================================================


class _FakeResponse:
    """Context-manager stand-in for a urlopen success result.

    Supports ``with urllib.request.urlopen(req) as response:`` and exposes
    ``.read()`` (EDN bytes) and ``.headers.get("X-Lemma-Session")``.
    """

    class _Headers:
        def __init__(self, session_id):
            self._session_id = session_id

        def get(self, name):
            if name == "X-Lemma-Session":
                return self._session_id
            return None

    def __init__(self, raw_bytes, session_id):
        self._raw = raw_bytes
        self.headers = self._Headers(session_id)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._raw


class PostEdnTransportTests(unittest.TestCase):
    """Drive post_edn against a patched ``urllib.request.urlopen`` seam."""

    def setUp(self):
        self._orig_urlopen = urllib.request.urlopen
        self.addCleanup(self._restore)

    def _restore(self):
        urllib.request.urlopen = self._orig_urlopen

    def _patch_urlopen(self, fake):
        urllib.request.urlopen = fake

    # --- (a) Happy 2xx -----------------------------------------------------

    def test_happy_path_returns_parsed_body_and_session_id(self):
        canned = (
            '{:event :welcome :version 1 '
            ':session #session "s-77" :world #world "default"}'
        )

        def fake_urlopen(request):
            return _FakeResponse(canned.encode("utf-8"), "s-77")

        self._patch_urlopen(fake_urlopen)

        body, session_id = post_edn("/v1/messages", (Symbol("hello"),))

        self.assertEqual(body, loads(canned))
        self.assertEqual(session_id, "s-77")

    def test_happy_path_builds_request_full_url_from_base_plus_path(self):
        captured = {}

        def fake_urlopen(request):
            captured["request"] = request
            return _FakeResponse(b"{:event :result}", None)

        self._patch_urlopen(fake_urlopen)

        post_edn("/v1/messages", (Symbol("hello"),), base="http://example.test:9999")

        self.assertEqual(
            captured["request"].full_url, "http://example.test:9999/v1/messages"
        )

    def test_happy_path_sends_application_edn_content_type(self):
        captured = {}

        def fake_urlopen(request):
            captured["request"] = request
            return _FakeResponse(b"{:event :result}", None)

        self._patch_urlopen(fake_urlopen)

        post_edn("/v1/messages", (Symbol("hello"),))

        # urllib title-cases header keys; check case-insensitively.
        self.assertEqual(
            captured["request"].get_header("Content-type"), "application/edn"
        )

    def test_happy_path_encodes_form_as_utf8_edn_body(self):
        captured = {}
        form = (Symbol("hello"),)

        def fake_urlopen(request):
            captured["request"] = request
            return _FakeResponse(b"{:event :result}", None)

        self._patch_urlopen(fake_urlopen)

        post_edn("/v1/messages", form)

        self.assertEqual(captured["request"].data, dumps(form).encode("utf-8"))

    def test_happy_path_without_session_omits_session_header(self):
        captured = {}

        def fake_urlopen(request):
            captured["request"] = request
            return _FakeResponse(b"{:event :result}", None)

        self._patch_urlopen(fake_urlopen)

        post_edn("/v1/messages", (Symbol("hello"),))

        self.assertIsNone(captured["request"].get_header("X-lemma-session"))

    def test_happy_path_with_session_sends_session_header(self):
        captured = {}

        def fake_urlopen(request):
            captured["request"] = request
            return _FakeResponse(b"{:event :result}", "s-77")

        self._patch_urlopen(fake_urlopen)

        post_edn("/v1/sessions/s-77/messages", (Symbol("query"),), session="s-77")

        self.assertEqual(captured["request"].get_header("X-lemma-session"), "s-77")

    # --- (b) HTTPError: recovered into an error envelope -------------------

    def test_http_error_is_parsed_into_error_envelope_without_raising(self):
        error_body = b'{:event :error :reason :malformed :message "bad verb form"}'

        def fake_urlopen(request):
            raise urllib.error.HTTPError(
                "http://127.0.0.1:8080/v1/messages",
                400,
                "Bad Request",
                {},
                io.BytesIO(error_body),
            )

        self._patch_urlopen(fake_urlopen)

        body, _session_id = post_edn("/v1/messages", (Symbol("hello"),))

        self.assertEqual(body[Keyword("event")], Keyword("error"))

    def test_http_error_surfaces_session_header_from_error_response(self):
        error_body = b'{:event :error :reason :malformed :message "bad verb form"}'

        # A real HTTPError exposes .read() and .headers; the header dict passed
        # to its constructor becomes the .headers the except branch reads.
        def fake_urlopen(request):
            raise urllib.error.HTTPError(
                "http://127.0.0.1:8080/v1/messages",
                400,
                "Bad Request",
                {"X-Lemma-Session": "s-99"},
                io.BytesIO(error_body),
            )

        self._patch_urlopen(fake_urlopen)

        _body, session_id = post_edn("/v1/messages", (Symbol("hello"),))

        self.assertEqual(session_id, "s-99")

    # --- (c) URLError: re-raised as a ConnectionError naming base ----------

    def test_url_error_is_translated_to_actionable_connection_error(self):
        original = urllib.error.URLError("Connection refused")

        def fake_urlopen(request):
            raise original

        self._patch_urlopen(fake_urlopen)

        with self.assertRaises(ConnectionError) as ctx:
            post_edn("/v1/messages", (Symbol("hello"),), base="http://down.test:1234")

        exc = ctx.exception
        # Chained from the original URLError (raised `from err`), so the
        # underlying cause is preserved for debugging.
        self.assertIs(exc.__cause__, original)
        # The message names the unreachable base, surfaces the underlying
        # reason, and stays actionable -- independent of how base is formatted.
        message = str(exc)
        self.assertIn("http://down.test:1234", message)
        self.assertIn("Connection refused", message)
        self.assertIn("is the server running?", message)


# ===========================================================================
# (C) HTTP handshake: drive main() with the post_edn seam monkeypatched.
# ===========================================================================


# Canned EDN response bodies for a full successful sequence.
_WELCOME = (
    '{:event :welcome :version 1 '
    ':session #session "s-77" :world #world "default" '
    ':capabilities #{:lemma/v1 :lemma/cursor-pagination :lemma/watch '
    ':lemma/import :lemma/export} '
    ':limits {:max-message-bytes 1048576} '
    ':verbs {:core #{hello use-world propose assert query} :extensions {}}}'
)
_WORLD_SELECTED = '{:event :world-selected :world #world "default"}'
_PROPOSED = '{:event :proposed :proposal #proposal "p-1"}'
_ASSERTED = '{:event :asserted}'
# Query rows bind ?o to the entity name as a plain string (see live Dianoia).
_RESULT = '{:event :result :rows [["venus"]] :done? true}'

# The seed-and-paginate tail main() now runs after the first query: a second
# propose (three member-of facts), its assert, then a :limit 2 query that comes
# back as a full first page (:done? false + #cursor) followed by a continue
# that drains the rest (:done? true). query_all is what stitches those two
# pages together inside main().
_PROPOSED_3X = '{:event :proposed :proposal #proposal "p-2"}'
_PAGE1 = (
    '{:event :result :rows [[#entity "alice"] [#entity "bob"]] '
    ':done? false :cursor #cursor "c-1"}'
)
_PAGE2 = '{:event :result :rows [[#entity "carol"]] :done? true :cursor #cursor "c-1"}'

# The watch tail main() now runs after the paged query when the server
# advertises :lemma/watch: register the standing pattern (the reply carries a
# #watch handle), propose+assert a fresh delta to trigger a push, then unwatch.
# The :watch-event itself does NOT come back through post_edn -- it arrives on
# the separate SSE event stream (read_sse_events), stubbed by _FakeSseTransport.
_WATCH_ESTABLISHED = '{:event :watch-established :watch #watch "w-1"}'
_WATCH_PROBE_PROPOSED = '{:event :proposed :proposal #proposal "p-3"}'
_UNWATCHED = '{:event :ok}'

# The full main() sequence over HTTP: hello, use-world, propose, assert, query,
# propose(3x), assert, paged-query(page1), continue(page2), watch-pattern,
# watch-probe propose, watch-probe assert, unwatch -- thirteen post_edn calls.
# (The :watch-event push rides the SSE seam, not post_edn.)
_FULL_SEQUENCE = [
    _WELCOME,
    _WORLD_SELECTED,
    _PROPOSED,
    _ASSERTED,
    _RESULT,
    _PROPOSED_3X,
    _ASSERTED,
    _PAGE1,
    _PAGE2,
    _WATCH_ESTABLISHED,
    _WATCH_PROBE_PROPOSED,
    _ASSERTED,
    _UNWATCHED,
]

_ERROR = '{:event :error :reason :malformed :message "bad verb form"}'
_REJECTED = (
    '{:event :rejected :reason :inconsistent '
    ':violations [#violation {:kind :cycle}]}'
)


class FakePostEdn:
    """A scripted stand-in for ``lemma_client.post_edn``.

    Each call pops the next canned EDN body off ``responses``, parses it with
    ``edn_format.loads``, and returns ``(body, session_id)``. The session id
    surfaced for the *first* (welcome) reply comes from ``welcome_session``,
    mimicking the server setting the ``X-Lemma-Session`` response header; later
    replies echo back whatever session the caller threaded in.

    It records every call's ``(path, form, session, base)`` for assertions.
    """

    def __init__(self, responses, welcome_session="s-77"):
        self._responses = list(responses)
        self._welcome_session = welcome_session
        self.calls = []

    def __call__(self, path, form, session=None, base=lc.DEFAULT_BASE):
        self.calls.append((path, form, session, base))
        raw = self._responses.pop(0)
        body = loads(raw)
        # The welcome handshake is the only call that mints a session id; it is
        # the first call and arrives with session=None.
        session_id = self._welcome_session if session is None else session
        return body, session_id


# --- SSE seam: a canned chunked HTTP response for read_sse_events ----------
#
# read_sse_events opens a RAW socket via socket.create_connection, writes a
# GET, then transfer-decodes a chunked body and parses SSE out of it. We feed a
# byte-accurate chunked response so the decoder's real logic runs:
#   * status line + headers + blank line, then
#   * a size-0 chunk (http-kit's header-flush keep-alive -- must be SKIPPED,
#     not treated as EOF), then
#   * a data chunk carrying a ``:``-comment keep-alive line (must be SKIPPED)
#     plus the ``data:`` lines of one :watch-event envelope, terminated by the
#     SSE blank line.
# The fake socket replays those bytes through recv(); once exhausted recv
# returns b"" (EOF), which ends the read without hanging.


def _chunk(payload_bytes):
    """Wrap ``payload_bytes`` as one HTTP chunked-transfer frame.

    ``hexlen\\r\\n<bytes>\\r\\n`` -- the exact framing http-kit emits and the
    framing read_sse_events transfer-decodes.
    """
    return f"{len(payload_bytes):x}\r\n".encode("ascii") + payload_bytes + b"\r\n"


# One :watch-event envelope's SSE block: a ``:``-comment keep-alive line (to be
# skipped) followed by the ``data:`` payload, ending in the SSE blank line.
_WATCH_EVENT_EDN = (
    '{:event :watch-event :type :assert '
    ':data #fact {:predicate subset-of :subject #entity "wp" :object #entity "group"}}'
)
_SSE_BLOCK = (
    ": keep-alive comment -- must be ignored\n"
    f"data: {_WATCH_EVENT_EDN}\n"
    "\n"
).encode("utf-8")


def _canned_sse_response(blocks=(_SSE_BLOCK,)):
    """Assemble a full chunked SSE HTTP/1.1 response as on-wire bytes.

    Headers, then a size-0 keep-alive flush chunk (proving it is NOT treated as
    EOF), then one transfer-chunk per SSE ``block``. No terminating size-0
    chunk: the stream simply goes quiet, and the fake socket signals EOF by
    returning b"" once these bytes are drained.
    """
    head = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
    )
    body = _chunk(b"")  # size-0 header-flush keep-alive
    for block in blocks:
        body += _chunk(block)
    return head + body


class _FakeSseSocket:
    """A raw-socket stand-in for read_sse_events: replays a byte script.

    Records the GET request bytes sent, replays the canned response through
    recv() in <=n-byte slices (so the header/chunk loops run for real), returns
    b"" once drained (EOF), and records close(). settimeout is a no-op.
    """

    def __init__(self, response_bytes):
        self._buf = response_bytes
        self.sent = b""
        self.timeout = None
        self.closed = False

    def settimeout(self, t):
        self.timeout = t

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk  # b"" once exhausted -> EOF, no hang

    def close(self):
        self.closed = True


class HandshakeBase(unittest.TestCase):
    def setUp(self):
        self._orig_post_edn = lc.post_edn
        self._orig_create_connection = socket.create_connection
        self.sse_sockets = []
        self.create_connection_calls = []
        self.addCleanup(self._restore)
        # By default, stub the SSE seam with one canned :watch-event so the
        # watch tail of the default (watch-capable) fixtures completes. Tests
        # that must prove the seam is NOT opened call disable_sse().
        self._install_sse(_canned_sse_response())

    def _restore(self):
        lc.post_edn = self._orig_post_edn
        socket.create_connection = self._orig_create_connection

    def _install_sse(self, response_bytes):
        sockets = self.sse_sockets
        calls = self.create_connection_calls

        def fake_create_connection(address, timeout=None):
            calls.append((address, timeout))
            fake = _FakeSseSocket(response_bytes)
            sockets.append(fake)
            return fake

        socket.create_connection = fake_create_connection

    def disable_sse(self):
        """Make any SSE-seam open fail the test loudly (it must not be reached)."""
        calls = self.create_connection_calls

        def forbidden(address, timeout=None):
            calls.append((address, timeout))
            raise AssertionError(
                "read_sse_events opened the SSE seam when it should have been gated out"
            )

        socket.create_connection = forbidden

    def install(self, fake):
        lc.post_edn = fake
        return fake

    def run_main_capturing(self, base=DEFAULT_BASE):
        out = io.StringIO()
        with redirect_stdout(out):
            main(base=base)
        return out.getvalue()


class HandshakeSuccessTests(HandshakeBase):
    def test_full_sequence_reaches_result_without_raising(self):
        fake = self.install(FakePostEdn(_FULL_SEQUENCE))
        output = self.run_main_capturing()
        # All thirteen protocol steps were issued: the five base steps, the
        # paginated query + continue, then the watch tail (watch-pattern,
        # probe propose+assert, unwatch).
        self.assertEqual(len(fake.calls), 13)
        # The conversation reached the query/result line.
        self.assertIn("rows=", output)
        self.assertIn('"venus"', output)

    def test_first_call_is_anonymous_hello_on_messages_endpoint(self):
        fake = self.install(FakePostEdn(_FULL_SEQUENCE))
        self.run_main_capturing()
        first_path, first_form, first_session, _ = fake.calls[0]
        self.assertEqual(first_path, "/v1/messages")
        self.assertIsNone(first_session)
        self.assertEqual(dumps(first_form), "(hello)")

    def test_session_id_from_header_is_used_in_named_endpoint_path(self):
        fake = self.install(FakePostEdn(_FULL_SEQUENCE, welcome_session="s-77"))
        self.run_main_capturing()
        # Every call after the hello must target the named-session endpoint
        # built from the X-Lemma-Session header value, and echo it back.
        for path, _form, session, _base in fake.calls[1:]:
            self.assertEqual(path, "/v1/sessions/s-77/messages")
            self.assertEqual(session, "s-77")

    def test_base_url_is_threaded_through(self):
        fake = self.install(FakePostEdn(_FULL_SEQUENCE))
        self.run_main_capturing(base="http://example.test:9999")
        for _path, _form, _session, base in fake.calls:
            self.assertEqual(base, "http://example.test:9999")

    def test_proposal_handle_is_threaded_into_assert(self):
        fake = self.install(FakePostEdn(_FULL_SEQUENCE))
        self.run_main_capturing()
        # Call index 3 is the assert; its form is (assert <proposal>) and the
        # proposal must be the #proposal handle returned by the propose reply.
        assert_form = fake.calls[3][1]
        self.assertIsInstance(assert_form, tuple)
        self.assertEqual(assert_form[0], Symbol("assert"))
        self.assertEqual(assert_form[1], loads('#proposal "p-1"'))


class HandshakeFailurePathTests(HandshakeBase):
    def test_non_welcome_first_reply_stops_cleanly(self):
        fake = self.install(FakePostEdn([_ERROR]))
        # Must not raise.
        output = self.run_main_capturing()
        self.assertEqual(len(fake.calls), 1)
        self.assertIn(":welcome", output)  # the "expected :welcome" message

    def test_rejected_after_welcome_stops_cleanly(self):
        # Welcome succeeds, then use-world is rejected: main stops, no later
        # calls, no exception.
        fake = self.install(FakePostEdn([_WELCOME, _REJECTED]))
        output = self.run_main_capturing()
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("use-world refused", output)

    def test_error_during_propose_stops_cleanly(self):
        fake = self.install(FakePostEdn([_WELCOME, _WORLD_SELECTED, _ERROR]))
        output = self.run_main_capturing()
        self.assertEqual(len(fake.calls), 3)
        self.assertIn("propose refused", output)


# ===========================================================================
# (D) UDS framing:  uds_send_frame / uds_recv_frame / _recv_exactly
#
# The UDS transport delimits each EDN message with a 4-byte big-endian
# UNSIGNED length prefix followed by that many UTF-8 body bytes (mirrors
# Dianoia's transport/uds.clj write-frame / read-frame). These tests exercise
# the framing in isolation against in-memory fake sockets -- no real socket,
# no blocking, no sleeps.
# ===========================================================================


def _frame(edn_str):
    """Build the on-wire bytes for ``edn_str``: >I length prefix + UTF-8 body.

    Uses the same primitives the implementation does so the expectation is a
    spec, not a re-derivation of the implementation.
    """
    body = edn_str.encode("utf-8")
    return struct.pack(">I", len(body)) + body


class _SendRecorderSocket:
    """A fake socket that records every ``sendall`` payload."""

    def __init__(self):
        self.sends = []

    def sendall(self, data):
        self.sends.append(data)


class _ScriptedRecvSocket:
    """A fake socket whose ``recv`` replays a scripted sequence of byte chunks.

    Each ``recv(n)`` pops the next pre-loaded chunk and returns at most ``n``
    bytes of it, like a real socket (a chunk longer than ``n`` has its tail
    pushed back for the next read). This lets a single logical frame be
    delivered across several small pieces, proving the reassembly loop. When
    the script is exhausted it returns ``b""`` to model the peer having closed
    the connection.
    """

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.recv_calls = []

    def recv(self, n):
        self.recv_calls.append(n)
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if len(chunk) > n:
            self._chunks.insert(0, chunk[n:])
            chunk = chunk[:n]
        return chunk


class UdsSendFrameTests(unittest.TestCase):
    def test_send_frame_emits_exact_length_prefixed_bytes(self):
        sock = _SendRecorderSocket()
        edn = dumps((Symbol("hello"),))

        uds_send_frame(sock, edn)

        body = edn.encode("utf-8")
        expected = struct.pack(">I", len(body)) + body
        # sendall is invoked exactly once with the whole frame.
        self.assertEqual(sock.sends, [expected])

    def test_send_frame_prefix_is_four_byte_big_endian_length(self):
        sock = _SendRecorderSocket()
        edn = '(use-world #world "default")'

        uds_send_frame(sock, edn)

        sent = sock.sends[0]
        self.assertEqual(sent[:4], struct.pack(">I", len(edn.encode("utf-8"))))

    def test_send_frame_body_is_utf8_encoding_of_edn(self):
        sock = _SendRecorderSocket()
        # A non-ASCII payload proves the byte length, not the char length, is
        # what gets framed.
        edn = dumps(entity("vénus"))

        uds_send_frame(sock, edn)

        sent = sock.sends[0]
        body = edn.encode("utf-8")
        (length,) = struct.unpack(">I", sent[:4])
        self.assertEqual(length, len(body))
        self.assertEqual(sent[4:], body)


class UdsRecvFrameTests(unittest.TestCase):
    def test_recv_frame_reconstructs_edn_string_from_one_chunk(self):
        edn = '{:event :welcome :version 1}'
        sock = _ScriptedRecvSocket([_frame(edn)])

        self.assertEqual(uds_recv_frame(sock), edn)

    def test_recv_frame_round_trips_a_sent_frame(self):
        # Send into a recorder, feed the recorded bytes back through recv.
        form = (Symbol("query"),)
        sender = _SendRecorderSocket()
        uds_send_frame(sender, dumps(form))
        receiver = _ScriptedRecvSocket([sender.sends[0]])

        got = uds_recv_frame(receiver)

        self.assertEqual(loads(got), form)

    def test_recv_frame_reassembles_a_frame_split_across_many_chunks(self):
        # Deliver the prefix one byte at a time, then the body in two pieces.
        edn = '{:event :result :rows [["venus"]] :done? true}'
        frame = _frame(edn)
        prefix = frame[:4]
        body = frame[4:]
        chunks = [prefix[i : i + 1] for i in range(4)]  # 4 single-byte prefix reads
        chunks.append(body[: len(body) // 2])
        chunks.append(body[len(body) // 2 :])
        sock = _ScriptedRecvSocket(chunks)

        self.assertEqual(uds_recv_frame(sock), edn)
        # The body arrived in multiple pieces, so _recv_exactly must have
        # looped: more recv calls than the two logical frame reads (prefix,
        # body) it would take if each returned everything at once.
        self.assertGreater(len(sock.recv_calls), 2)


class RecvExactlyTests(unittest.TestCase):
    def test_recv_exactly_returns_requested_bytes_across_chunks(self):
        sock = _ScriptedRecvSocket([b"ab", b"cd", b"ef"])

        self.assertEqual(lc._recv_exactly(sock, 6), b"abcdef")

    def test_recv_exactly_premature_eof_raises_connection_error(self):
        # Two bytes available, then the peer closes (b"") -- asking for four
        # must raise rather than hang or return a short read.
        sock = _ScriptedRecvSocket([b"ab"])

        with self.assertRaises(ConnectionError) as ctx:
            lc._recv_exactly(sock, 4)

        self.assertIn("connection closed", str(ctx.exception))

    def test_recv_frame_premature_eof_in_body_raises_connection_error(self):
        # A valid 10-byte length prefix, but the body never fully arrives.
        sock = _ScriptedRecvSocket([struct.pack(">I", 10), b"short"])

        with self.assertRaises(ConnectionError):
            uds_recv_frame(sock)


# ===========================================================================
# (E) UDS handshake: drive main_uds() with no real socket.
#
# socket.socket is monkeypatched to return a scripted fake that supports
# connect()/sendall()/recv()/close(). The canned reply frames are built with
# the real framing primitives so they are byte-correct. Determinism: no real
# sockets, no sleeps; socket.socket is restored via addCleanup.
# ===========================================================================


class _FakeUdsSocket:
    """A scripted Unix-domain-socket stand-in for main_uds().

    Construction records the (family, type) the client requested. ``connect``
    records the path. ``sendall`` records each frame's raw bytes. ``recv``
    replays the welcome/world-selected/proposed/asserted/result reply frames,
    delivered as a single in-memory byte stream so multi-byte reads work
    naturally. Supports close().
    """

    def __init__(self, family, type):
        self.family = family
        self.type = type
        self.connected_path = None
        self.connect_calls = 0
        self.sends = []
        self.closed = False
        self.timeout = None
        self._recv_buffer = b""

    # -- script the replies the server would send back -----------------------

    def load_replies(self, edn_replies):
        self._recv_buffer = b"".join(_frame(r) for r in edn_replies)

    # -- socket surface used by main_uds -------------------------------------

    def settimeout(self, t):
        # The UDS watch path bounds its reads with sock.settimeout so a missing
        # push cannot hang; record it, no real timer needed for the in-memory
        # byte stream.
        self.timeout = t

    def connect(self, path):
        self.connect_calls += 1
        self.connected_path = path

    def sendall(self, data):
        self.sends.append(data)

    def recv(self, n):
        chunk = self._recv_buffer[:n]
        self._recv_buffer = self._recv_buffer[n:]
        return chunk  # b"" once exhausted, modelling EOF

    def close(self):
        self.closed = True


# Canned UDS reply bodies. The welcome carries the connection-bound session as
# a #session handle; the rest mirror the HTTP fixtures above.
_UDS_WELCOME = (
    '{:event :welcome :version 1 '
    ':session #session "s-uds-1" :world #world "default" '
    ':capabilities #{:lemma/v1 :lemma/cursor-pagination :lemma/watch '
    ':lemma/import :lemma/export} '
    ':limits {:max-message-bytes 1048576} '
    ':verbs {:core #{hello use-world propose assert query} :extensions {}}}'
)
_UDS_WORLD_SELECTED = '{:event :world-selected :world #world "default"}'
_UDS_PROPOSED = '{:event :proposed :proposal #proposal "p-1"}'
_UDS_ASSERTED = '{:event :asserted}'
_UDS_RESULT = '{:event :result :rows [["venus"]] :done? true}'

# The seed-and-paginate tail (mirrors the HTTP fixtures): a second propose,
# its assert, then the two-page paginated query that query_all drains.
_UDS_PROPOSED_3X = '{:event :proposed :proposal #proposal "p-2"}'
_UDS_PAGE1 = (
    '{:event :result :rows [[#entity "alice"] [#entity "bob"]] '
    ':done? false :cursor #cursor "c-1"}'
)
_UDS_PAGE2 = (
    '{:event :result :rows [[#entity "carol"]] :done? true :cursor #cursor "c-1"}'
)

# The UDS watch tail. Over UDS the :watch-event has no separate channel: it
# interleaves with command replies on the one socket, so _uds_await_watch_event
# must skip command echoes until it finds the event. The recv script below puts
# the watch-established/proposed/asserted command replies, then a STRAY command
# echo (proving the demux skips non-events), then the :watch-event frame, then
# the unwatch :ok -- so the loop has to discard at least one frame before it
# lands on the event.
_UDS_WATCH_ESTABLISHED = '{:event :watch-established :watch #watch "w-1"}'
_UDS_WATCH_PROBE_PROPOSED = '{:event :proposed :proposal #proposal "p-3"}'
_UDS_WATCH_EVENT = (
    '{:event :watch-event :type :assert '
    ':data #fact {:predicate subset-of :subject #entity "wp" :object #entity "group"}}'
)
# A command-reply-shaped frame that is NOT a :watch-event; the demux must skip
# it before reaching the event frame.
_UDS_STRAY_ECHO = '{:event :asserted}'
_UDS_UNWATCHED = '{:event :ok}'

_UDS_FULL_SEQUENCE = [
    _UDS_WELCOME,
    _UDS_WORLD_SELECTED,
    _UDS_PROPOSED,
    _UDS_ASSERTED,
    _UDS_RESULT,
    _UDS_PROPOSED_3X,
    _UDS_ASSERTED,
    _UDS_PAGE1,
    _UDS_PAGE2,
    # watch tail recv order: command replies, then an interleaved stray echo and
    # the :watch-event push, then the unwatch reply.
    _UDS_WATCH_ESTABLISHED,
    _UDS_WATCH_PROBE_PROPOSED,
    _UDS_ASSERTED,
    _UDS_STRAY_ECHO,
    _UDS_WATCH_EVENT,
    _UDS_UNWATCHED,
]


class UdsHandshakeBase(unittest.TestCase):
    def setUp(self):
        self._orig_socket = socket.socket
        self.addCleanup(self._restore)
        self.created = []

    def _restore(self):
        socket.socket = self._orig_socket

    def install_socket(self, replies):
        """Patch socket.socket to mint a scripted fake pre-loaded with replies.

        Returns the factory; each created fake is appended to ``self.created``
        so a test can assert exactly one socket was opened.
        """
        created = self.created

        def factory(family, type):
            fake = _FakeUdsSocket(family, type)
            fake.load_replies(replies)
            created.append(fake)
            return fake

        socket.socket = factory
        return factory

    def run_main_uds_capturing(self, socket_path=DEFAULT_SOCKET):
        out = io.StringIO()
        with redirect_stdout(out):
            main_uds(socket_path)
        return out.getvalue()

    def sent_frames_as_edn(self, fake):
        """Decode every frame the client sent back into EDN strings."""
        buf = b"".join(fake.sends)
        forms = []
        pos = 0
        while pos < len(buf):
            (length,) = struct.unpack(">I", buf[pos : pos + 4])
            pos += 4
            forms.append(buf[pos : pos + length].decode("utf-8"))
            pos += length
        return forms


class UdsHandshakeSuccessTests(UdsHandshakeBase):
    def test_full_sequence_reaches_result_line(self):
        self.install_socket(_UDS_FULL_SEQUENCE)

        output = self.run_main_uds_capturing()

        self.assertIn("rows=", output)
        self.assertIn('"venus"', output)

    def test_opens_exactly_one_unix_stream_socket(self):
        self.install_socket(_UDS_FULL_SEQUENCE)

        self.run_main_uds_capturing()

        self.assertEqual(len(self.created), 1)
        fake = self.created[0]
        self.assertEqual(fake.family, socket.AF_UNIX)
        self.assertEqual(fake.type, socket.SOCK_STREAM)

    def test_connects_once_to_the_given_socket_path(self):
        self.install_socket(_UDS_FULL_SEQUENCE)

        self.run_main_uds_capturing(socket_path="/run/lemma/custom.sock")

        fake = self.created[0]
        self.assertEqual(fake.connect_calls, 1)
        self.assertEqual(fake.connected_path, "/run/lemma/custom.sock")

    def test_default_socket_path_is_used_when_unspecified(self):
        self.install_socket(_UDS_FULL_SEQUENCE)

        out = io.StringIO()
        with redirect_stdout(out):
            main_uds()

        self.assertEqual(self.created[0].connected_path, DEFAULT_SOCKET)

    def test_first_sent_frame_is_anonymous_hello(self):
        self.install_socket(_UDS_FULL_SEQUENCE)

        self.run_main_uds_capturing()

        sent = self.sent_frames_as_edn(self.created[0])
        self.assertEqual(sent[0], "(hello)")

    def test_sends_thirteen_frames_one_per_protocol_step(self):
        self.install_socket(_UDS_FULL_SEQUENCE)

        self.run_main_uds_capturing()

        # Five base + paged query/continue + watch tail (watch-pattern, probe
        # propose+assert, unwatch) = thirteen sent frames.
        self.assertEqual(len(self.sent_frames_as_edn(self.created[0])), 13)

    def test_no_frame_after_hello_echoes_the_session_handle(self):
        # CRITICAL: over UDS the session is bound to the connection by the
        # server, so the client must NOT re-send the session id in any later
        # frame. Decode every sent frame and assert none after the hello
        # carries a #session handle or the session value.
        self.install_socket(_UDS_FULL_SEQUENCE)

        self.run_main_uds_capturing()

        sent = self.sent_frames_as_edn(self.created[0])
        for frame in sent[1:]:
            self.assertNotIn("#session", frame)
            self.assertNotIn("s-uds-1", frame)
            self.assertNotIn(":session", frame)

    def test_proposal_handle_is_threaded_into_assert_frame(self):
        self.install_socket(_UDS_FULL_SEQUENCE)

        self.run_main_uds_capturing()

        sent = self.sent_frames_as_edn(self.created[0])
        # Frame index 3 is the assert: (assert #proposal "p-1").
        assert_form = loads(sent[3])
        self.assertIsInstance(assert_form, tuple)
        self.assertEqual(assert_form[0], Symbol("assert"))
        self.assertEqual(assert_form[1], loads('#proposal "p-1"'))

    def test_socket_is_closed_after_a_successful_run(self):
        self.install_socket(_UDS_FULL_SEQUENCE)

        self.run_main_uds_capturing()

        self.assertTrue(self.created[0].closed)


class UdsHandshakeFailurePathTests(UdsHandshakeBase):
    def test_non_welcome_first_reply_stops_cleanly(self):
        self.install_socket(['{:event :error :reason :malformed :message "bad"}'])

        output = self.run_main_uds_capturing()

        self.assertIn(":welcome", output)  # the "expected :welcome" message
        # Only the hello was sent; no later frames.
        self.assertEqual(len(self.sent_frames_as_edn(self.created[0])), 1)

    def test_rejected_after_welcome_stops_cleanly(self):
        self.install_socket(
            [
                _UDS_WELCOME,
                '{:event :rejected :reason :inconsistent '
                ':violations [#violation {:kind :cycle}]}',
            ]
        )

        output = self.run_main_uds_capturing()

        self.assertIn("use-world refused", output)
        self.assertEqual(len(self.sent_frames_as_edn(self.created[0])), 2)

    def test_socket_is_closed_even_when_a_step_is_refused(self):
        self.install_socket(
            [_UDS_WELCOME, '{:event :error :reason :malformed :message "bad"}']
        )

        self.run_main_uds_capturing()

        self.assertTrue(self.created[0].closed)


class UdsConnectFailureTests(UdsHandshakeBase):
    def test_connect_filenotfound_raises_connection_error_naming_path(self):
        path = "/no/such.sock"

        def factory(family, type):
            fake = _FakeUdsSocket(family, type)

            def boom(_p):
                raise FileNotFoundError(2, "No such file or directory")

            fake.connect = boom
            self.created.append(fake)
            return fake

        socket.socket = factory

        with self.assertRaises(ConnectionError) as ctx:
            main_uds(path)

        message = str(ctx.exception)
        self.assertIn(path, message)
        self.assertIn("is the server running?", message)

    def test_connect_failure_still_closes_the_socket(self):
        def factory(family, type):
            fake = _FakeUdsSocket(family, type)

            def boom(_p):
                raise FileNotFoundError(2, "No such file or directory")

            fake.connect = boom
            self.created.append(fake)
            return fake

        socket.socket = factory

        with self.assertRaises(ConnectionError):
            main_uds("/no/such.sock")

        self.assertTrue(self.created[0].closed)


# ===========================================================================
# (F) CLI dispatch: argv -> transport selection, with no network.
# ===========================================================================


class CliDispatchTests(unittest.TestCase):
    """_dispatch(argv) routes to main()/main_uds() without performing I/O."""

    def setUp(self):
        self._orig_main = lc.main
        self._orig_main_uds = lc.main_uds
        self.calls = []
        lc.main = lambda *a, **k: self.calls.append(("main", a, k))
        lc.main_uds = lambda *a, **k: self.calls.append(("main_uds", a, k))
        self.addCleanup(self._restore)

    def _restore(self):
        lc.main = self._orig_main
        lc.main_uds = self._orig_main_uds

    def test_no_args_runs_http_main_with_default_base(self):
        lc._dispatch([])
        self.assertEqual(self.calls, [("main", (DEFAULT_BASE,), {})])

    def test_url_arg_runs_http_main_with_that_base(self):
        lc._dispatch(["http://host:9999"])
        self.assertEqual(self.calls, [("main", ("http://host:9999",), {})])

    def test_uds_arg_runs_main_uds_with_default_socket(self):
        lc._dispatch(["uds"])
        self.assertEqual(self.calls, [("main_uds", (DEFAULT_SOCKET,), {})])

    def test_uds_with_path_runs_main_uds_with_that_path(self):
        lc._dispatch(["uds", "/tmp/custom.sock"])
        self.assertEqual(self.calls, [("main_uds", ("/tmp/custom.sock",), {})])

    def test_uds_selection_does_not_invoke_http_main(self):
        lc._dispatch(["uds"])
        self.assertNotIn("main", [name for name, _, _ in self.calls])


# ===========================================================================
# (G) Cursor pagination: query_all(send, query_form) -> (rows, pages, failure)
#
# query_all is transport-agnostic: it takes a `form -> body` callable and
# drains a paginated query by following the #cursor through (continue ...)
# until :done? is true. We drive it with a purely in-memory scripted fake --
# no socket, no urlopen -- that pops canned `:result`/`:error` bodies and
# records the form it was handed each call. The canned bodies are built with
# `edn_format.loads` so a `#cursor "c-1"` parses into the same `_Handle` the
# loop feeds back into (continue #cursor ...).
# ===========================================================================


class _ScriptedSend:
    """A scripted `form -> body` stand-in for query_all's `send` callable.

    Holds a list of canned EDN body strings; each call parses the next one with
    ``edn_format.loads`` and returns it, recording the `form` it was handed in
    ``forms``. Running past the script raises (a test that over-drives the send
    is a bug worth surfacing, not a silent empty read).
    """

    def __init__(self, bodies):
        self._bodies = [loads(b) for b in bodies]
        self._next = 0
        self.forms = []

    def __call__(self, form):
        self.forms.append(form)
        body = self._bodies[self._next]
        self._next += 1
        return body

    @property
    def call_count(self):
        return len(self.forms)


# A representative initial (query ...) form. query_all never inspects it -- it
# just hands it to send unchanged on the first call -- so its shape only needs
# to be plausible, not server-validated.
_QFORM = (
    Symbol("query"),
    {
        Keyword("find"): [Symbol("?x")],
        Keyword("where"): [[Symbol("member-of"), Symbol("?x"), entity("team")]],
        Keyword("limit"): 2,
    },
)


class QueryAllTests(unittest.TestCase):
    # --- (A) Multi-page drain ----------------------------------------------

    def test_multi_page_drain_concatenates_rows_in_order(self):
        send = _ScriptedSend([
            '{:event :result :rows [[#entity "alice"] [#entity "bob"]] '
            ':done? false :cursor #cursor "c-1"}',
            '{:event :result :rows [[#entity "carol"]] :done? true '
            ':cursor #cursor "c-1"}',
        ])

        rows, pages, failure = lc.query_all(send, _QFORM)

        self.assertIsNone(failure)
        self.assertEqual(pages, 2)
        self.assertEqual(
            rows,
            [[entity("alice")], [entity("bob")], [entity("carol")]],
        )

    def test_multi_page_drain_second_call_is_continue_carrying_the_cursor(self):
        send = _ScriptedSend([
            '{:event :result :rows [[#entity "alice"] [#entity "bob"]] '
            ':done? false :cursor #cursor "c-1"}',
            '{:event :result :rows [[#entity "carol"]] :done? true '
            ':cursor #cursor "c-1"}',
        ])

        lc.query_all(send, _QFORM)

        # The first form is the original query; the second is the continue that
        # carries the exact #cursor handle the first page returned.
        self.assertEqual(send.forms[0], _QFORM)
        self.assertEqual(
            send.forms[1],
            (Symbol("continue"), lc._Handle("cursor", "c-1")),
        )

    # --- (B) Single page ---------------------------------------------------

    def test_single_page_done_true_returns_one_page_without_continue(self):
        send = _ScriptedSend([
            '{:event :result :rows [[#entity "alice"]] :done? true}',
        ])

        rows, pages, failure = lc.query_all(send, _QFORM)

        self.assertIsNone(failure)
        self.assertEqual(pages, 1)
        self.assertEqual(rows, [[entity("alice")]])

    def test_single_page_invokes_send_exactly_once(self):
        # :done? true on the first page means the continue branch -- and the
        # :cursor read it depends on -- is never reached, so a body with NO
        # :cursor key must not raise KeyError.
        send = _ScriptedSend([
            '{:event :result :rows [[#entity "alice"]] :done? true}',
        ])

        lc.query_all(send, _QFORM)

        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.forms, [_QFORM])

    # --- (C) Failure propagation (expired cursor) --------------------------

    def test_continue_error_propagates_as_failure_with_rows_so_far(self):
        send = _ScriptedSend([
            '{:event :result :rows [[#entity "alice"] [#entity "bob"]] '
            ':done? false :cursor #cursor "c-1"}',
            '{:event :error :reason :unknown-handle '
            ':message "cursor c-1 has expired"}',
        ])

        rows, pages, failure = lc.query_all(send, _QFORM)

        # The error body is returned verbatim as `failure` -- no exception.
        self.assertIsNotNone(failure)
        self.assertEqual(failure[Keyword("event")], Keyword("error"))
        self.assertEqual(failure[Keyword("reason")], Keyword("unknown-handle"))
        # The rows gathered before the cursor expired are preserved.
        self.assertEqual(rows, [[entity("alice")], [entity("bob")]])
        # pages counts only the successfully-drained first page.
        self.assertEqual(pages, 1)

    def test_first_call_error_returns_empty_rows_and_zero_pages(self):
        # A query that fails outright (before any page lands) reports no rows,
        # zero pages, and the error body -- and never issues a continue.
        send = _ScriptedSend([
            '{:event :error :reason :malformed :message "bad query form"}',
        ])

        rows, pages, failure = lc.query_all(send, _QFORM)

        self.assertEqual(rows, [])
        self.assertEqual(pages, 0)
        self.assertIsNotNone(failure)
        self.assertEqual(failure[Keyword("event")], Keyword("error"))
        self.assertEqual(send.call_count, 1)


# ===========================================================================
# (H) read_welcome / ServerInfo: parse the :welcome surface (SPEC §10).
#
# read_welcome turns a parsed :welcome map into a queryable ServerInfo: a
# frozenset of capability Keywords, a :limits dict (with max_message_bytes
# convenience), and FLATTENED :verbs / :predicates Symbol sets (core unioned
# with every :extensions pack). Every section is optional -- a minimal welcome
# must not crash. The fixtures here are parsed with edn_format.loads so the
# objects match exactly what the live transports hand read_welcome.
# ===========================================================================


# A realistic welcome advertising three capabilities, a byte cap, and split
# core/extensions verb + predicate surfaces.
_REALISTIC_WELCOME = (
    '{:event :welcome :version 1 :session #session "s-1" :world nil '
    ':capabilities #{:lemma/v1 :lemma/cursor-pagination :lemma/watch} '
    ':limits {:max-message-bytes 1048576} '
    ':predicates {:core #{equivalent subset-of} :extensions {}} '
    ':verbs {:core #{hello query continue} :extensions {}}}'
)


class ReadWelcomeTests(unittest.TestCase):
    def test_supports_returns_true_for_an_advertised_capability(self):
        info = read_welcome(loads(_REALISTIC_WELCOME))
        self.assertTrue(info.supports(Keyword("lemma/cursor-pagination")))

    def test_supports_returns_false_for_an_unadvertised_capability(self):
        info = read_welcome(loads(_REALISTIC_WELCOME))
        self.assertFalse(info.supports(Keyword("lemma/nope")))

    def test_max_message_bytes_reads_the_advertised_limit(self):
        info = read_welcome(loads(_REALISTIC_WELCOME))
        self.assertEqual(info.max_message_bytes, 1048576)

    def test_verbs_surface_contains_the_core_symbols(self):
        info = read_welcome(loads(_REALISTIC_WELCOME))
        self.assertIn(Symbol("hello"), info.verbs)
        self.assertIn(Symbol("query"), info.verbs)
        self.assertIn(Symbol("continue"), info.verbs)

    def test_predicates_surface_contains_the_core_symbols(self):
        info = read_welcome(loads(_REALISTIC_WELCOME))
        self.assertIn(Symbol("equivalent"), info.predicates)
        self.assertIn(Symbol("subset-of"), info.predicates)

    def test_missing_limits_yields_none_max_message_bytes_without_crashing(self):
        # A welcome that omits :limits entirely must parse cleanly and report an
        # unadvertised (None) byte cap rather than raising.
        welcome = (
            '{:event :welcome :version 1 :session #session "s-1" :world nil '
            ':capabilities #{:lemma/v1}}'
        )
        info = read_welcome(loads(welcome))
        self.assertIsNone(info.max_message_bytes)

    def test_verb_extensions_packs_are_flattened_into_the_verb_set(self):
        # :verbs splits into :core plus per-pack :extensions; read_welcome unions
        # them, so an extension verb (foo) appears alongside the core ones.
        welcome = (
            '{:event :welcome :version 1 :session #session "s-1" :world nil '
            ':verbs {:core #{hello} :extensions {somepack #{foo}}}}'
        )
        info = read_welcome(loads(welcome))
        self.assertIn(Symbol("foo"), info.verbs)
        self.assertIn(Symbol("hello"), info.verbs)


# ===========================================================================
# (I) within_message_limit: is an EDN message under the server's byte cap?
#
# The cap is :max-message-bytes, measured in UTF-8 bytes (SPEC §10). An
# unadvertised cap (None) means unlimited, so any message passes.
# ===========================================================================


class WithinMessageLimitTests(unittest.TestCase):
    def test_small_payload_is_within_a_one_megabyte_limit(self):
        info = ServerInfo(
            version=1,
            capabilities=frozenset(),
            limits={Keyword("max-message-bytes"): 1048576},
            verbs=set(),
            predicates=set(),
        )
        self.assertTrue(within_message_limit(info, "(hello)"))

    def test_oversized_payload_against_a_tiny_limit_is_rejected(self):
        info = ServerInfo(
            version=1,
            capabilities=frozenset(),
            limits={Keyword("max-message-bytes"): 4},
            verbs=set(),
            predicates=set(),
        )
        # Eight bytes against a four-byte cap.
        self.assertFalse(within_message_limit(info, "(hello!!)"[:8]))

    def test_no_advertised_limit_always_passes(self):
        info = ServerInfo(
            version=1,
            capabilities=frozenset(),
            limits={},
            verbs=set(),
            predicates=set(),
        )
        self.assertIsNone(info.max_message_bytes)
        # An arbitrarily large payload still passes when no cap is advertised.
        self.assertTrue(within_message_limit(info, "x" * 10_000_000))


# ===========================================================================
# (J) Capability gating in the handshake: the paginated tail is gated on the
#     server advertising :lemma/cursor-pagination (SPEC §10). Without it,
#     main()/main_uds() must SKIP the seed-propose / paged-query block and
#     issue only the five base messages, printing the skip note. With it (the
#     default fixtures), the full nine-step paginated path runs.
# ===========================================================================


# A welcome WITHOUT cursor-pagination or watch: both gated tails are skipped, so
# only the five base steps run. (Watch is omitted too so these stay pure
# pagination-gating tests; the watch gate has its own suite below.)
_WELCOME_NO_PAGINATION = (
    '{:event :welcome :version 1 '
    ':session #session "s-77" :world #world "default" '
    ':capabilities #{:lemma/v1} '
    ':limits {:max-message-bytes 1048576} '
    ':verbs {:core #{hello use-world propose assert query} :extensions {}}}'
)
_UDS_WELCOME_NO_PAGINATION = (
    '{:event :welcome :version 1 '
    ':session #session "s-uds-1" :world #world "default" '
    ':capabilities #{:lemma/v1} '
    ':limits {:max-message-bytes 1048576} '
    ':verbs {:core #{hello use-world propose assert query} :extensions {}}}'
)

# The first five base steps a gated run still issues: hello, use-world,
# propose, assert, query.
_BASE_SEQUENCE = [
    _WELCOME_NO_PAGINATION,
    _WORLD_SELECTED,
    _PROPOSED,
    _ASSERTED,
    _RESULT,
]
_UDS_BASE_SEQUENCE = [
    _UDS_WELCOME_NO_PAGINATION,
    _UDS_WORLD_SELECTED,
    _UDS_PROPOSED,
    _UDS_ASSERTED,
    _UDS_RESULT,
]


class HttpCapabilityGatingTests(HandshakeBase):
    def test_missing_pagination_capability_skips_the_paged_query(self):
        fake = self.install(FakePostEdn(_BASE_SEQUENCE))

        self.run_main_capturing()

        # Only the five base messages were issued -- the seed-propose, its
        # assert, and the two-page query/continue were all skipped.
        self.assertEqual(len(fake.calls), 5)

    def test_missing_pagination_capability_prints_the_skip_note(self):
        self.install(FakePostEdn(_BASE_SEQUENCE))

        output = self.run_main_capturing()

        self.assertIn("does not advertise cursor pagination", output)

    def test_missing_pagination_capability_issues_no_continue(self):
        fake = self.install(FakePostEdn(_BASE_SEQUENCE))

        self.run_main_capturing()

        # No frame after the gate carries a (continue ...) verb head.
        for _path, form, _session, _base in fake.calls:
            self.assertNotEqual(dumps(form).split(" ", 1)[0], "(continue")
            self.assertFalse(dumps(form).startswith("(continue"))

    def test_present_pagination_capability_runs_the_full_thirteen_step_path(self):
        # The default fixture advertises :lemma/cursor-pagination, so the full
        # paginated path runs end to end, followed by the watch tail.
        fake = self.install(FakePostEdn(_FULL_SEQUENCE))

        self.run_main_capturing()

        self.assertEqual(len(fake.calls), 13)


class UdsCapabilityGatingTests(UdsHandshakeBase):
    def test_missing_pagination_capability_skips_the_paged_query(self):
        self.install_socket(_UDS_BASE_SEQUENCE)

        self.run_main_uds_capturing()

        sent = self.sent_frames_as_edn(self.created[0])
        # Only the five base frames -- the paged tail is gated out.
        self.assertEqual(len(sent), 5)

    def test_missing_pagination_capability_prints_the_skip_note(self):
        self.install_socket(_UDS_BASE_SEQUENCE)

        output = self.run_main_uds_capturing()

        self.assertIn("does not advertise cursor pagination", output)

    def test_missing_pagination_capability_issues_no_continue(self):
        self.install_socket(_UDS_BASE_SEQUENCE)

        self.run_main_uds_capturing()

        sent = self.sent_frames_as_edn(self.created[0])
        for frame in sent:
            self.assertFalse(frame.startswith("(continue"))

    def test_present_pagination_capability_runs_the_full_thirteen_step_path(self):
        self.install_socket(_UDS_FULL_SEQUENCE)

        self.run_main_uds_capturing()

        self.assertEqual(len(self.sent_frames_as_edn(self.created[0])), 13)


# ===========================================================================
# (K) SSE reader, SPLIT in two (ordering is load-bearing -- SPEC §9):
#
#   * open_sse_stream(base, session_id, timeout) connects a RAW socket
#     (socket.create_connection), writes the GET, and reads PAST the status
#     line + headers (the blank-line terminator) -- which is what registers
#     Dianoia's per-session SSE sink. It returns an open _SSEStream handle and
#     does NOT read any event bodies. Callers open this BEFORE triggering the
#     change, since the dispatcher delivers only to sinks present at emit time.
#   * read_sse_events(stream, max_events) drains parsed envelopes from an
#     already-open handle, transfer-decoding chunked + SSE framing -- because
#     http-kit flushes a size-0 chunk up front that a stock chunked reader
#     would mistake for EOF.
#
# These tests drive the real header-read AND the real decoder against a
# byte-accurate canned response (built by _canned_sse_response / _chunk above),
# so the header consumption, the size-0-keep-alive handling, the :-comment
# skipping, the max_events bound, and the terminate-on-EOF behaviour all run for
# real. socket.create_connection is the only seam patched; restored via
# addCleanup. The single _FakeSseSocket serves BOTH phases off one byte script
# -- open_sse_stream consumes the header bytes, read_sse_events drains the body
# bytes that follow -- exactly as a live socket would. No real network, no hang.
# ===========================================================================


class SseStreamTestBase(unittest.TestCase):
    """Shared SSE seam plumbing: patch socket.create_connection, record sockets."""

    def setUp(self):
        self._orig_create_connection = socket.create_connection
        self.created = []
        self.addCleanup(self._restore)

    def _restore(self):
        socket.create_connection = self._orig_create_connection

    def install_sse(self, response_bytes):
        created = self.created

        def factory(address, timeout=None):
            fake = _FakeSseSocket(response_bytes)
            fake.address = address
            fake.connect_timeout = timeout
            created.append(fake)
            return fake

        socket.create_connection = factory

    def open_stream(self, base="http://127.0.0.1:8080", session_id="s-1", **kw):
        """Open a stream and arrange for its socket to be closed at teardown."""
        stream = lc.open_sse_stream(base, session_id, **kw)
        self.addCleanup(stream.close)
        return stream


class OpenSseStreamTests(SseStreamTestBase):
    """open_sse_stream: connect, send a well-formed GET, consume headers only."""

    def test_opens_connection_to_host_and_port_parsed_from_base(self):
        self.install_sse(_canned_sse_response())

        self.open_stream(base="http://127.0.0.1:8080", session_id="s-9")

        self.assertEqual(self.created[0].address, ("127.0.0.1", 8080))

    def test_issues_get_on_the_session_events_endpoint_with_session_header(self):
        self.install_sse(_canned_sse_response())

        self.open_stream(base="http://127.0.0.1:8080", session_id="s-9")

        sent = self.created[0].sent.decode("utf-8")
        self.assertIn("GET /v1/sessions/s-9/events HTTP/1.1", sent)
        self.assertIn("Accept: text/event-stream", sent)
        # The sink-registration GET must carry the session header so Dianoia
        # binds the lazily-registered SSE sink to the right session.
        self.assertIn("X-Lemma-Session: s-9", sent)

    def test_consumes_headers_without_reading_event_bodies(self):
        # open_sse_stream reads PAST the blank-line header terminator (the act
        # that registers the sink) but must NOT consume event bodies. With the
        # canned response, the size-0 keep-alive + the data chunk follow the
        # headers, so the handle's buffer must still hold un-decoded body bytes
        # and contain no parsed event text -- read_sse_events will do the body
        # drain. We prove the split by showing the data chunk is still pending.
        self.install_sse(_canned_sse_response())

        stream = self.open_stream()

        # Headers are gone (the partition dropped them); the chunked body bytes
        # that begin with the size-0 keep-alive chunk are retained on the handle.
        self.assertNotIn(b"HTTP/1.1", stream.buf)
        self.assertNotIn(b"text/event-stream", stream.buf)
        # The event payload has NOT been parsed away -- its bytes are still on
        # the socket/buffer awaiting read_sse_events.
        remaining = stream.buf + self.created[0]._buf
        self.assertIn(b":watch-event", remaining)

    def test_returns_open_handle_whose_socket_is_the_connected_one(self):
        self.install_sse(_canned_sse_response())

        stream = self.open_stream()

        self.assertIs(stream.sock, self.created[0])

    def test_immediate_connection_close_during_headers_yields_empty_buffer(self):
        # The socket closes (recv -> b"") before the header terminator arrives:
        # open_sse_stream still returns a usable handle, with an empty buffer.
        self.install_sse(b"HTTP/1.1 200 OK\r\n")  # no blank-line terminator

        stream = self.open_stream()

        self.assertEqual(stream.buf, b"")

    def test_close_closes_the_underlying_socket(self):
        # The caller owns the socket; close() must release it so the server
        # tears the stream down.
        self.install_sse(_canned_sse_response())
        stream = lc.open_sse_stream("http://127.0.0.1:8080", "s-1")

        stream.close()

        self.assertTrue(self.created[0].closed)


class ReadSseEventsTests(SseStreamTestBase):
    """read_sse_events: drain parsed envelopes from an already-open handle."""

    # --- happy path: one event parsed off the chunked stream ----------------

    def test_yields_the_parsed_watch_event_off_the_chunked_stream(self):
        self.install_sse(_canned_sse_response())
        stream = self.open_stream()

        events = lc.read_sse_events(stream, max_events=1)

        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt[Keyword("event")], Keyword("watch-event"))
        self.assertEqual(evt[Keyword("type")], Keyword("assert"))
        # The :data carried a #fact, which round-trips into the client's _Fact.
        self.assertEqual(evt[Keyword("data")], loads(_WATCH_EVENT_EDN)[Keyword("data")])

    def test_size_zero_keep_alive_chunk_is_not_treated_as_end_of_stream(self):
        # The canned response leads with a size-0 chunk BEFORE the data chunk.
        # If read_sse_events mistook it for EOF it would return [] before ever
        # seeing the event; getting the event back proves the size-0 chunk was
        # skipped (kept reading) rather than ending the stream.
        self.install_sse(_canned_sse_response())
        stream = self.open_stream()

        events = lc.read_sse_events(stream, max_events=1)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][Keyword("event")], Keyword("watch-event"))

    def test_colon_comment_keep_alive_line_is_skipped(self):
        # The SSE block contains a leading ``: comment`` line; only the data:
        # line should feed the parse. If the comment leaked into the EDN, loads
        # would fail or the envelope would be malformed. A clean :watch-event
        # parse proves the comment was dropped.
        self.install_sse(_canned_sse_response())
        stream = self.open_stream()

        events = lc.read_sse_events(stream, max_events=1)

        self.assertEqual(events[0][Keyword("event")], Keyword("watch-event"))

    # --- max_events bound ---------------------------------------------------

    def test_honors_max_events_and_returns_after_that_many(self):
        # Feed two events but ask for only one: the loop must stop at one even
        # though more data remains on the wire.
        two = _canned_sse_response(blocks=(_SSE_BLOCK, _SSE_BLOCK))
        self.install_sse(two)
        stream = self.open_stream()

        events = lc.read_sse_events(stream, max_events=1)

        self.assertEqual(len(events), 1)

    def test_drains_multiple_events_when_max_events_allows(self):
        two = _canned_sse_response(blocks=(_SSE_BLOCK, _SSE_BLOCK))
        self.install_sse(two)
        stream = self.open_stream()

        events = lc.read_sse_events(stream, max_events=2)

        self.assertEqual(len(events), 2)
        self.assertTrue(
            all(e[Keyword("event")] == Keyword("watch-event") for e in events)
        )

    # --- termination / no-hang ----------------------------------------------

    def test_stream_end_before_any_event_returns_empty_without_hanging(self):
        # Headers + a lone size-0 keep-alive chunk, then EOF: no event ever
        # arrives. open_sse_stream consumes the headers; the subsequent read
        # must terminate and return [] rather than blocking.
        head = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        self.install_sse(head + _chunk(b""))
        stream = self.open_stream()

        events = lc.read_sse_events(stream, max_events=1)

        self.assertEqual(events, [])

    def test_immediate_connection_close_during_headers_returns_empty(self):
        # The socket closes (recv -> b"") before the header terminator arrives:
        # open_sse_stream hands back an empty-buffer handle and read_sse_events
        # returns [] rather than raising or hanging.
        self.install_sse(b"HTTP/1.1 200 OK\r\n")  # no blank-line terminator
        stream = self.open_stream()

        events = lc.read_sse_events(stream, max_events=1)

        self.assertEqual(events, [])

    def test_does_not_close_the_socket_the_caller_owns_it(self):
        # Under the split the reader only reads; teardown stays with the caller
        # (main() closes the stream in its finally). So read_sse_events must
        # leave the socket open.
        self.install_sse(_canned_sse_response())
        stream = self.open_stream()

        lc.read_sse_events(stream, max_events=1)

        self.assertFalse(self.created[0].closed)


# ===========================================================================
# (L) Watch capability gating: the watch tail is gated on :lemma/watch.
#
# (B) drive main()/main_uds() with a welcome whose :capabilities OMITS
#     :lemma/watch. The watch demo must be skipped: the skip note printed, no
#     (watch-pattern ...) frame sent, and -- over HTTP -- the SSE seam never
#     opened. The capability-PRESENT path is exercised by the full-sequence
#     suites above (which now include the watch tail end to end).
# ===========================================================================


# A welcome advertising pagination but NOT watch: the paged tail runs, the
# watch tail is gated out. Built so the full nine-step paginated path completes,
# then the watch block is skipped.
_WELCOME_NO_WATCH = (
    '{:event :welcome :version 1 '
    ':session #session "s-77" :world #world "default" '
    ':capabilities #{:lemma/v1 :lemma/cursor-pagination} '
    ':limits {:max-message-bytes 1048576} '
    ':verbs {:core #{hello use-world propose assert query} :extensions {}}}'
)
_UDS_WELCOME_NO_WATCH = (
    '{:event :welcome :version 1 '
    ':session #session "s-uds-1" :world #world "default" '
    ':capabilities #{:lemma/v1 :lemma/cursor-pagination} '
    ':limits {:max-message-bytes 1048576} '
    ':verbs {:core #{hello use-world propose assert query} :extensions {}}}'
)

# Pagination present, watch absent: the nine paginated steps run, then the watch
# tail is skipped -- nine post_edn calls / nine UDS frames, no watch-pattern.
_NO_WATCH_SEQUENCE = [
    _WELCOME_NO_WATCH,
    _WORLD_SELECTED,
    _PROPOSED,
    _ASSERTED,
    _RESULT,
    _PROPOSED_3X,
    _ASSERTED,
    _PAGE1,
    _PAGE2,
]
_UDS_NO_WATCH_SEQUENCE = [
    _UDS_WELCOME_NO_WATCH,
    _UDS_WORLD_SELECTED,
    _UDS_PROPOSED,
    _UDS_ASSERTED,
    _UDS_RESULT,
    _UDS_PROPOSED_3X,
    _UDS_ASSERTED,
    _UDS_PAGE1,
    _UDS_PAGE2,
]


class HttpWatchGatingTests(HandshakeBase):
    def test_missing_watch_capability_skips_the_watch_demo(self):
        # Pagination runs (nine calls), watch tail gated out -- so exactly nine
        # post_edn calls, none of them a (watch-pattern ...).
        fake = self.install(FakePostEdn(_NO_WATCH_SEQUENCE))

        self.run_main_capturing()

        self.assertEqual(len(fake.calls), 9)

    def test_missing_watch_capability_prints_the_skip_note(self):
        self.install(FakePostEdn(_NO_WATCH_SEQUENCE))

        output = self.run_main_capturing()

        self.assertIn("does not advertise watch", output)

    def test_missing_watch_capability_sends_no_watch_pattern_frame(self):
        fake = self.install(FakePostEdn(_NO_WATCH_SEQUENCE))

        self.run_main_capturing()

        for _path, form, _session, _base in fake.calls:
            self.assertFalse(dumps(form).startswith("(watch-pattern"))

    def test_missing_watch_capability_never_opens_the_sse_seam(self):
        # CRITICAL: the SSE stream must not be touched when watch is unadvertised.
        # disable_sse makes any socket.create_connection raise, so if the gated
        # code reached read_sse_events the test would fail loudly.
        self.disable_sse()
        self.install(FakePostEdn(_NO_WATCH_SEQUENCE))

        self.run_main_capturing()

        self.assertEqual(self.create_connection_calls, [])

    def test_present_watch_capability_opens_the_sse_seam_once(self):
        # The default fixture advertises :lemma/watch, so the watch tail runs
        # and read_sse_events opens the (stubbed) SSE seam exactly once.
        self.install(FakePostEdn(_FULL_SEQUENCE))

        self.run_main_capturing()

        self.assertEqual(len(self.create_connection_calls), 1)

    def test_present_watch_capability_prints_the_observed_event(self):
        self.install(FakePostEdn(_FULL_SEQUENCE))

        output = self.run_main_capturing()

        # The :watch-event drained off the SSE stream is reported, and the
        # watch is torn down.
        self.assertIn(":watch-event", output)
        self.assertIn("unwatch", output)

    def test_present_watch_capability_sends_unwatch_with_the_watch_handle(self):
        fake = self.install(FakePostEdn(_FULL_SEQUENCE))

        self.run_main_capturing()

        # The final post_edn call is the unwatch carrying the #watch handle from
        # the watch-established reply.
        last_form = fake.calls[-1][1]
        self.assertIsInstance(last_form, tuple)
        self.assertEqual(last_form[0], Symbol("unwatch"))
        self.assertEqual(last_form[1], loads('#watch "w-1"'))


class UdsWatchGatingTests(UdsHandshakeBase):
    def test_missing_watch_capability_skips_the_watch_demo(self):
        self.install_socket(_UDS_NO_WATCH_SEQUENCE)

        self.run_main_uds_capturing()

        sent = self.sent_frames_as_edn(self.created[0])
        self.assertEqual(len(sent), 9)

    def test_missing_watch_capability_prints_the_skip_note(self):
        self.install_socket(_UDS_NO_WATCH_SEQUENCE)

        output = self.run_main_uds_capturing()

        self.assertIn("does not advertise watch", output)

    def test_missing_watch_capability_sends_no_watch_pattern_frame(self):
        self.install_socket(_UDS_NO_WATCH_SEQUENCE)

        self.run_main_uds_capturing()

        for frame in self.sent_frames_as_edn(self.created[0]):
            self.assertFalse(frame.startswith("(watch-pattern"))


# ===========================================================================
# (M) UDS watch demux: _uds_await_watch_event picks the :watch-event out of
#     interleaved command-reply frames, and the watch path tears down with an
#     (unwatch ...). Driven both at the unit level (the helper alone, against a
#     scripted recv socket) and end-to-end through main_uds (the interleaved
#     full sequence above), so the demux and the unwatch are both verified.
# ===========================================================================


class UdsAwaitWatchEventTests(unittest.TestCase):
    """Unit-level: _uds_await_watch_event reads framed replies until the event."""

    def _socket_of(self, edn_frames):
        return _ScriptedRecvSocket([_frame(f) for f in edn_frames])

    def test_returns_watch_event_skipping_leading_command_replies(self):
        sock = self._socket_of([
            _UDS_STRAY_ECHO,            # a command echo -- must be skipped
            '{:event :asserted}',       # another non-event -- skipped
            _UDS_WATCH_EVENT,           # the event we want
        ])

        evt = lc._uds_await_watch_event(sock, max_frames=8)

        self.assertIsNotNone(evt)
        self.assertEqual(evt[Keyword("event")], Keyword("watch-event"))
        self.assertEqual(evt[Keyword("type")], Keyword("assert"))

    def test_returns_first_event_when_it_is_the_very_first_frame(self):
        sock = self._socket_of([_UDS_WATCH_EVENT])

        evt = lc._uds_await_watch_event(sock)

        self.assertEqual(evt[Keyword("event")], Keyword("watch-event"))

    def test_returns_none_when_no_event_within_max_frames(self):
        # Only command echoes, never an event: the bounded loop gives up and
        # returns None (the caller reports "no event observed") rather than
        # hanging.
        sock = self._socket_of(['{:event :asserted}'] * 3)

        evt = lc._uds_await_watch_event(sock, max_frames=3)

        self.assertIsNone(evt)

    def test_returns_none_on_premature_stream_close(self):
        # The frame stream closes (EOF) before any event: _recv_exactly raises
        # ConnectionError, which the helper swallows and reports as None.
        sock = _ScriptedRecvSocket([])  # immediately b"" -> EOF

        evt = lc._uds_await_watch_event(sock)

        self.assertIsNone(evt)

    def test_bounded_by_max_frames_even_with_more_non_event_frames(self):
        # Six non-event frames available but max_frames=2: the loop reads at
        # most two and returns None without draining the rest.
        sock = self._socket_of(['{:event :asserted}'] * 6)

        evt = lc._uds_await_watch_event(sock, max_frames=2)

        self.assertIsNone(evt)
        # Exactly two frames (each: a 4-byte prefix read + a body read) were
        # consumed -- the loop did not run away past its bound.
        self.assertLessEqual(len(sock._chunks), 4)


class UdsWatchPathEndToEndTests(UdsHandshakeBase):
    """End-to-end: main_uds demuxes the interleaved event and unwatches."""

    def test_watch_event_is_demuxed_and_reported(self):
        self.install_socket(_UDS_FULL_SEQUENCE)

        output = self.run_main_uds_capturing()

        # The interleaved :watch-event (which followed a stray command echo on
        # the wire) was picked out and reported.
        self.assertIn(":watch-event", output)
        self.assertNotIn("no event observed", output)

    def test_watch_path_sends_unwatch_with_the_watch_handle(self):
        self.install_socket(_UDS_FULL_SEQUENCE)

        self.run_main_uds_capturing()

        sent = self.sent_frames_as_edn(self.created[0])
        # The last frame is the unwatch carrying the #watch handle the
        # watch-established reply returned.
        last_form = loads(sent[-1])
        self.assertEqual(last_form[0], Symbol("unwatch"))
        self.assertEqual(last_form[1], loads('#watch "w-1"'))

    def test_watch_path_sends_a_watch_pattern_frame(self):
        self.install_socket(_UDS_FULL_SEQUENCE)

        self.run_main_uds_capturing()

        sent = self.sent_frames_as_edn(self.created[0])
        self.assertTrue(any(f.startswith("(watch-pattern") for f in sent))

    def test_watch_path_sets_a_read_timeout_so_a_missing_push_cannot_hang(self):
        # main_uds calls sock.settimeout(10.0) before the watch reads so a
        # missing push degrades to "no event observed" instead of blocking.
        self.install_socket(_UDS_FULL_SEQUENCE)

        self.run_main_uds_capturing()

        self.assertEqual(self.created[0].timeout, 10.0)


if __name__ == "__main__":
    unittest.main()
