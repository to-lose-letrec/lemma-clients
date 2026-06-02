#!/usr/bin/env python3
"""Unit tests for the stdlib-only Lemma client codec, transport, and recipe.

Run with zero third-party dependencies:

    python3 -m unittest test_lemma_client

(from the ``python/`` directory), or from the repo root:

    python3 -m unittest python.test_lemma_client

The suite covers three layers of ``lemma_client``:

  (A) the writer  -- Python value  -> exact EDN text
  (B) the reader  -- EDN text       -> Python value (incl. round-trips and
                     real response envelopes)
  (C) main()      -- the full handshake, driven with the HTTP seam
                     (``post_edn``) monkeypatched so no socket is opened.

Everything is deterministic: no network, no sleeps, no shared mutable state
that leaks between tests.
"""

import io
import socket
import struct
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout

import lemma_client as lc
from lemma_client import (
    DEFAULT_BASE,
    DEFAULT_SOCKET,
    Keyword,
    Lst,
    Symbol,
    Tagged,
    edn_read,
    edn_write,
    main,
    main_uds,
    post_edn,
    uds_recv_frame,
    uds_send_frame,
)


# ===========================================================================
# (A) Writer:  Python value  ->  exact EDN text
# ===========================================================================


class WriterScalarTests(unittest.TestCase):
    def test_none_writes_nil(self):
        self.assertEqual(edn_write(None), "nil")

    def test_true_writes_true(self):
        self.assertEqual(edn_write(True), "true")

    def test_false_writes_false(self):
        self.assertEqual(edn_write(False), "false")

    def test_int_writes_literal(self):
        self.assertEqual(edn_write(42), "42")

    def test_negative_int_writes_literal(self):
        self.assertEqual(edn_write(-3), "-3")

    def test_zero_writes_literal(self):
        self.assertEqual(edn_write(0), "0")

    def test_float_writes_literal(self):
        self.assertEqual(edn_write(3.14), "3.14")

    def test_bool_is_not_emitted_as_int(self):
        # bool subclasses int; the writer must test bool first.
        self.assertEqual(edn_write(True), "true")
        self.assertEqual(edn_write(False), "false")


class WriterStringTests(unittest.TestCase):
    def test_plain_string_is_quoted(self):
        self.assertEqual(edn_write("alice"), '"alice"')

    def test_empty_string_is_quoted_pair(self):
        self.assertEqual(edn_write(""), '""')

    def test_string_escapes_double_quote(self):
        self.assertEqual(edn_write('he said "hi"'), '"he said \\"hi\\""')

    def test_string_escapes_backslash(self):
        self.assertEqual(edn_write("a\\b"), '"a\\\\b"')

    def test_string_escapes_newline(self):
        self.assertEqual(edn_write("a\nb"), '"a\\nb"')

    def test_string_escapes_tab(self):
        self.assertEqual(edn_write("a\tb"), '"a\\tb"')

    def test_string_escapes_carriage_return(self):
        self.assertEqual(edn_write("a\rb"), '"a\\rb"')

    def test_backslash_escaped_before_introduced_escapes(self):
        # A literal backslash followed by 'n' must not become a newline escape.
        self.assertEqual(edn_write("\\n"), '"\\\\n"')


class WriterKeywordSymbolTests(unittest.TestCase):
    def test_keyword_writes_with_colon(self):
        self.assertEqual(edn_write(Keyword(":event")), ":event")

    def test_namespaced_keyword_writes_verbatim(self):
        self.assertEqual(edn_write(Keyword(":verbs/core")), ":verbs/core")

    def test_symbol_writes_bare_name(self):
        self.assertEqual(edn_write(Symbol("equivalent")), "equivalent")

    def test_query_variable_symbol_writes_with_question_mark(self):
        self.assertEqual(edn_write(Symbol("?o")), "?o")


class WriterCollectionTests(unittest.TestCase):
    def test_python_list_writes_as_vector(self):
        self.assertEqual(edn_write([1, 2, 3]), "[1 2 3]")

    def test_empty_list_writes_as_empty_vector(self):
        self.assertEqual(edn_write([]), "[]")

    def test_tuple_writes_as_vector(self):
        self.assertEqual(edn_write((1, 2, 3)), "[1 2 3]")

    def test_lst_writes_as_list_parens(self):
        self.assertEqual(
            edn_write(Lst([Symbol("a"), Symbol("b"), Symbol("c")])),
            "(a b c)",
        )

    def test_empty_lst_writes_as_empty_parens(self):
        self.assertEqual(edn_write(Lst([])), "()")

    def test_dict_writes_as_map_in_insertion_order(self):
        self.assertEqual(
            edn_write({Keyword(":a"): 1, Keyword(":b"): 2}),
            "{:a 1 :b 2}",
        )

    def test_empty_dict_writes_as_empty_map(self):
        self.assertEqual(edn_write({}), "{}")

    def test_set_writes_as_hash_brace(self):
        # A single-element set has a deterministic rendering.
        self.assertEqual(edn_write({Symbol("a")}), "#{a}")

    def test_frozenset_writes_as_hash_brace(self):
        self.assertEqual(edn_write(frozenset({1})), "#{1}")


class WriterTaggedTests(unittest.TestCase):
    def test_tagged_entity_separates_scalar_payload_with_space(self):
        self.assertEqual(edn_write(Tagged("entity", "alice")), '#entity "alice"')

    def test_tagged_world_default(self):
        self.assertEqual(edn_write(Tagged("world", "default")), '#world "default"')

    def test_tagged_with_map_payload_abuts_delimiter(self):
        # A collection payload opens with its own delimiter, so no space.
        out = edn_write(Tagged("fact", {Keyword(":k"): Symbol("v")}))
        self.assertEqual(out, "#fact{:k v}")

    def test_tagged_with_vector_payload_abuts_delimiter(self):
        self.assertEqual(edn_write(Tagged("ref", [1, 2])), "#ref[1 2]")


class WriterGrammarAcceptTests(unittest.TestCase):
    """Anchor the writer to concrete grammar accept-cases from the docstring."""

    def test_use_world_verb_form(self):
        form = Lst([Symbol("use-world"), Tagged("world", "default")])
        self.assertEqual(edn_write(form), '(use-world #world "default")')

    def test_propose_fact_form_reparses_to_expected_value(self):
        # The map's key order is not part of the contract, so assert via
        # re-parse equality rather than raw-string comparison.
        fact = Tagged("fact", {
            Keyword(":predicate"): Symbol("member-of"),
            Keyword(":subject"): Tagged("entity", "alice"),
            Keyword(":object"): Tagged("entity", "managers"),
        })
        form = Lst([Symbol("propose"), fact])
        expected = edn_read(
            '(propose #fact{:predicate member-of '
            ':subject #entity "alice" :object #entity "managers"})'
        )
        self.assertEqual(edn_read(edn_write(form)), expected)

    def test_unencodable_type_raises_typeerror(self):
        with self.assertRaises(TypeError):
            edn_write(object())


# ===========================================================================
# (B) Reader:  EDN text  ->  Python value
# ===========================================================================


class ReaderRoundTripTests(unittest.TestCase):
    """edn_read(edn_write(x)) == x for each supported value type."""

    def _round_trip(self, value):
        self.assertEqual(edn_read(edn_write(value)), value)

    def test_round_trip_nil(self):
        self._round_trip(None)

    def test_round_trip_true(self):
        self._round_trip(True)

    def test_round_trip_false(self):
        self._round_trip(False)

    def test_round_trip_int(self):
        self._round_trip(42)

    def test_round_trip_negative_int(self):
        self._round_trip(-7)

    def test_round_trip_float(self):
        self._round_trip(3.14)

    def test_round_trip_string_with_escapes(self):
        self._round_trip('tab\there\nnewline "quote" \\slash')

    def test_round_trip_keyword(self):
        self._round_trip(Keyword(":event"))

    def test_round_trip_symbol(self):
        self._round_trip(Symbol("member-of"))

    def test_round_trip_vector(self):
        self._round_trip([1, 2, 3])

    def test_round_trip_lst(self):
        self._round_trip(Lst([Symbol("hello")]))

    def test_round_trip_map(self):
        self._round_trip({Keyword(":a"): 1, Keyword(":b"): [2, 3]})

    def test_round_trip_set(self):
        self._round_trip({1, 2, 3})

    def test_round_trip_tagged_scalar(self):
        self._round_trip(Tagged("entity", "alice"))

    def test_round_trip_tagged_map(self):
        self._round_trip(Tagged("fact", {Keyword(":predicate"): Symbol("equivalent")}))

    def test_vector_reads_as_python_list(self):
        self.assertIsInstance(edn_read("[1 2 3]"), list)

    def test_list_reads_as_lst(self):
        self.assertIsInstance(edn_read("(a b c)"), Lst)


class ReaderEnvelopeTests(unittest.TestCase):
    """Parse real Lemma response envelopes and look up their salient keys."""

    def test_welcome_envelope_lookups(self):
        # Verbatim shape emitted by Dianoia 0.9.0: :version is an integer,
        # :verbs/:predicates are nested {:core #{...} :extensions {...}} maps,
        # and :limits carries :max-message-bytes.
        welcome = (
            '{:event :welcome, :version 1, :session #session "s-42", '
            ':world #world "default", '
            ':capabilities #{:lemma/v1 :lemma/watch :lemma/export}, '
            ':limits {:max-message-bytes 1048576}, '
            ':predicates {:core #{equivalent member-of disjoint} :extensions {}}, '
            ':verbs {:core #{hello use-world propose assert query} :extensions {}}}'
        )
        body = edn_read(welcome)
        self.assertEqual(body[Keyword(":event")], Keyword(":welcome"))
        self.assertEqual(body[Keyword(":version")], 1)
        self.assertEqual(body[Keyword(":session")], Tagged("session", "s-42"))
        self.assertEqual(body[Keyword(":world")], Tagged("world", "default"))
        core_verbs = body[Keyword(":verbs")][Keyword(":core")]
        self.assertIsInstance(core_verbs, set)
        self.assertIn(Symbol("propose"), core_verbs)
        self.assertIn(Symbol("query"), core_verbs)
        limits = body[Keyword(":limits")]
        self.assertEqual(limits[Keyword(":max-message-bytes")], 1048576)

    def test_result_envelope_lookups(self):
        # Dianoia binds a query variable to the entity's *name* (a plain
        # string), so result rows are [["venus"]], not [[#entity "venus"]].
        result = '{:event :result :rows [["venus"]] :done? true}'
        body = edn_read(result)
        self.assertEqual(body[Keyword(":event")], Keyword(":result"))
        self.assertEqual(body[Keyword(":rows")], [["venus"]])
        self.assertIs(body[Keyword(":done?")], True)

    def test_error_envelope_lookups(self):
        error = (
            '{:event :error '
            ':reason :malformed '
            ':message "could not parse verb form"}'
        )
        body = edn_read(error)
        self.assertEqual(body[Keyword(":event")], Keyword(":error"))
        self.assertEqual(body[Keyword(":reason")], Keyword(":malformed"))
        self.assertEqual(body[Keyword(":message")], "could not parse verb form")

    def test_unknown_tag_falls_back_to_tagged(self):
        self.assertEqual(
            edn_read('#inst "2026-05-09T12:34:56.789Z"'),
            Tagged("inst", "2026-05-09T12:34:56.789Z"),
        )


class ReaderWhitespaceAndCommentTests(unittest.TestCase):
    def test_commas_are_whitespace(self):
        self.assertEqual(
            edn_read("{:a 1, :b 2}"),
            {Keyword(":a"): 1, Keyword(":b"): 2},
        )

    def test_line_comment_is_ignored(self):
        text = (
            "; a leading comment\n"
            "{:a 1 ; trailing comment\n"
            " :b 2}\n"
        )
        self.assertEqual(
            edn_read(text),
            {Keyword(":a"): 1, Keyword(":b"): 2},
        )

    def test_leading_and_trailing_whitespace_tolerated(self):
        self.assertEqual(edn_read("  \n  42  \n "), 42)


class ReaderErrorTests(unittest.TestCase):
    def test_trailing_data_is_an_error(self):
        with self.assertRaises(lc.EDNReadError):
            edn_read("1 2")

    def test_unterminated_string_is_an_error(self):
        with self.assertRaises(lc.EDNReadError):
            edn_read('"unterminated')

    def test_empty_input_is_an_error(self):
        with self.assertRaises(lc.EDNReadError):
            edn_read("   ")


# ===========================================================================
# (C) Handshake: drive main() with the HTTP seam monkeypatched.
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

_ERROR = '{:event :error :reason :malformed :message "bad verb form"}'
_REJECTED = (
    '{:event :rejected :reason :inconsistent '
    ':violations [#violation "cycle"]}'
)


class FakePostEdn:
    """A scripted stand-in for ``lemma_client.post_edn``.

    Each call pops the next canned EDN body off ``responses``, parses it, and
    returns ``(body, session_id)``. The session id surfaced for the *first*
    (welcome) reply comes from ``welcome_session``, mimicking the server
    setting the ``X-Lemma-Session`` response header; later replies echo back
    whatever session the caller threaded in.

    It records every call's ``(path, form, session)`` for assertions.
    """

    def __init__(self, responses, welcome_session="s-77"):
        self._responses = list(responses)
        self._welcome_session = welcome_session
        self.calls = []

    def __call__(self, path, form, session=None, base=lc.DEFAULT_BASE):
        self.calls.append((path, form, session, base))
        raw = self._responses.pop(0)
        body = edn_read(raw)
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
        fake = self.install(
            FakePostEdn([_WELCOME, _WORLD_SELECTED, _PROPOSED, _ASSERTED, _RESULT])
        )
        output = self.run_main_capturing()
        # All five protocol steps were issued.
        self.assertEqual(len(fake.calls), 5)
        # The conversation reached the query/result line.
        self.assertIn("rows=", output)
        self.assertIn('"venus"', output)

    def test_first_call_is_anonymous_hello_on_messages_endpoint(self):
        fake = self.install(
            FakePostEdn([_WELCOME, _WORLD_SELECTED, _PROPOSED, _ASSERTED, _RESULT])
        )
        self.run_main_capturing()
        first_path, first_form, first_session, _ = fake.calls[0]
        self.assertEqual(first_path, "/v1/messages")
        self.assertIsNone(first_session)
        self.assertEqual(edn_write(first_form), "(hello)")

    def test_session_id_from_header_is_used_in_named_endpoint_path(self):
        fake = self.install(
            FakePostEdn(
                [_WELCOME, _WORLD_SELECTED, _PROPOSED, _ASSERTED, _RESULT],
                welcome_session="s-77",
            )
        )
        self.run_main_capturing()
        # Every call after the hello must target the named-session endpoint
        # built from the X-Lemma-Session header value, and echo it back.
        for path, _form, session, _base in fake.calls[1:]:
            self.assertEqual(path, "/v1/sessions/s-77/messages")
            self.assertEqual(session, "s-77")

    def test_base_url_is_threaded_through(self):
        fake = self.install(
            FakePostEdn([_WELCOME, _WORLD_SELECTED, _PROPOSED, _ASSERTED, _RESULT])
        )
        self.run_main_capturing(base="http://example.test:9999")
        for _path, _form, _session, base in fake.calls:
            self.assertEqual(base, "http://example.test:9999")

    def test_proposal_handle_is_threaded_into_assert(self):
        fake = self.install(
            FakePostEdn([_WELCOME, _WORLD_SELECTED, _PROPOSED, _ASSERTED, _RESULT])
        )
        self.run_main_capturing()
        # Call index 3 is the assert; its form is (assert <proposal>) and the
        # proposal must be the #proposal handle returned by the propose reply.
        assert_form = fake.calls[3][1]
        self.assertIsInstance(assert_form, Lst)
        self.assertEqual(assert_form.items[0], Symbol("assert"))
        self.assertEqual(assert_form.items[1], Tagged("proposal", "p-1"))


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
# (D) Transport: exercise post_edn's OWN urlopen call, header construction,
#     and its two except branches by patching the urlopen seam.
#
# The handshake tests above monkeypatch post_edn itself, so post_edn's
# transport body is otherwise untested. Here we replace the lower seam --
# urllib.request.urlopen (the code calls it fully-qualified) -- so the real
# Request construction and except branches run.
#
# These three cases map 1:1 to the Ergo error model:
#   (a) happy path           -> the success branch
#   (b) recovered HTTPError   -> guardsOn(post-edn, HTTPError)
#                                + coercesTo(HTTPError, ErrorEnvelope)
#   (c) URLError              -> mayFail(post-edn, connection-refused)
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

        body, session_id = post_edn("/v1/messages", Lst([Symbol("hello")]))

        self.assertEqual(body, edn_read(canned))
        self.assertEqual(session_id, "s-77")

    def test_happy_path_builds_request_full_url_from_base_plus_path(self):
        captured = {}

        def fake_urlopen(request):
            captured["request"] = request
            return _FakeResponse(b"{:event :result}", None)

        self._patch_urlopen(fake_urlopen)

        post_edn("/v1/messages", Lst([Symbol("hello")]), base="http://example.test:9999")

        self.assertEqual(
            captured["request"].full_url, "http://example.test:9999/v1/messages"
        )

    def test_happy_path_sends_application_edn_content_type(self):
        captured = {}

        def fake_urlopen(request):
            captured["request"] = request
            return _FakeResponse(b"{:event :result}", None)

        self._patch_urlopen(fake_urlopen)

        post_edn("/v1/messages", Lst([Symbol("hello")]))

        # urllib title-cases header keys; check case-insensitively.
        self.assertEqual(
            captured["request"].get_header("Content-type"), "application/edn"
        )

    def test_happy_path_encodes_form_as_utf8_edn_body(self):
        captured = {}
        form = Lst([Symbol("hello")])

        def fake_urlopen(request):
            captured["request"] = request
            return _FakeResponse(b"{:event :result}", None)

        self._patch_urlopen(fake_urlopen)

        post_edn("/v1/messages", form)

        self.assertEqual(captured["request"].data, edn_write(form).encode("utf-8"))

    def test_happy_path_without_session_omits_session_header(self):
        captured = {}

        def fake_urlopen(request):
            captured["request"] = request
            return _FakeResponse(b"{:event :result}", None)

        self._patch_urlopen(fake_urlopen)

        post_edn("/v1/messages", Lst([Symbol("hello")]))

        self.assertIsNone(captured["request"].get_header("X-lemma-session"))

    def test_happy_path_with_session_sends_session_header(self):
        captured = {}

        def fake_urlopen(request):
            captured["request"] = request
            return _FakeResponse(b"{:event :result}", "s-77")

        self._patch_urlopen(fake_urlopen)

        post_edn("/v1/sessions/s-77/messages", Lst([Symbol("query")]), session="s-77")

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

        body, _session_id = post_edn("/v1/messages", Lst([Symbol("hello")]))

        self.assertEqual(body[Keyword(":event")], Keyword(":error"))

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

        _body, session_id = post_edn("/v1/messages", Lst([Symbol("hello")]))

        self.assertEqual(session_id, "s-99")

    # --- (c) URLError: re-raised as a ConnectionError naming base ----------

    def test_url_error_is_translated_to_actionable_connection_error(self):
        original = urllib.error.URLError("Connection refused")

        def fake_urlopen(request):
            raise original

        self._patch_urlopen(fake_urlopen)

        with self.assertRaises(ConnectionError) as ctx:
            post_edn("/v1/messages", Lst([Symbol("hello")]), base="http://down.test:1234")

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
# (E) UDS framing:  uds_send_frame / uds_recv_frame / _recv_exactly
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
    """A fake socket that records every ``sendall`` payload.

    Only the write side is exercised here; ``sendall`` concatenates each call's
    bytes both into a per-call list and a single running buffer.
    """

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
        form = Lst([Symbol("hello")])
        edn = edn_write(form)

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
        edn = edn_write(Tagged("entity", "vénus"))

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
        form = Lst([Symbol("query")])
        sender = _SendRecorderSocket()
        uds_send_frame(sender, edn_write(form))
        receiver = _ScriptedRecvSocket([sender.sends[0]])

        got = uds_recv_frame(receiver)

        self.assertEqual(edn_read(got), form)

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
# (F) UDS handshake: drive main_uds() with no real socket.
#
# socket.socket is monkeypatched to return a scripted fake that supports
# connect()/sendall()/recv()/close() and the context-manager protocol. The
# canned reply frames are built with the real framing primitives so they are
# byte-correct. Determinism: no real sockets, no sleeps; socket.socket is
# restored via addCleanup.
# ===========================================================================


class _FakeUdsSocket:
    """A scripted Unix-domain-socket stand-in for main_uds().

    Construction records the (family, type) the client requested. ``connect``
    is a no-op that records the path. ``sendall`` records each frame's raw
    bytes. ``recv`` replays the welcome/world-selected/proposed/asserted/result
    reply frames, delivered as a single in-memory byte stream so multi-byte
    reads work naturally. Supports close() and the context-manager protocol.
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

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


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

_UDS_FULL_SEQUENCE = [
    _UDS_WELCOME,
    _UDS_WORLD_SELECTED,
    _UDS_PROPOSED,
    _UDS_ASSERTED,
    _UDS_RESULT,
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

    def test_sends_five_frames_one_per_protocol_step(self):
        self.install_socket(_UDS_FULL_SEQUENCE)

        self.run_main_uds_capturing()

        self.assertEqual(len(self.sent_frames_as_edn(self.created[0])), 5)

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
        assert_form = edn_read(sent[3])
        self.assertIsInstance(assert_form, Lst)
        self.assertEqual(assert_form.items[0], Symbol("assert"))
        self.assertEqual(assert_form.items[1], Tagged("proposal", "p-1"))

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
                ':violations [#violation "cycle"]}',
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


if __name__ == "__main__":
    unittest.main()
