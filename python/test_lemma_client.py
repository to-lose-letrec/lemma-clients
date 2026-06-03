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
    entity,
    fact,
    main,
    main_uds,
    post_edn,
    uds_recv_frame,
    uds_send_frame,
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

# The full nine-step main() sequence: hello, use-world, propose, assert, query,
# propose(3x), assert, paged-query(page1), continue(page2).
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


class HandshakeBase(unittest.TestCase):
    def setUp(self):
        self._orig_post_edn = lc.post_edn
        self.addCleanup(self._restore)

    def _restore(self):
        lc.post_edn = self._orig_post_edn

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
        # All nine protocol steps were issued (the tail is the paginated query
        # plus its continue).
        self.assertEqual(len(fake.calls), 9)
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
        self._recv_buffer = b""

    # -- script the replies the server would send back -----------------------

    def load_replies(self, edn_replies):
        self._recv_buffer = b"".join(_frame(r) for r in edn_replies)

    # -- socket surface used by main_uds -------------------------------------

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

    def test_sends_nine_frames_one_per_protocol_step(self):
        self.install_socket(_UDS_FULL_SEQUENCE)

        self.run_main_uds_capturing()

        self.assertEqual(len(self.sent_frames_as_edn(self.created[0])), 9)

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


if __name__ == "__main__":
    unittest.main()
