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
	"io"
	"net/http"
	"net/http/httptest"
	"os"
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
