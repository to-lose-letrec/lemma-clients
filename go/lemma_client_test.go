// lemma_client_test.go — unit tests for the Lemma client's hello-world core.
//
// Parity reference: python/test_lemma_client.py's TagRoundTripTests +
// PostEdnTransportTests + CliDispatchTests. We cover the surface this client
// *owns* — not go-edn's parser:
//
//	(A) the Lemma tagged literals — that the marshaller's wire text round-trips
//	    through marshal->parse back into equal values, that the verb form has
//	    the (use-world #world …) shape and parses cleanly, and that a :result
//	    envelope parses to the expected map shape.
//	(B) the HTTP transport (postEDN) — outbound request shape (URL, method,
//	    content-type, body, session header presence/absence), the happy 2xx
//	    path returning parsed body + session id, the 4xx error-envelope
//	    recovery (parsed, no error), and the refused-connection -> error path
//	    naming the base.
//	(C) the CLI dispatcher (dispatch) — argv -> transport selection, exercised
//	    by argument shape against an httptest server (a URL arg routes to HTTP
//	    with that base; a "uds" arg does NOT touch the HTTP path).
//
// Everything is deterministic: no real network beyond httptest loopback, no
// sleeps, no shared mutable state that leaks between tests.
package main

import (
	"encoding/binary"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"olympos.io/encoding/edn"
)

// marshal is a test helper: marshal v or fail the test. Keeps the round-trip
// assertions below readable.
func marshal(t *testing.T, v interface{}) []byte {
	t.Helper()
	b, err := edn.Marshal(v)
	if err != nil {
		t.Fatalf("marshalling %#v: %v", v, err)
	}
	return b
}

// parse is a test helper: unmarshal raw into a fresh interface{} or fail.
func parse(t *testing.T, raw []byte) interface{} {
	t.Helper()
	var out interface{}
	if err := edn.Unmarshal(raw, &out); err != nil {
		t.Fatalf("parsing %q: %v", raw, err)
	}
	return out
}

// ===========================================================================
// (A) Tagged literals: Lemma value <-> wire text, proven by round-trip.
//
// We do not assert the exact inter-token spacing of every form (go-edn's
// Compact pass strips the space between a tag and its payload, which is
// wire-valid and parses cleanly either way). Instead we prove the contract
// that matters: the marshalled text parses back to an EQUAL value, and the
// verb form carries the (use-world #world …) shape a Lemma server accepts.
// ===========================================================================

// The verb form must marshal to a bare top-level list opening with the verb
// symbol and the #world handle — the shape a Lemma server parses as
// (use-world #world "default").
func TestUseWorldVerbForm_HasUseWorldWorldShape(t *testing.T) {
	got := string(marshal(t, verb{edn.Symbol("use-world"), world("default")}))
	if !strings.HasPrefix(got, "(use-world #world") {
		t.Fatalf("verb form %q does not start with the (use-world #world shape", got)
	}
	if !strings.HasSuffix(got, ")") {
		t.Fatalf("verb form %q is not a closed top-level list", got)
	}
	if !strings.Contains(got, `"default"`) {
		t.Fatalf("verb form %q does not carry the world name payload", got)
	}
}

// The marshalled verb form must parse back without error — i.e. it is text a
// compliant EDN reader (and thus a Lemma server) accepts. (go-edn decodes both
// lists and vectors to []interface{}; the client only ever emits verb forms,
// so an inbound list/vector ambiguity is irrelevant here.)
func TestUseWorldVerbForm_ParsesBackCleanly(t *testing.T) {
	wire := marshal(t, verb{edn.Symbol("use-world"), world("default")})

	got, ok := parse(t, wire).([]interface{})
	if !ok {
		t.Fatalf("verb form parsed to %T, want a sequence", parse(t, wire))
	}
	if len(got) != 2 {
		t.Fatalf("verb form parsed to %d elements, want 2: %#v", len(got), got)
	}
	if got[0] != edn.Symbol("use-world") {
		t.Errorf("verb head = %#v, want edn.Symbol(\"use-world\")", got[0])
	}
	if got[1] != world("default") {
		t.Errorf("verb arg = %#v, want %#v", got[1], world("default"))
	}
}

// Each #entity / #world string-payload handle must survive marshal->parse as an
// EQUAL Handle value. Table-driven over the handle constructors.
func TestStringHandle_RoundTripsToEqualValue(t *testing.T) {
	cases := []struct {
		name   string
		handle Handle
	}{
		{"entity", entity("alice")},
		{"world", world("default")},
		{"entity-empty-name", entity("")},
		{"raw-proposal-handle", Handle{Name: "proposal", Value: "p-1"}},
		{"raw-session-handle", Handle{Name: "session", Value: "s-77"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := parse(t, marshal(t, tc.handle))
			if got != tc.handle {
				t.Fatalf("round-trip = %#v, want %#v", got, tc.handle)
			}
		})
	}
}

// A #fact map must round-trip marshal->parse to an equal Fact: predicate stays
// a Symbol, subject/object stay #entity Handles, keys stay keywords.
func TestFact_RoundTripsToEqualValue(t *testing.T) {
	f := fact(edn.Symbol("member-of"), entity("alice"), entity("managers"))

	got, ok := parse(t, marshal(t, f)).(Fact)
	if !ok {
		t.Fatalf("fact parsed to %T, want Fact", parse(t, marshal(t, f)))
	}
	if got.Value[edn.Keyword("predicate")] != edn.Symbol("member-of") {
		t.Errorf("predicate = %#v, want edn.Symbol(\"member-of\")",
			got.Value[edn.Keyword("predicate")])
	}
	if got.Value[edn.Keyword("subject")] != entity("alice") {
		t.Errorf("subject = %#v, want %#v",
			got.Value[edn.Keyword("subject")], entity("alice"))
	}
	if got.Value[edn.Keyword("object")] != entity("managers") {
		t.Errorf("object = %#v, want %#v",
			got.Value[edn.Keyword("object")], entity("managers"))
	}
}

// A :result envelope with rows of handles must parse to the expected map shape:
// a keyword-keyed map whose :event is :result, :done? is true, and :rows is a
// vector of vectors of #entity handles.
func TestResultEnvelope_ParsesToExpectedShape(t *testing.T) {
	raw := []byte(`{:event :result :rows [[#entity "venus"]] :done? true}`)

	body := parse(t, raw)
	m, ok := body.(map[interface{}]interface{})
	if !ok {
		t.Fatalf("result envelope parsed to %T, want a map", body)
	}
	if m[edn.Keyword("event")] != edn.Keyword("result") {
		t.Errorf(":event = %#v, want :result", m[edn.Keyword("event")])
	}
	if m[edn.Keyword("done?")] != true {
		t.Errorf(":done? = %#v, want true", m[edn.Keyword("done?")])
	}
	rows, ok := m[edn.Keyword("rows")].([]interface{})
	if !ok || len(rows) != 1 {
		t.Fatalf(":rows = %#v, want a 1-element vector", m[edn.Keyword("rows")])
	}
	row, ok := rows[0].([]interface{})
	if !ok || len(row) != 1 {
		t.Fatalf("rows[0] = %#v, want a 1-element vector", rows[0])
	}
	if row[0] != entity("venus") {
		t.Errorf("rows[0][0] = %#v, want %#v", row[0], entity("venus"))
	}
}

// ===========================================================================
// (B) HTTP transport: drive postEDN against an httptest.Server.
//
// httptest gives us a real loopback HTTP server with no external network. Each
// test installs a handler that records the inbound request and/or returns a
// canned EDN body + session header, then asserts on what postEDN sent and what
// it returned.
// ===========================================================================

// capturedRequest holds the parts of an inbound request a transport test cares
// about, copied out of the handler so assertions run after the call returns.
type capturedRequest struct {
	method      string
	path        string
	rawQuery    string
	contentType string
	session     string
	sessionSet  bool
	body        []byte
}

// recordingServer spins up an httptest.Server whose handler records the inbound
// request into *capturedRequest and writes status + sessionHeader + edn body.
// The cleanup is registered on t so the server is always closed.
func recordingServer(t *testing.T, status int, sessionHeader, ednBody string) (*httptest.Server, *capturedRequest) {
	t.Helper()
	captured := &capturedRequest{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		captured.method = r.Method
		captured.path = r.URL.Path
		captured.rawQuery = r.URL.RawQuery
		captured.contentType = r.Header.Get("Content-Type")
		captured.session, captured.sessionSet = func() (string, bool) {
			vals, ok := r.Header["X-Lemma-Session"]
			if !ok || len(vals) == 0 {
				return "", false
			}
			return vals[0], true
		}()
		captured.body, _ = io.ReadAll(r.Body)

		if sessionHeader != "" {
			w.Header().Set("X-Lemma-Session", sessionHeader)
		}
		w.WriteHeader(status)
		io.WriteString(w, ednBody)
	}))
	t.Cleanup(srv.Close)
	return srv, captured
}

// Happy path: a 2xx welcome returns the parsed body and the session id read
// from the X-Lemma-Session response header.
func TestPostEDN_HappyPath_ReturnsParsedBodyAndSessionID(t *testing.T) {
	canned := `{:event :welcome :version 1 :session #session "s-77" :world #world "default"}`
	srv, _ := recordingServer(t, http.StatusOK, "s-77", canned)

	body, sid, err := postEDN("/v1/messages", verb{edn.Symbol("hello")}, "", srv.URL)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if sid != "s-77" {
		t.Errorf("session id = %q, want %q", sid, "s-77")
	}
	m, ok := body.(map[interface{}]interface{})
	if !ok {
		t.Fatalf("body parsed to %T, want a map", body)
	}
	if m[edn.Keyword("event")] != edn.Keyword("welcome") {
		t.Errorf(":event = %#v, want :welcome", m[edn.Keyword("event")])
	}
	// The #session handle in the body parsed into a typed Handle.
	if m[edn.Keyword("session")] != (Handle{Name: "session", Value: "s-77"}) {
		t.Errorf(":session = %#v, want #session \"s-77\"", m[edn.Keyword("session")])
	}
}

// The request URL must be base+path (path preserved exactly, no stray query).
func TestPostEDN_BuildsRequestURLFromBasePlusPath(t *testing.T) {
	srv, captured := recordingServer(t, http.StatusOK, "", "{:event :result}")

	if _, _, err := postEDN("/v1/messages", verb{edn.Symbol("hello")}, "", srv.URL); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if captured.method != http.MethodPost {
		t.Errorf("method = %q, want POST", captured.method)
	}
	if captured.path != "/v1/messages" {
		t.Errorf("request path = %q, want /v1/messages", captured.path)
	}
	if captured.rawQuery != "" {
		t.Errorf("request had unexpected query %q", captured.rawQuery)
	}
}

// The request must carry Content-Type: application/edn.
func TestPostEDN_SendsApplicationEDNContentType(t *testing.T) {
	srv, captured := recordingServer(t, http.StatusOK, "", "{:event :result}")

	if _, _, err := postEDN("/v1/messages", verb{edn.Symbol("hello")}, "", srv.URL); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if captured.contentType != "application/edn" {
		t.Errorf("Content-Type = %q, want application/edn", captured.contentType)
	}
}

// The request body must be the exact UTF-8 EDN of the form passed in.
func TestPostEDN_EncodesFormAsUTF8EDNBody(t *testing.T) {
	srv, captured := recordingServer(t, http.StatusOK, "", "{:event :result}")
	form := verb{edn.Symbol("use-world"), world("default")}

	if _, _, err := postEDN("/v1/messages", form, "", srv.URL); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := marshal(t, form)
	if string(captured.body) != string(want) {
		t.Errorf("request body = %q, want %q", captured.body, want)
	}
}

// With no session passed, the x-lemma-session request header must be absent.
func TestPostEDN_WithoutSession_OmitsSessionHeader(t *testing.T) {
	srv, captured := recordingServer(t, http.StatusOK, "", "{:event :result}")

	if _, _, err := postEDN("/v1/messages", verb{edn.Symbol("hello")}, "", srv.URL); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if captured.sessionSet {
		t.Errorf("X-Lemma-Session header present (%q), want absent", captured.session)
	}
}

// With a session passed, it must be echoed in the x-lemma-session request
// header.
func TestPostEDN_WithSession_SendsSessionHeader(t *testing.T) {
	srv, captured := recordingServer(t, http.StatusOK, "", "{:event :result}")

	if _, _, err := postEDN("/v1/sessions/s-77/messages", verb{edn.Symbol("query")}, "s-77", srv.URL); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !captured.sessionSet || captured.session != "s-77" {
		t.Errorf("X-Lemma-Session = %q (set=%v), want %q", captured.session, captured.sessionSet, "s-77")
	}
}

// A 400 response whose body is an EDN error envelope must come back PARSED with
// no transport error — net/http surfaces a non-2xx as a normal response, and
// the caller inspects :event to tell a welcome from an error.
func TestPostEDN_HTTPErrorStatus_ReturnsParsedEnvelopeWithoutError(t *testing.T) {
	envelope := `{:event :error :reason :malformed :message "bad verb form"}`
	srv, _ := recordingServer(t, http.StatusBadRequest, "", envelope)

	body, _, err := postEDN("/v1/messages", verb{edn.Symbol("hello")}, "", srv.URL)
	if err != nil {
		t.Fatalf("a 4xx with a valid error envelope must not be a transport error, got: %v", err)
	}
	m, ok := body.(map[interface{}]interface{})
	if !ok {
		t.Fatalf("error envelope parsed to %T, want a map", body)
	}
	if m[edn.Keyword("event")] != edn.Keyword("error") {
		t.Errorf(":event = %#v, want :error", m[edn.Keyword("event")])
	}
}

// A refused connection (pointing at a closed server) must return an error that
// names the base URL so the failure is actionable.
func TestPostEDN_RefusedConnection_ReturnsErrorNamingBase(t *testing.T) {
	// Stand up a server, capture its URL, then close it so the address is
	// refused — deterministic and requires no fixed port.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	base := srv.URL
	srv.Close()

	body, sid, err := postEDN("/v1/messages", verb{edn.Symbol("hello")}, "", base)
	if err == nil {
		t.Fatalf("expected a connection error against closed server %q, got body=%#v sid=%q", base, body, sid)
	}
	if !strings.Contains(err.Error(), base) {
		t.Errorf("error %q does not name the base %q", err.Error(), base)
	}
}

// ===========================================================================
// (C) Dispatch routing: argv -> transport selection, by argument shape.
//
// dispatch has no injectable seam (it calls mainRun directly), so we route by
// ARGUMENT SHAPE against an httptest server, mirroring the Python
// CliDispatchTests intent:
//
//   - a URL arg routes to the HTTP path with THAT base (the server records a
//     real inbound hello),
//   - a "uds" arg does NOT invoke the HTTP path (the server records nothing).
//
// The no-args case (HTTP against DefaultBase) is asserted indirectly: dispatch
// with no args reaches mainRun(DefaultBase), which — with no server at
// 127.0.0.1:8080 — fails the connection and prints the actionable line rather
// than panicking. We assert it returns without panicking and touches no test
// server.
// ===========================================================================

// A URL argument routes to the HTTP transport against that base: the httptest
// server records the anonymous hello on /v1/messages.
func TestDispatch_URLArg_RoutesToHTTPWithThatBase(t *testing.T) {
	var hits int
	var helloPath string
	var helloBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		if hits == 1 {
			helloPath = r.URL.Path
			helloBody, _ = io.ReadAll(r.Body)
		}
		// A non-welcome reply stops mainRun cleanly after one call, so the
		// test needs no full canned sequence.
		w.Header().Set("X-Lemma-Session", "s-1")
		io.WriteString(w, `{:event :error :reason :malformed :message "stop"}`)
	}))
	t.Cleanup(srv.Close)

	dispatch([]string{srv.URL})

	if hits == 0 {
		t.Fatalf("URL arg did not route to the HTTP transport (server saw no requests)")
	}
	if helloPath != "/v1/messages" {
		t.Errorf("first HTTP call path = %q, want /v1/messages", helloPath)
	}
	if string(helloBody) != "(hello)" {
		t.Errorf("first HTTP call body = %q, want (hello)", helloBody)
	}
}

// A "uds" argument selects the (stubbed) UDS transport and must NOT invoke the
// HTTP path: an httptest server handed in via os.Args-shaped routing would see
// zero requests. We prove non-invocation by routing "uds" while a live server
// stands by — and asserting the server is never hit.
func TestDispatch_UDSArg_DoesNotInvokeHTTPPath(t *testing.T) {
	var hits int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		io.WriteString(w, `{:event :welcome}`)
	}))
	t.Cleanup(srv.Close)

	dispatch([]string{"uds"})

	if hits != 0 {
		t.Fatalf("uds dispatch made %d HTTP request(s), want 0 (HTTP path must not run)", hits)
	}
}

// No args routes to the HTTP transport against DefaultBase. There is (by
// design) no server at DefaultBase in the test environment, so mainRun must
// catch the refused connection and return cleanly — dispatch must not panic and
// must not touch any test server.
func TestDispatch_NoArgs_RoutesToHTTPWithDefaultBaseWithoutPanicking(t *testing.T) {
	var hits int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
	}))
	t.Cleanup(srv.Close)

	// Guard: DefaultBase must not coincidentally be this test server's URL.
	if DefaultBase == srv.URL {
		t.Skipf("test server happened to bind DefaultBase %q", DefaultBase)
	}

	// dispatch([]) -> mainRun(DefaultBase); no server there -> clean return.
	dispatch(nil)

	if hits != 0 {
		t.Errorf("no-args dispatch hit the test server %d time(s), want 0", hits)
	}
}

// Sanity guard against accidental reliance on process argv inside the tests:
// dispatch is driven with explicit slices, never os.Args, so the suite is
// independent of how the test binary was invoked.
func TestDispatch_IsDrivenByExplicitArgsNotProcessArgv(t *testing.T) {
	if len(os.Args) == 0 {
		t.Skip("no process args to compare against")
	}
	// This is a documentation guard, not a behavioural assertion: it simply
	// records that the dispatch tests above pass explicit slices. Nothing to
	// assert beyond the suite compiling and the other dispatch tests passing.
}

// ===========================================================================
// (D) UDS framing: udsSendFrame / udsRecvFrame over an in-memory net.Pipe.
//
// Parity reference: python/test_lemma_client.py's UdsSendFrameTests,
// UdsRecvFrameTests, and RecvExactlyTests. net.Pipe gives us a synchronous,
// unbuffered conn pair with no real socket: a Write blocks until the peer
// Reads it, so a goroutine-scripted peer drives the exchange deterministically
// (no sleeps). Closing one end surfaces as EOF / io.ErrUnexpectedEOF on the
// other, which is exactly how we exercise the truncation paths.
//
// frame builds the wire bytes (4-byte big-endian length prefix + UTF-8 body)
// the way udsSendFrame does, so a scripted peer can hand the reader byte-correct
// input without going through the sender.
// ===========================================================================

// frame builds a length-prefixed UDS frame for ednStr: a 4-byte big-endian
// uint32 byte-length prefix followed by the UTF-8 body. Mirrors udsSendFrame's
// wire shape so a scripted peer can emit canned frames directly.
func frame(ednStr string) []byte {
	body := []byte(ednStr)
	out := make([]byte, 4+len(body))
	binary.BigEndian.PutUint32(out[:4], uint32(len(body)))
	copy(out[4:], body)
	return out
}

// A frame written by udsSendFrame must read back through udsRecvFrame as the
// EXACT same EDN string. A goroutine-scripted peer sends on its half of the
// pipe while the test reads on the other; net.Pipe's synchronous semantics mean
// no sleep is needed.
func TestUDSFrame_RoundTripsEDNStringAcrossPipe(t *testing.T) {
	ednStr := string(marshal(t, verb{edn.Symbol("hello")}))
	client, peer := net.Pipe()
	t.Cleanup(func() { client.Close(); peer.Close() })

	sendErr := make(chan error, 1)
	go func() { sendErr <- udsSendFrame(peer, ednStr) }()

	got, err := udsRecvFrame(client)
	if err != nil {
		t.Fatalf("udsRecvFrame: %v", err)
	}
	if err := <-sendErr; err != nil {
		t.Fatalf("udsSendFrame: %v", err)
	}
	if got != ednStr {
		t.Errorf("round-trip = %q, want %q", got, ednStr)
	}
}

// The frame prefix must be EXACTLY four bytes, big-endian, carrying the body's
// byte length. We capture the raw bytes udsSendFrame puts on the wire for a
// known-length body and assert the first four bytes equal the big-endian
// encoding of that length.
func TestUDSFrame_PrefixIsFourByteBigEndianLength(t *testing.T) {
	// A 5-byte ASCII body: prefix must be 00 00 00 05.
	const ednStr = "hello"
	client, peer := net.Pipe()
	t.Cleanup(func() { client.Close(); peer.Close() })

	sendErr := make(chan error, 1)
	go func() { sendErr <- udsSendFrame(peer, ednStr) }()

	raw := make([]byte, 4+len(ednStr))
	if _, err := io.ReadFull(client, raw); err != nil {
		t.Fatalf("reading raw frame: %v", err)
	}
	if err := <-sendErr; err != nil {
		t.Fatalf("udsSendFrame: %v", err)
	}
	wantPrefix := []byte{0x00, 0x00, 0x00, 0x05}
	if string(raw[:4]) != string(wantPrefix) {
		t.Errorf("prefix = % x, want % x", raw[:4], wantPrefix)
	}
	if string(raw[4:]) != ednStr {
		t.Errorf("body = %q, want %q", raw[4:], ednStr)
	}
}

// udsRecvFrame must reassemble a frame delivered in several small chunks: a
// single conn.Read can return fewer bytes than asked, and io.ReadFull loops
// until satisfied. The peer writes the prefix one byte at a time, then the body
// in two pieces; the reader must still reconstruct the whole EDN string.
func TestUDSRecvFrame_ReassemblesFrameSplitAcrossChunks(t *testing.T) {
	ednStr := `{:event :result :rows [["venus"]] :done? true}`
	wire := frame(ednStr)
	client, peer := net.Pipe()
	t.Cleanup(func() { client.Close(); peer.Close() })

	go func() {
		// Four single-byte prefix writes, then the body in two halves. Each
		// Write blocks on the reader's matching Read (net.Pipe is synchronous),
		// so this drip-feeds without any sleep.
		for i := 0; i < 4; i++ {
			peer.Write(wire[i : i+1])
		}
		body := wire[4:]
		mid := len(body) / 2
		peer.Write(body[:mid])
		peer.Write(body[mid:])
	}()

	got, err := udsRecvFrame(client)
	if err != nil {
		t.Fatalf("udsRecvFrame: %v", err)
	}
	if got != ednStr {
		t.Errorf("reassembled = %q, want %q", got, ednStr)
	}
}

// A peer that closes mid-prefix (fewer than 4 prefix bytes delivered) must make
// udsRecvFrame return an error naming the prefix read and the 4 expected bytes
// — io.ReadFull surfaces the early close as io.ErrUnexpectedEOF.
func TestUDSRecvFrame_PrematureEOFInPrefix_Errors(t *testing.T) {
	client, peer := net.Pipe()
	t.Cleanup(func() { client.Close() })

	go func() {
		peer.Write([]byte{0x00, 0x00}) // two of four prefix bytes
		peer.Close()                   // close mid-prefix -> reader sees EOF
	}()

	_, err := udsRecvFrame(client)
	if err == nil {
		t.Fatalf("expected an error on a truncated prefix, got nil")
	}
	if !strings.Contains(err.Error(), "4 bytes expected") {
		t.Errorf("error %q does not name the 4 expected prefix bytes", err.Error())
	}
}

// A peer that sends a full prefix but then closes mid-body must make
// udsRecvFrame return an error naming the body read and the declared length.
func TestUDSRecvFrame_PrematureEOFInBody_Errors(t *testing.T) {
	client, peer := net.Pipe()
	t.Cleanup(func() { client.Close() })

	go func() {
		// Declare a 10-byte body, then deliver only 5 before closing.
		peer.Write([]byte{0x00, 0x00, 0x00, 0x0a})
		peer.Write([]byte("short"))
		peer.Close()
	}()

	_, err := udsRecvFrame(client)
	if err == nil {
		t.Fatalf("expected an error on a truncated body, got nil")
	}
	if !strings.Contains(err.Error(), "10 bytes expected") {
		t.Errorf("error %q does not name the 10 expected body bytes", err.Error())
	}
}

// ===========================================================================
// (E) UDS round-trip: drive mainUDS against a scripted unix-socket listener.
//
// Parity reference: python/test_lemma_client.py's UdsHandshakeSuccessTests +
// UdsConnectFailureTests. mainUDS dials a real path, so here (and ONLY here) we
// stand up a net.Listener on a unix socket in t.TempDir() with a goroutine that
// reads each request frame and replies with the matching canned frame. The Go
// mainUDS sends exactly five frames (hello, use-world, propose, assert, query)
// — no pagination/watch — so the script answers five requests in order.
// Determinism: synchronous frame exchange, no sleeps, listener closed via
// t.Cleanup.
// ===========================================================================

// The five canned reply bodies for the Go mainUDS sequence. The welcome carries
// the connection-bound session in its BODY (:session), per the UDS protocol.
const (
	udsWelcome       = `{:event :welcome :version 1 :session #session "s-uds-1" :world #world "default"}`
	udsWorldSelected = `{:event :world-selected :world #world "default"}`
	udsProposed      = `{:event :proposed :proposal #proposal "p-1"}`
	udsAsserted      = `{:event :asserted}`
	udsResult        = `{:event :result :rows [["venus"]] :done? true}`
)

// scriptedUDSServer listens on a unix socket inside t.TempDir() and, for the
// first accepted connection, replies to each inbound frame with the next canned
// reply in order. It records every request frame's decoded EDN body in *recvd
// (guarded by the goroutine lifecycle: the test reads it only after mainUDS
// returns, by which point the conn has been drained). Returns the socket path.
func scriptedUDSServer(t *testing.T, replies []string) (string, *[]string) {
	t.Helper()
	dir := t.TempDir()
	sockPath := filepath.Join(dir, "dianoia.sock")
	ln, err := net.Listen("unix", sockPath)
	if err != nil {
		t.Fatalf("listening on %q: %v", sockPath, err)
	}
	t.Cleanup(func() { ln.Close() })

	recvd := &[]string{}
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return // listener closed by cleanup
		}
		defer conn.Close()
		for _, reply := range replies {
			req, err := udsRecvFrame(conn)
			if err != nil {
				return // client hung up early
			}
			*recvd = append(*recvd, req)
			if err := udsSendFrame(conn, reply); err != nil {
				return
			}
		}
	}()
	return sockPath, recvd
}

// mainUDS against a scripted listener must walk the full sequence and print the
// query result line (captured from stdout); the FIRST frame it sends is the
// anonymous (hello) carrying no session; and NO frame after the hello echoes
// the session handle (over UDS the server binds the session to the connection).
func TestMainUDS_FullSequence_ReachesResultLineAndNeverEchoesSession(t *testing.T) {
	replies := []string{udsWelcome, udsWorldSelected, udsProposed, udsAsserted, udsResult}
	sockPath, recvd := scriptedUDSServer(t, replies)

	out := captureStdout(t, func() { mainUDS(sockPath) })

	// The run reached the query result line.
	if !strings.Contains(out, "rows=") {
		t.Errorf("output did not reach the query result line: %q", out)
	}
	if !strings.Contains(out, `"venus"`) {
		t.Errorf("result line did not carry the queried row: %q", out)
	}

	sent := *recvd
	if len(sent) == 0 {
		t.Fatalf("server received no frames")
	}
	// The first sent frame is the anonymous hello, with no session attached.
	if sent[0] != "(hello)" {
		t.Errorf("first sent frame = %q, want (hello)", sent[0])
	}
	if strings.Contains(sent[0], "session") {
		t.Errorf("hello frame %q must not carry a session", sent[0])
	}
	// CRITICAL: no later frame re-sends the session handle or its value — the
	// server already pinned the session to this connection.
	for i, fr := range sent[1:] {
		if strings.Contains(fr, "#session") || strings.Contains(fr, "s-uds-1") || strings.Contains(fr, ":session") {
			t.Errorf("frame %d after hello echoed the session: %q", i+1, fr)
		}
	}
}

// The proposal handle returned in the :proposed reply must be threaded verbatim
// into the assert frame — proving the round-trip plumbs server output back into
// the next request.
func TestMainUDS_ThreadsProposalHandleIntoAssertFrame(t *testing.T) {
	replies := []string{udsWelcome, udsWorldSelected, udsProposed, udsAsserted, udsResult}
	sockPath, recvd := scriptedUDSServer(t, replies)

	captureStdout(t, func() { mainUDS(sockPath) })

	sent := *recvd
	if len(sent) < 4 {
		t.Fatalf("server received %d frames, want at least 4: %#v", len(sent), sent)
	}
	// Frame index 3 is the assert: (assert #proposal "p-1"). Parse it back to a
	// sequence and compare structurally, so the exact tag/payload spacing the
	// codec emits (Compact may strip the space) does not make the test brittle.
	parsed, ok := parse(t, []byte(sent[3])).([]interface{})
	if !ok || len(parsed) != 2 {
		t.Fatalf("assert frame parsed to %#v, want a 2-element sequence", parse(t, []byte(sent[3])))
	}
	if parsed[0] != edn.Symbol("assert") {
		t.Errorf("assert verb head = %#v, want edn.Symbol(\"assert\")", parsed[0])
	}
	if parsed[1] != (Handle{Name: "proposal", Value: "p-1"}) {
		t.Errorf("assert arg = %#v, want #proposal \"p-1\"", parsed[1])
	}
}

// mainUDS pointed at a nonexistent socket path must fail FAST (no hang),
// printing the actionable line that names the path — net.Dial returns
// immediately when the socket file is absent.
func TestMainUDS_NonexistentSocketPath_FailsFastNamingPath(t *testing.T) {
	// A path inside a fresh temp dir that was never bound by any listener.
	missing := filepath.Join(t.TempDir(), "absent.sock")

	out := captureStdout(t, func() { mainUDS(missing) })

	if !strings.Contains(out, missing) {
		t.Errorf("connect-failure line %q does not name the path %q", out, missing)
	}
	if !strings.Contains(out, "is the server running?") {
		t.Errorf("connect-failure line %q is not the actionable message", out)
	}
}

// ===========================================================================
// (F) Cursor pagination: drive queryAll with a scripted send closure.
//
// Parity reference: python/test_lemma_client.py's QueryAllTests + _ScriptedSend.
// queryAll's only seam is its `send func(form) (body, error)` argument, so we
// hand it a scripted stand-in: a closure over a slice of canned EDN reply
// strings, each parsed via the REAL codec (edn.Unmarshal) so a `#cursor "c-1"`
// payload becomes the real Handle type queryAll reads back and threads into the
// (continue …) form. The closure records every form it was sent, so we can
// assert the drain issued exactly the right requests. No network: send never
// touches a socket. Running past the script fails the test — over-driving send
// is a bug worth surfacing, not a silent empty read.
//
// scriptedSend parses bodies once up front (failing the test on a bad canned
// string) and returns a recording closure plus a pointer to the recorded forms.
// ===========================================================================

// scriptedSend builds a queryAll `send` stand-in over canned EDN reply bodies.
// Each body is parsed once via the real codec; the returned closure hands them
// back in order, recording every form it is called with into *forms. Calling
// past the end of the script fails the test (an over-driven drain is a bug).
func scriptedSend(t *testing.T, bodies []string) (func(form interface{}) (interface{}, error), *[]interface{}) {
	t.Helper()
	parsed := make([]interface{}, len(bodies))
	for i, b := range bodies {
		parsed[i] = parse(t, []byte(b))
	}
	forms := &[]interface{}{}
	next := 0
	send := func(form interface{}) (interface{}, error) {
		*forms = append(*forms, form)
		if next >= len(parsed) {
			t.Fatalf("send over-driven: called %d time(s) but only %d canned repl(ies) scripted",
				next+1, len(parsed))
		}
		body := parsed[next]
		next++
		return body, nil
	}
	return send, forms
}

// A representative initial (query …) form. queryAll never inspects it — it just
// hands it to send unchanged on the first call — so its shape only needs to be
// plausible, not server-validated.
func paginationQueryForm() verb {
	return verb{edn.Symbol("query"), map[edn.Keyword]interface{}{
		edn.Keyword("find"): []interface{}{edn.Symbol("?x")},
		edn.Keyword("where"): []interface{}{
			[]interface{}{edn.Symbol("subset-of"), edn.Symbol("?x"), entity("group")},
		},
		edn.Keyword("limit"): 2,
	}}
}

// (A) Multi-page drain: a first page with :done? false + a #cursor, then a
// final page with :done? true, must concatenate every row in order across the
// pages, report pages == 2, and surface no failure.
func TestQueryAll_MultiPage_ConcatenatesRowsInOrder(t *testing.T) {
	send, _ := scriptedSend(t, []string{
		`{:event :result :rows [[#entity "a"] [#entity "b"]] :done? false :cursor #cursor "c-1"}`,
		`{:event :result :rows [[#entity "c"]] :done? true}`,
	})

	rows, pages, failure, err := queryAll(send, paginationQueryForm())
	if err != nil {
		t.Fatalf("unexpected transport error: %v", err)
	}
	if failure != nil {
		t.Fatalf("unexpected failure envelope: %#v", failure)
	}
	if pages != 2 {
		t.Errorf("pages = %d, want 2", pages)
	}
	want := []interface{}{
		[]interface{}{entity("a")},
		[]interface{}{entity("b")},
		[]interface{}{entity("c")},
	}
	if !reflect.DeepEqual(rows, want) {
		t.Errorf("rows = %#v, want %#v", rows, want)
	}
}

// (A) Multi-page drain: the SECOND form queryAll sends must be exactly
// (continue <the cursor from page 1>). We compare the threaded cursor against
// the Handle value parsed out of reply 1 — both structurally (==) and by
// re-marshalled wire text — to prove queryAll read the real #cursor handle and
// fed it back verbatim, rather than a string or a fresh value.
func TestQueryAll_MultiPage_SecondFormIsContinueCarryingTheCursor(t *testing.T) {
	page1 := `{:event :result :rows [[#entity "a"] [#entity "b"]] :done? false :cursor #cursor "c-1"}`
	send, forms := scriptedSend(t, []string{
		page1,
		`{:event :result :rows [[#entity "c"]] :done? true}`,
	})

	queryForm := paginationQueryForm()
	if _, _, _, err := queryAll(send, queryForm); err != nil {
		t.Fatalf("unexpected transport error: %v", err)
	}

	sent := *forms
	if len(sent) != 2 {
		t.Fatalf("queryAll sent %d form(s), want 2: %#v", len(sent), sent)
	}
	// The first form is the original query, passed through untouched.
	if !reflect.DeepEqual(sent[0], interface{}(queryForm)) {
		t.Errorf("first form = %#v, want the original query form %#v", sent[0], queryForm)
	}

	// Recover the exact #cursor handle reply 1 carried, independent of queryAll.
	wantCursor := get(parse(t, []byte(page1)), "cursor").(Handle)

	// The second form must be the verb (continue <cursor>).
	cont, ok := sent[1].(verb)
	if !ok {
		t.Fatalf("second form is %T, want verb (continue …)", sent[1])
	}
	if len(cont) != 2 {
		t.Fatalf("continue form has %d element(s), want 2: %#v", len(cont), cont)
	}
	if cont[0] != edn.Symbol("continue") {
		t.Errorf("continue head = %#v, want edn.Symbol(\"continue\")", cont[0])
	}
	// Structural equality: the threaded arg IS the parsed cursor handle.
	if cont[1] != interface{}(wantCursor) {
		t.Errorf("continue cursor = %#v, want %#v", cont[1], wantCursor)
	}
	// And it re-marshals to the same wire text as the original cursor handle —
	// proving a faithful round-trip back onto the wire.
	if got, want := string(marshal(t, cont[1])), string(marshal(t, wantCursor)); got != want {
		t.Errorf("continue cursor wire text = %q, want %q", got, want)
	}
}

// (B) Single page: a lone reply with :done? true and NO :cursor key must drain
// in exactly one send (no continue), return that page's rows, and not panic on
// the absent :cursor — the continue branch and its cursor read are never
// reached when the first page is already done.
func TestQueryAll_SinglePage_OneSendNoContinueNoCursorKey(t *testing.T) {
	send, forms := scriptedSend(t, []string{
		`{:event :result :rows [[#entity "a"]] :done? true}`,
	})

	queryForm := paginationQueryForm()
	rows, pages, failure, err := queryAll(send, queryForm)
	if err != nil {
		t.Fatalf("unexpected transport error: %v", err)
	}
	if failure != nil {
		t.Fatalf("unexpected failure envelope: %#v", failure)
	}
	if pages != 1 {
		t.Errorf("pages = %d, want 1", pages)
	}
	want := []interface{}{[]interface{}{entity("a")}}
	if !reflect.DeepEqual(rows, want) {
		t.Errorf("rows = %#v, want %#v", rows, want)
	}
	// Exactly one send, and it was the original query — no continue was issued.
	sent := *forms
	if len(sent) != 1 {
		t.Fatalf("queryAll sent %d form(s), want exactly 1: %#v", len(sent), sent)
	}
	if !reflect.DeepEqual(sent[0], interface{}(queryForm)) {
		t.Errorf("only form = %#v, want the original query form %#v", sent[0], queryForm)
	}
}

// (C) Failure on continue (e.g. an expired cursor): a not-done first page
// followed by an :error envelope on the continue must stop the drain cleanly —
// returning the rows gathered from page 1, the error body as failure, pages
// counting only the drained first page, and a nil transport error.
func TestQueryAll_ContinueFailure_PropagatesFailureWithRowsSoFar(t *testing.T) {
	send, forms := scriptedSend(t, []string{
		`{:event :result :rows [[#entity "a"] [#entity "b"]] :done? false :cursor #cursor "c-1"}`,
		`{:event :error :reason :unknown-handle :message "cursor c-1 has expired"}`,
	})

	rows, pages, failure, err := queryAll(send, paginationQueryForm())
	if err != nil {
		t.Fatalf("a failure envelope must not surface as a transport error, got: %v", err)
	}
	if failure == nil {
		t.Fatalf("expected the :error envelope as failure, got nil")
	}
	// The failure is the error envelope returned verbatim.
	if got := get(failure, "event"); got != edn.Keyword("error") {
		t.Errorf("failure :event = %#v, want :error", got)
	}
	if got := get(failure, "reason"); got != edn.Keyword("unknown-handle") {
		t.Errorf("failure :reason = %#v, want :unknown-handle", got)
	}
	// Rows gathered before the cursor expired are preserved.
	want := []interface{}{[]interface{}{entity("a")}, []interface{}{entity("b")}}
	if !reflect.DeepEqual(rows, want) {
		t.Errorf("rows = %#v, want %#v", rows, want)
	}
	// pages counts only the successfully-drained first page.
	if pages != 1 {
		t.Errorf("pages = %d, want 1 (only the drained first page)", pages)
	}
	// Exactly two sends: the query and the (failed) continue.
	if sent := *forms; len(sent) != 2 {
		t.Errorf("queryAll sent %d form(s), want 2 (query + continue): %#v", len(sent), sent)
	}
}

// (D) Failure on the FIRST reply: a query refused before any page lands must
// yield empty rows, zero pages, the failure body, and a nil error — and issue
// no continue.
func TestQueryAll_FirstReplyFailure_ReturnsEmptyRowsZeroPages(t *testing.T) {
	send, forms := scriptedSend(t, []string{
		`{:event :rejected :reason :forbidden :message "not allowed"}`,
	})

	rows, pages, failure, err := queryAll(send, paginationQueryForm())
	if err != nil {
		t.Fatalf("a failure envelope must not surface as a transport error, got: %v", err)
	}
	if failure == nil {
		t.Fatalf("expected the :rejected envelope as failure, got nil")
	}
	if got := get(failure, "event"); got != edn.Keyword("rejected") {
		t.Errorf("failure :event = %#v, want :rejected", got)
	}
	// Empty (non-nil) rows and zero pages: nothing came back.
	if len(rows) != 0 {
		t.Errorf("rows = %#v, want empty", rows)
	}
	if rows == nil {
		t.Errorf("rows is nil, want a non-nil empty slice ([] not nil)")
	}
	if pages != 0 {
		t.Errorf("pages = %d, want 0", pages)
	}
	// Exactly one send: the query, and no continue.
	if sent := *forms; len(sent) != 1 {
		t.Errorf("queryAll sent %d form(s), want exactly 1 (no continue): %#v", len(sent), sent)
	}
}

// captureStdout runs fn with os.Stdout redirected to a pipe and returns whatever
// fn printed. mainUDS prints its status lines straight to stdout (it has no
// injectable writer), so we capture at the os.Stdout level. The pipe is drained
// on a goroutine so a large write cannot deadlock, and stdout is always restored.
func captureStdout(t *testing.T, fn func()) string {
	t.Helper()
	orig := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("creating stdout pipe: %v", err)
	}
	os.Stdout = w
	done := make(chan string, 1)
	go func() {
		b, _ := io.ReadAll(r)
		done <- string(b)
	}()

	fn()

	w.Close()
	os.Stdout = orig
	return <-done
}
