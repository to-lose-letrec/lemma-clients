// lemma_client.go — a single-file Go client for the Lemma wire protocol.
//
// This file is a *recipe*, not a library: read it end to end. The first thing
// any Lemma client needs is a way to turn Go values into the EDN text the
// server speaks, and to turn the server's EDN responses back into Go values.
// Rather than hand-roll that codec, this client leans on go-edn
// (olympos.io/encoding/edn — the one third-party dependency); everything else
// is the standard library. On top of the codec sit an HTTP transport, a UDS
// transport, and a runnable main() that walks the propose/assert/query
// round-trip over either.
//
// EDN in a nutshell
// -----------------
// EDN (Extensible Data Notation) is Clojure's data syntax. Lemma uses a small,
// well-defined subset of it. The pieces we care about and their go-edn mappings:
//
//	nil true false              -- nil / true / false
//	42  -3  3.14                 -- int64 / float64
//	"a string\n"                -- string
//	:event  :verbs/core         -- edn.Keyword
//	equivalent  member-of  ?o   -- edn.Symbol (?-vars are symbols)
//	( a b c )                   -- an EDN LIST   — decoded to []interface{}
//	[ a b c ]                   -- an EDN VECTOR — decoded to []interface{}
//	{ k v, k v }                -- an EDN MAP    — decoded to map[interface{}]interface{}
//	#{ a b c }                  -- an EDN SET
//	#tag payload                -- a tagged literal (an edn.Tag, or a custom type)
//
// Lists versus vectors — the one design decision
// ----------------------------------------------
// EDN distinguishes lists `( … )` from vectors `[ … ]`, and Lemma relies on the
// distinction (grammar §3): a *list* appears ONLY as the top-level verb form —
// `(propose …)`, `(query …)`, `(hello)`. Everywhere inside the arguments,
// collections are *vectors* (`:find [?x]`, `:where [[…]]`), maps, or sets —
// never lists.
//
// go-edn encodes the distinction in its type system, but ASYMMETRICALLY: a Go
// slice marshals to a VECTOR by default, and the only built-in way to ask for a
// list is the `edn:",list"` struct-field tag — which necessarily nests the list
// inside a map (`{:field (…)}`), so it cannot emit a *bare* top-level list. We
// therefore carry the verb form in a small `verb` type whose `MarshalEDN`
// renders `( … )` directly (proven by round-trip below). Argument slices stay
// plain `[]interface{}`, so they marshal as vectors — exactly the grammar's
// split. On the read side go-edn decodes both lists and vectors to
// `[]interface{}`, so a verb form and a vector are indistinguishable inbound;
// that is fine, because the client only ever *emits* verb forms, never parses
// one back.
//
// Tagged literals
// ---------------
// The ten core Lemma tags are `#entity #world #proposal #tx #ref #cursor #watch
// #session #fact #violation` (grammar §5). We make each round-trip in BOTH
// directions: marshalling re-emits the exact `#tag payload` wire text, and
// parsing reconstructs a typed value. The eight string-payload tags share one
// Handle type (tag name + string value); #fact / #violation carry a map and get
// their own types. A reader is registered for each via edn.AddTagFn, so a tag in
// a response parses straight into the matching typed value. Tags we do NOT
// register (e.g. #inst) still parse — go-edn falls back to a generic edn.Tag —
// so an unexpected tag never breaks a response.
//
// Importing this file performs no network I/O — only main / dispatch (and the
// main() guard) touch the network.
package main

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"

	"olympos.io/encoding/edn"
)

// ---------------------------------------------------------------------------
// Tagged literals:  #tag payload  <->  typed value
//
// A round-tripping tag type needs two halves: a MarshalEDN that renders the full
// `#tag payload` wire text (the encode side), and a reader registered with
// edn.AddTagFn that rebuilds the typed value from the parsed payload (the decode
// side). We split the ten core tags by payload kind — string handles versus
// map-bearing #fact / #violation — and register a reader for each.
//
// go-edn's marshaller routes any value implementing edn.Marshaler through its
// MarshalEDN method and then runs the result through Compact, which validates it
// as EDN. So a hand-built `#entity "alice"` string is checked, not trusted
// blindly. Compact happens to strip the space between tag and payload
// (`#entity"alice"`); that is wire-valid — clojure .edn ignores whitespace
// between a tag and its payload — and parses back cleanly either way.
// ---------------------------------------------------------------------------

// The eight tags whose payload is a single string (grammar §5, §5.3).
var stringTags = []string{
	"entity", "world", "proposal", "tx", "ref", "cursor", "watch", "session",
}

// Handle is a string-payload tag such as `#entity "alice"` or `#world "default"`.
// Name is the tag without the leading `#`; Value is the string payload. The one
// type backs all eight string-payload tags — which tag a given instance
// represents is just its Name.
type Handle struct {
	Name  string
	Value string
}

// MarshalEDN renders the handle as `#<name> "<value>"`. We marshal Value through
// go-edn (rather than fmt-quoting it ourselves) so string escaping matches the
// codec exactly.
func (h Handle) MarshalEDN() ([]byte, error) {
	val, err := edn.Marshal(h.Value)
	if err != nil {
		return nil, err
	}
	return []byte(fmt.Sprintf("#%s %s", h.Name, val)), nil
}

// Fact is the `#fact {…}` tag (grammar §5.1) — a map payload carrying some
// combination of :predicate / :subject / :object / :args. We keep the map
// verbatim and let the server enforce the legal shapes.
type Fact struct {
	Value map[edn.Keyword]interface{}
}

// MarshalEDN renders the fact as `#fact {…}` with the payload map marshalled by
// go-edn.
func (f Fact) MarshalEDN() ([]byte, error) {
	val, err := edn.Marshal(f.Value)
	if err != nil {
		return nil, err
	}
	return append([]byte("#fact "), val...), nil
}

// Violation is the `#violation {…}` tag (grammar §5.2) — a server-emitted map
// payload. Registered so a violation in a response parses into a typed value
// that round-trips cleanly back onto the wire if an agent feeds it into a later
// query or argument position.
type Violation struct {
	Value map[edn.Keyword]interface{}
}

// MarshalEDN renders the violation as `#violation {…}`.
func (v Violation) MarshalEDN() ([]byte, error) {
	val, err := edn.Marshal(v.Value)
	if err != nil {
		return nil, err
	}
	return append([]byte("#violation "), val...), nil
}

// init registers a decode reader for every core tag, so a `#tag payload` in a
// response parses into the matching typed value above. The string-payload tags
// share one factory closure (binding the tag name per iteration); #fact and
// #violation each take a map payload. A reader's input type is what the payload
// parses to — a string for handles, a keyword-keyed map for #fact / #violation —
// and go-edn matches the registered function's single argument against it.
func init() {
	for _, tag := range stringTags {
		name := tag // bind per iteration so each reader keeps its own tag name
		edn.MustAddTagFn(name, func(s string) (Handle, error) {
			return Handle{Name: name, Value: s}, nil
		})
	}
	edn.MustAddTagFn("fact", func(m map[edn.Keyword]interface{}) (Fact, error) {
		return Fact{Value: m}, nil
	})
	edn.MustAddTagFn("violation", func(m map[edn.Keyword]interface{}) (Violation, error) {
		return Violation{Value: m}, nil
	})
}

// ---------------------------------------------------------------------------
// The verb form:  a bare top-level EDN list  ( verb arg … )
//
// This is the list-vs-vector vehicle. go-edn cannot emit a bare top-level list
// from a plain Go value (its `,list` struct tag only works on a map field), so
// we carry the verb form in this type and render the list by hand in MarshalEDN:
// each element is marshalled by go-edn and the results are space-joined inside
// `( … )`. The elements themselves go through the normal codec, so a Handle
// stays a `#tag`, a `[]interface{}` argument stays a VECTOR, and a map stays a
// MAP — only the outermost wrapper becomes a list. Round-trip proven before this
// file was built: `verb{Symbol("use-world"), world("default")}` marshals to
// `(use-world #world "default")`.
// ---------------------------------------------------------------------------

type verb []interface{}

// MarshalEDN renders the verb form as `( e0 e1 … )`, marshalling each element
// through go-edn so nested handles, vectors, and maps encode normally.
func (vb verb) MarshalEDN() ([]byte, error) {
	var buf bytes.Buffer
	buf.WriteByte('(')
	for i, el := range vb {
		if i > 0 {
			buf.WriteByte(' ')
		}
		b, err := edn.Marshal(el)
		if err != nil {
			return nil, err
		}
		buf.Write(b)
	}
	buf.WriteByte(')')
	return buf.Bytes(), nil
}

// ---------------------------------------------------------------------------
// Constructor helpers
//
// Thin wrappers so the round-trip code below reads as prose rather than as a
// wall of tag-value constructions. They mirror the grammar's payload shapes.
// ---------------------------------------------------------------------------

// entity builds an `#entity "<name>"` handle (grammar §5.3).
func entity(name string) Handle {
	return Handle{Name: "entity", Value: name}
}

// world builds a `#world "<name>"` handle (grammar §5).
func world(name string) Handle {
	return Handle{Name: "world", Value: name}
}

// fact builds a `#fact {…}` binary fact: `(predicate subject object)`. predicate
// is a Symbol; subject / object are typically #entity handles. The keys are the
// grammar's reserved fact keys.
func fact(predicate edn.Symbol, subject, object interface{}) Fact {
	return Fact{Value: map[edn.Keyword]interface{}{
		edn.Keyword("predicate"): predicate,
		edn.Keyword("subject"):   subject,
		edn.Keyword("object"):    object,
	}}
}

// ---------------------------------------------------------------------------
// HTTP transport:  EDN form  ->  POST  ->  parsed EDN response
//
// With the codec in hand, talking to a Lemma server is just "encode, POST,
// decode". A flat stdlib-only helper, not an abstraction. The session protocol
// (SPEC §3) is:
//
//   - The first call is anonymous: POST /v1/messages with (hello). The :welcome
//     response carries the new session id in the X-Lemma-Session response header.
//   - Subsequent calls reuse that id, either on the named endpoint
//     POST /v1/sessions/{id}/messages or simply by echoing it back in the
//     x-lemma-session request header. We do both here: the named endpoint plus
//     the header.
//
// This helper handles one round-trip; the caller threads the returned session id
// into the next call.
// ---------------------------------------------------------------------------

// DefaultBase is where a locally booted Dianoia HTTP listener lives by default
// (see the protocol examples). Override per call via the base argument.
const DefaultBase = "http://127.0.0.1:8080"

// postEDN POSTs an EDN form to base+path and returns (parsed body, session id).
//
// form is any value edn.Marshal accepts — typically a verb form such as
// verb{edn.Symbol("hello")}. It is encoded to EDN text and sent as
// application/edn UTF-8 bytes; the response body is parsed back into Go values
// with edn.Unmarshal (so the §5 handles round-trip into the typed values above).
// When session is non-empty it is echoed in the x-lemma-session request header
// so the server attaches the call to an existing session. The returned session
// id is the X-Lemma-Session response header value, or "" if absent.
//
// An HTTP error status (4xx/5xx) still carries a valid Lemma EDN *error
// envelope* in its body, so we parse and return that as the body rather than
// discarding it — the caller inspects :event to tell a welcome from an error. A
// connection-level failure (server down, refused) is the one transport error:
// it returns a non-nil error that names the base URL so the failure is
// actionable rather than a bare errno.
func postEDN(path string, form interface{}, session, base string) (interface{}, string, error) {
	payload, err := edn.Marshal(form)
	if err != nil {
		return nil, "", fmt.Errorf("encoding EDN request: %w", err)
	}

	req, err := http.NewRequest(http.MethodPost, base+path, bytes.NewReader(payload))
	if err != nil {
		return nil, "", fmt.Errorf("building request: %w", err)
	}
	req.Header.Set("Content-Type", "application/edn")
	if session != "" {
		req.Header.Set("x-lemma-session", session)
	}

	rsp, err := http.DefaultClient.Do(req)
	if err != nil {
		// No HTTP response at all — we never reached a Lemma server. Name the
		// base so the failure points at what to fix. (A non-2xx status is NOT
		// this case: net/http returns it as a normal response, handled below.)
		return nil, "", fmt.Errorf(
			"could not reach the Lemma server at %q (%v); is the server running?",
			base, err)
	}
	defer rsp.Body.Close()

	raw, err := io.ReadAll(rsp.Body)
	if err != nil {
		return nil, "", fmt.Errorf("reading response body: %w", err)
	}

	// Whether the status was 2xx or 4xx/5xx, the body is a structured Lemma
	// reply: a :welcome / :ok / … envelope on success, an :error / :rejected
	// envelope on refusal. Parse it the same way either way; the caller reads
	// :event to tell them apart.
	var body interface{}
	if err := edn.Unmarshal(raw, &body); err != nil {
		return nil, "", fmt.Errorf("parsing EDN response %q: %w", raw, err)
	}
	return body, rsp.Header.Get("X-Lemma-Session"), nil
}

// ---------------------------------------------------------------------------
// Envelope inspection
//
// Every Lemma reply is a map keyed by :event. Two values mean refusal: :error
// (malformed / illegal) and :rejected (well-formed but disallowed, e.g. a
// consistency violation). Parsed maps are map[interface{}]interface{} with
// edn.Keyword keys and (for keyword fields like :event) edn.Keyword values, so
// the helpers below look keys up by edn.Keyword and compare values against it.
// ---------------------------------------------------------------------------

// asMap returns body as a parsed EDN map, or (nil, false) if it is not one.
// go-edn decodes EDN maps to map[interface{}]interface{}; pulling that out once
// keeps the lookups below readable.
func asMap(body interface{}) (map[interface{}]interface{}, bool) {
	m, ok := body.(map[interface{}]interface{})
	return m, ok
}

// isFailure reports whether body is an :error or :rejected envelope.
func isFailure(body interface{}) bool {
	m, ok := asMap(body)
	if !ok {
		return false
	}
	event := m[edn.Keyword("event")]
	return event == edn.Keyword("error") || event == edn.Keyword("rejected")
}

// describeFailure formats the salient parts of an error/rejection envelope for
// printing. It pulls whichever of :reason / :message / :violations the server
// included — the fields that explain *why* a call was refused — and renders each
// through the codec so keywords, strings, and #violation handles print as they
// appear on the wire.
func describeFailure(body interface{}) string {
	m, ok := asMap(body)
	if !ok {
		return "(no detail provided)"
	}
	var parts []string
	for _, key := range []string{"reason", "message", "violations"} {
		if val, present := m[edn.Keyword(key)]; present && val != nil {
			rendered, err := edn.Marshal(val)
			if err != nil {
				continue
			}
			parts = append(parts, fmt.Sprintf(":%s %s", key, rendered))
		}
	}
	if len(parts) == 0 {
		return "(no detail provided)"
	}
	out := parts[0]
	for _, p := range parts[1:] {
		out += "; " + p
	}
	return out
}

// render is a small printing helper: marshal a parsed value back to EDN text so
// the round-trip's status lines show wire-faithful values (a Handle prints as
// `#proposal "p-1"`, a keyword as `:welcome`). On the off chance a value cannot
// be marshalled, we fall back to Go's default formatting rather than fail a
// status line.
func render(v interface{}) string {
	b, err := edn.Marshal(v)
	if err != nil {
		return fmt.Sprintf("%v", v)
	}
	return string(b)
}

// get looks a keyword field up in a parsed reply map, returning nil if body is
// not a map or the key is absent.
func get(body interface{}, key string) interface{} {
	m, ok := asMap(body)
	if !ok {
		return nil
	}
	return m[edn.Keyword(key)]
}

// ---------------------------------------------------------------------------
// Runnable recipe:  the full Lemma round-trip
//
// A flat, linear retelling of the protocol's hello → use-world → propose →
// assert → query sequence. Each step prints one human-readable line so a reader
// can follow the wire conversation by running the file. After every reply we
// check :event; an :error / :rejected is printed (via describeFailure) and the
// sequence returns cleanly rather than panicking. A connection-level failure is
// caught up front at the hello and reported the same way.
// ---------------------------------------------------------------------------

// mainRun walks the propose/assert/query round-trip against a Lemma server over
// HTTP. base is the server's base URL (e.g. http://127.0.0.1:8080).
func mainRun(base string) {
	// 1. Anonymous hello. The welcome reply carries the new session id in the
	//    X-Lemma-Session response header, which postEDN surfaces for us.
	body, sid, err := postEDN("/v1/messages", verb{edn.Symbol("hello")}, "", base)
	if err != nil {
		// Connection-level failure: postEDN already named the base URL. Print
		// the actionable line and stop — there is nothing more to attempt.
		fmt.Println(err)
		return
	}
	if get(body, "event") != edn.Keyword("welcome") {
		fmt.Printf("hello: expected :welcome, got %s -- %s\n",
			render(get(body, "event")), describeFailure(body))
		return
	}
	fmt.Printf("hello -> :welcome  version=%s  session=%s  world=%s\n",
		render(get(body, "version")), sid, render(get(body, "world")))

	// 2. Every later call rides the same session, on the named endpoint, with
	//    the session id echoed in the request header.
	named := func(form interface{}) (interface{}, error) {
		b, _, err := postEDN("/v1/sessions/"+sid+"/messages", form, sid, base)
		return b, err
	}

	// 3. Enter the world. (use-world #world "default")
	body, err = named(verb{edn.Symbol("use-world"), world("default")})
	if err != nil {
		fmt.Println(err)
		return
	}
	if isFailure(body) {
		fmt.Printf("use-world refused: %s\n", describeFailure(body))
		return
	}
	fmt.Printf("use-world \"default\" -> %s  world=%s\n",
		render(get(body, "event")), render(get(body, "world")))

	// 4. Propose a fact: morningstar is equivalent to venus. The reply hands
	//    back a #proposal handle we feed straight into the assert.
	f := fact(edn.Symbol("equivalent"), entity("morningstar"), entity("venus"))
	body, err = named(verb{edn.Symbol("propose"), f})
	if err != nil {
		fmt.Println(err)
		return
	}
	if isFailure(body) {
		fmt.Printf("propose refused: %s\n", describeFailure(body))
		return
	}
	proposal := get(body, "proposal")
	fmt.Printf("propose (equivalent morningstar venus) -> %s  proposal=%s\n",
		render(get(body, "event")), render(proposal))

	// 5. Assert the proposed fact into the world.
	body, err = named(verb{edn.Symbol("assert"), proposal})
	if err != nil {
		fmt.Println(err)
		return
	}
	if isFailure(body) {
		fmt.Printf("assert refused: %s\n", describeFailure(body))
		return
	}
	fmt.Printf("assert proposal -> %s\n", render(get(body, "event")))

	// 6. Query it back. Note :find / :where are VECTORS ([]interface{}) and the
	//    where-clause is a vector of vectors; only the verb head is a list, and
	//    the query variable ?o stays a Symbol.
	body, err = named(verb{edn.Symbol("query"), map[edn.Keyword]interface{}{
		edn.Keyword("find"): []interface{}{edn.Symbol("?o")},
		edn.Keyword("where"): []interface{}{
			[]interface{}{edn.Symbol("equivalent"), entity("morningstar"), edn.Symbol("?o")},
		},
	}})
	if err != nil {
		fmt.Println(err)
		return
	}
	if isFailure(body) {
		fmt.Printf("query refused: %s\n", describeFailure(body))
		return
	}
	fmt.Printf("query (equivalent morningstar ?o) -> rows=%s  done?=%s\n",
		render(get(body, "rows")), render(get(body, "done?")))
}

// ---------------------------------------------------------------------------
// UDS transport:  EDN form  ->  length-prefixed frame  ->  parsed EDN response
//
// A second transport that speaks the same EDN codec over a Unix domain socket
// instead of HTTP. It sits alongside postEDN rather than replacing it — same
// "encode, send, decode" shape, different plumbing. Two things differ from HTTP:
//
//   - Framing. There is no HTTP envelope, so each message is delimited
//     explicitly: a 4-byte big-endian UNSIGNED length prefix followed by that
//     many UTF-8 bytes of EDN. This matches Dianoia's transport/uds.clj
//     write-frame / read-frame exactly (DataOutputStream.writeInt is a 4-byte
//     big-endian int).
//   - Session binding. Over HTTP the client threads the session id back into
//     each request header. Over UDS the server binds the session to the
//     *connection*: it captures the id from the welcome envelope and attaches
//     it to the socket (see uds.clj handle-frame / build-ctx). So the client
//     must NOT echo the session id into later frames — it just keeps sending on
//     the same socket, and the server already knows who it is.
//
// Stdlib only for the plumbing: net for the connection, encoding/binary for the
// length prefix; the EDN codec is the same go-edn used everywhere above.
// ---------------------------------------------------------------------------

// DefaultSocket is where a locally booted Dianoia UDS listener binds by default
// (see uds.clj start! :socket-path). Override per call via the socketPath
// argument to mainUDS.
const DefaultSocket = "/tmp/dianoia.sock"

// udsSendFrame frames ednStr and writes it: a 4-byte big-endian length prefix,
// then the body. The body is the UTF-8 encoding of ednStr (Go strings are
// already UTF-8); the prefix is its byte length as a big-endian uint32.
// binary.Write emits the four prefix bytes, and conn.Write on a stream socket
// keeps writing until every byte is accepted, so a single Write of the body
// suffices. Mirrors uds.clj write-frame.
func udsSendFrame(conn net.Conn, ednStr string) error {
	body := []byte(ednStr)
	if err := binary.Write(conn, binary.BigEndian, uint32(len(body))); err != nil {
		return fmt.Errorf("writing frame length prefix: %w", err)
	}
	if _, err := conn.Write(body); err != nil {
		return fmt.Errorf("writing frame body: %w", err)
	}
	return nil
}

// udsRecvFrame reads one length-prefixed frame and returns its body as a string.
// The inverse of udsSendFrame: read the 4-byte big-endian length, then read
// exactly that many body bytes, then interpret them as UTF-8 (a Go string holds
// the bytes verbatim). io.ReadFull is the loop-until-satisfied read — a single
// conn.Read may return fewer bytes than asked, and a peer that closes mid-frame
// surfaces as io.ErrUnexpectedEOF. We name how many bytes were still expected
// (prefix vs. body) so a truncated frame is actionable, mirroring python's
// _recv_exactly message shape. Mirrors uds.clj read-frame.
func udsRecvFrame(conn net.Conn) (string, error) {
	var prefix [4]byte
	if _, err := io.ReadFull(conn, prefix[:]); err != nil {
		// A clean io.EOF here means the peer closed before any of the next
		// frame arrived; io.ErrUnexpectedEOF means it closed mid-prefix.
		return "", fmt.Errorf(
			"reading frame length prefix (4 bytes expected): %w", err)
	}
	length := binary.BigEndian.Uint32(prefix[:])
	body := make([]byte, length)
	if _, err := io.ReadFull(conn, body); err != nil {
		return "", fmt.Errorf(
			"reading frame body (%d bytes expected): %w", length, err)
	}
	return string(body), nil
}

// ---------------------------------------------------------------------------
// Runnable recipe:  the full Lemma round-trip, over UDS
//
// Step for step this is mainRun — hello → use-world → propose → assert → query,
// one printed line per step, stopping cleanly on a failure envelope — but spoken
// over a UDS frame stream. The one protocol difference is session handling: the
// server binds the session to the connection from the welcome envelope (uds.clj
// handle-frame), so we do NOT thread the session id into later frames, and the
// :welcome carries the session in its BODY (:session) rather than in a header as
// HTTP does. Every call after hello simply rides the same open socket.
// ---------------------------------------------------------------------------

// mainUDS walks the propose/assert/query round-trip against a Lemma server over
// a Unix domain socket. socketPath is the listener's path (e.g.
// /tmp/dianoia.sock).
func mainUDS(socketPath string) {
	// Dial the socket. A failure here — the socket file is missing, or nothing
	// is accepting on it — is the one transport error we name up front, pointing
	// at the path so the failure is actionable rather than a bare errno.
	conn, err := net.Dial("unix", socketPath)
	if err != nil {
		fmt.Printf(
			"could not connect to the Lemma UDS server at %q (%v); is the server running?\n",
			socketPath, err)
		return
	}
	// The socket is always closed when we leave, whatever the round-trip does;
	// closing also lets the server's reader observe EOF and drop the session.
	defer conn.Close()

	// One round-trip: frame out, frame in, parse. The session lives on the
	// connection — no id is echoed back, unlike the HTTP transport. A transport
	// error (write/read/parse) is returned so the caller can stop cleanly.
	call := func(form interface{}) (interface{}, error) {
		payload, err := edn.Marshal(form)
		if err != nil {
			return nil, fmt.Errorf("encoding EDN request: %w", err)
		}
		if err := udsSendFrame(conn, string(payload)); err != nil {
			return nil, err
		}
		raw, err := udsRecvFrame(conn)
		if err != nil {
			return nil, err
		}
		var body interface{}
		if err := edn.Unmarshal([]byte(raw), &body); err != nil {
			return nil, fmt.Errorf("parsing EDN response %q: %w", raw, err)
		}
		return body, nil
	}

	// 1. Anonymous hello. The welcome reply carries the session id, which the
	//    server has already pinned to this connection for us — so unlike HTTP we
	//    read it from the body (:session), not a header.
	body, err := call(verb{edn.Symbol("hello")})
	if err != nil {
		fmt.Println(err)
		return
	}
	if get(body, "event") != edn.Keyword("welcome") {
		fmt.Printf("hello: expected :welcome, got %s -- %s\n",
			render(get(body, "event")), describeFailure(body))
		return
	}
	fmt.Printf("hello -> :welcome  version=%s  session=%s  world=%s\n",
		render(get(body, "version")), render(get(body, "session")),
		render(get(body, "world")))

	// 2. Enter the world. (use-world #world "default")
	body, err = call(verb{edn.Symbol("use-world"), world("default")})
	if err != nil {
		fmt.Println(err)
		return
	}
	if isFailure(body) {
		fmt.Printf("use-world refused: %s\n", describeFailure(body))
		return
	}
	fmt.Printf("use-world \"default\" -> %s  world=%s\n",
		render(get(body, "event")), render(get(body, "world")))

	// 3. Propose a fact: morningstar is equivalent to venus. The reply hands
	//    back a #proposal handle we feed straight into the assert.
	f := fact(edn.Symbol("equivalent"), entity("morningstar"), entity("venus"))
	body, err = call(verb{edn.Symbol("propose"), f})
	if err != nil {
		fmt.Println(err)
		return
	}
	if isFailure(body) {
		fmt.Printf("propose refused: %s\n", describeFailure(body))
		return
	}
	proposal := get(body, "proposal")
	fmt.Printf("propose (equivalent morningstar venus) -> %s  proposal=%s\n",
		render(get(body, "event")), render(proposal))

	// 4. Assert the proposed fact into the world.
	body, err = call(verb{edn.Symbol("assert"), proposal})
	if err != nil {
		fmt.Println(err)
		return
	}
	if isFailure(body) {
		fmt.Printf("assert refused: %s\n", describeFailure(body))
		return
	}
	fmt.Printf("assert proposal -> %s\n", render(get(body, "event")))

	// 5. Query it back. As in the HTTP path, :find / :where are VECTORS
	//    ([]interface{}) and the where-clause is a vector of vectors; only the
	//    verb head is a list, and the query variable ?o stays a Symbol.
	body, err = call(verb{edn.Symbol("query"), map[edn.Keyword]interface{}{
		edn.Keyword("find"): []interface{}{edn.Symbol("?o")},
		edn.Keyword("where"): []interface{}{
			[]interface{}{edn.Symbol("equivalent"), entity("morningstar"), edn.Symbol("?o")},
		},
	}})
	if err != nil {
		fmt.Println(err)
		return
	}
	if isFailure(body) {
		fmt.Printf("query refused: %s\n", describeFailure(body))
		return
	}
	fmt.Printf("query (equivalent morningstar ?o) -> rows=%s  done?=%s\n",
		render(get(body, "rows")), render(get(body, "done?")))
}

// ---------------------------------------------------------------------------
// Dispatcher
//
// dispatch routes the CLI args to a transport. No args runs the HTTP round-trip
// against DefaultBase. A leading "uds" selects the Unix-domain-socket transport,
// taking an optional second argument as the socket path (else DefaultSocket).
// Any other leading argument is an HTTP base URL.
// ---------------------------------------------------------------------------

func dispatch(args []string) {
	switch {
	case len(args) > 0 && args[0] == "uds":
		if len(args) > 1 {
			mainUDS(args[1])
		} else {
			mainUDS(DefaultSocket)
		}
	case len(args) > 0:
		mainRun(args[0])
	default:
		mainRun(DefaultBase)
	}
}

func main() {
	// No network happens at import time — only here.
	dispatch(os.Args[1:])
}
