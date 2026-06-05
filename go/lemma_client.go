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
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

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
// Capabilities & limits:  the :welcome surface  ->  ServerInfo
//
// Every session opens with a (hello) whose :welcome reply advertises what the
// server can do (SPEC §10): a :capabilities set of namespaced flag keywords, a
// :limits map of resource caps, and the :verbs / :predicates the world exposes.
// A well-behaved client reads this once and tailors itself to it — skipping
// features the server doesn't advertise and staying under the byte caps it
// enforces. ServerInfo is the parsed, queryable form of that surface; it is a
// plain data record (not a new abstraction layer), so the round-trip code below
// can ask "does this server paginate?" or "is my message small enough?" in one
// readable call.
//
// go-edn decoding shapes (probed before this was built): an EDN set `#{…}`
// decodes to map[interface{}]bool keyed by the decoded elements, so
// :capabilities arrives as map[interface{}]bool with edn.Keyword keys and a
// :core / :extensions surface arrives as map[interface{}]bool with edn.Symbol
// keys. We narrow those generic maps into typed sets so the record below reads
// as data rather than as a pile of interface{} assertions.
// ---------------------------------------------------------------------------

// ServerInfo is the parsed :welcome surface: what this server advertises
// (SPEC §10). Fields mirror the welcome map. capabilities is a set of Keyword
// flags (e.g. lemma/cursor-pagination); limits is a Keyword -> value map of
// resource caps; verbs and predicates are flat sets of Symbol names with the
// :core and :extensions surfaces merged. Every field has a zero-value-safe
// default: a minimal welcome that omits a section yields an empty (non-nil)
// set or map, so the lookups below never panic on a nil map.
type ServerInfo struct {
	Version      interface{}
	Capabilities map[edn.Keyword]bool
	Limits       map[edn.Keyword]interface{}
	Verbs        map[edn.Symbol]bool
	Predicates   map[edn.Symbol]bool
}

// supports reports whether capability (a Keyword) is in the advertised set.
func (si ServerInfo) supports(capability edn.Keyword) bool {
	return si.Capabilities[capability]
}

// maxMessageBytes returns the :max-message-bytes limit and true, or (0, false)
// when the server advertised no such limit. The (int64, bool) shape is Go's
// idiomatic "absent marker": go-edn decodes EDN ints to int64, so a present
// limit asserts cleanly, and an unadvertised one is distinguishable from a real
// zero. withinMessageLimit reads the bool to mean "unlimited".
func (si ServerInfo) maxMessageBytes() (int64, bool) {
	v, present := si.Limits[edn.Keyword("max-message-bytes")]
	if !present {
		return 0, false
	}
	n, ok := v.(int64)
	return n, ok
}

// flattenSurface merges a {:core #{…} :extensions {pack #{…}}} map into one flat
// set of Symbol names. The :verbs and :predicates entries of a welcome split
// names into a :core set plus per-pack :extensions sets (SPEC §10); a client
// mostly just wants "every name this server understands", so we union :core with
// all the extension sets. A surface decodes to map[interface{}]interface{} and
// each name set to map[interface{}]bool (an EDN set) — we copy out only the
// edn.Symbol keys. Missing keys default to empty: a minimal welcome need not
// carry every section, and the result is always a non-nil set.
func flattenSurface(surface interface{}) map[edn.Symbol]bool {
	names := map[edn.Symbol]bool{}
	m, ok := surface.(map[interface{}]interface{})
	if !ok {
		return names
	}
	addSet := func(raw interface{}) {
		set, ok := raw.(map[interface{}]bool)
		if !ok {
			return
		}
		for k := range set {
			if sym, ok := k.(edn.Symbol); ok {
				names[sym] = true
			}
		}
	}
	addSet(m[edn.Keyword("core")])
	if exts, ok := m[edn.Keyword("extensions")].(map[interface{}]interface{}); ok {
		for _, packNames := range exts {
			addSet(packNames)
		}
	}
	return names
}

// readWelcome parses a :welcome envelope (the parsed body map) into a
// ServerInfo. We pull :version, :capabilities (an EDN set of Keyword flags),
// :limits (a Keyword -> value map), and the flattened :verbs / :predicates
// surfaces. Every key is optional: a server that omits a section yields an empty
// default rather than an error, so this stays robust against minimal welcomes
// (a bare {:event :welcome} parses to all-empty defaults with no nil-map panic).
func readWelcome(body interface{}) ServerInfo {
	si := ServerInfo{
		Capabilities: map[edn.Keyword]bool{},
		Limits:       map[edn.Keyword]interface{}{},
		Verbs:        flattenSurface(get(body, "verbs")),
		Predicates:   flattenSurface(get(body, "predicates")),
		Version:      get(body, "version"),
	}
	// :capabilities is an EDN set, so it decodes to map[interface{}]bool keyed by
	// edn.Keyword; narrow it to the typed flag set, ignoring any non-keyword key.
	if caps, ok := get(body, "capabilities").(map[interface{}]bool); ok {
		for k := range caps {
			if kw, ok := k.(edn.Keyword); ok {
				si.Capabilities[kw] = true
			}
		}
	}
	// :limits is an EDN map (map[interface{}]interface{}); keep its Keyword keys.
	if limits, ok := get(body, "limits").(map[interface{}]interface{}); ok {
		for k, v := range limits {
			if kw, ok := k.(edn.Keyword); ok {
				si.Limits[kw] = v
			}
		}
	}
	return si
}

// printServerInfo prints the one-line caps/limit summary both mains emit right
// after the welcome, mirroring python's
// `server: caps={…} max-message-bytes=…`. Cap names are sorted for a stable
// line; an unadvertised :max-message-bytes prints as "none" (the absent marker),
// otherwise the integer byte cap.
func printServerInfo(info ServerInfo) {
	names := make([]string, 0, len(info.Capabilities))
	for c := range info.Capabilities {
		// Python prints the keyword's bare name (no leading colon), e.g.
		// "lemma/cursor-pagination" — string(c), not c.String() which re-adds ":".
		names = append(names, string(c))
	}
	sort.Strings(names)
	limit := "none"
	if cap, advertised := info.maxMessageBytes(); advertised {
		limit = fmt.Sprintf("%d", cap)
	}
	fmt.Printf("server: caps={%s} max-message-bytes=%s\n",
		strings.Join(names, ", "), limit)
}

// withinMessageLimit reports whether ednText fits under the server's
// :max-message-bytes cap. The limit is measured in UTF-8 BYTES (SPEC §10), which
// for a Go string is simply len(ednText) — Go strings are already UTF-8. An
// unadvertised limit means unlimited, so any message passes.
func withinMessageLimit(si ServerInfo, ednText string) bool {
	cap, advertised := si.maxMessageBytes()
	if !advertised {
		return true
	}
	return int64(len(ednText)) <= cap
}

// ---------------------------------------------------------------------------
// Cursor pagination:  drain a (query …) across (continue #cursor …) pages
//
// A query with :limit returns a full first page with :done? false plus a
// #cursor handle (SPEC §8); (continue #cursor) carries the next :rows / :cursor
// / :done? until :done? is true. queryAll runs that drain loop so a caller can
// treat a paginated result as one flat row set. The send closure is the
// per-transport "form -> body" call (HTTP named with the session id dropped, or
// the UDS call as-is); a transport-level error propagates as err so the caller
// stops cleanly, while a server failure envelope (e.g. an expired cursor) is
// returned as failure rather than err — the two failure modes stay distinct.
// ---------------------------------------------------------------------------

// queryAll runs queryForm and drains every page via (continue #cursor …). The
// first send is the query form itself. Returns (rows, pages, failure, err):
//
//   - A failure envelope on the very first reply yields ([], 0, body, nil) — the
//     query was refused before any page came back.
//   - Otherwise :rows are collected and, while :done? is false, the per-page
//     #cursor is read and (continue <cursor>) sent. :cursor is present EXACTLY
//     when :done? is false — the server omits it on a single-page result — so we
//     read it only inside the loop, never on an already-done body.
//   - A failure envelope on a continue (e.g. an expired cursor coming back as
//     :error :unknown-handle, server idle TTL ~300s per SPEC §8) stops the drain
//     cleanly, returning the rows gathered so far plus that failure body; a real
//     client would re-issue the original query to start a fresh page.
//   - A transport-level error from send propagates as err.
func queryAll(send func(form interface{}) (interface{}, error), queryForm interface{}) (rows []interface{}, pages int, failure interface{}, err error) {
	body, err := send(queryForm)
	if err != nil {
		return nil, 0, nil, err
	}
	if isFailure(body) {
		return []interface{}{}, 0, body, nil
	}

	if r, ok := get(body, "rows").([]interface{}); ok {
		rows = append(rows, r...)
	}
	pages = 1
	for get(body, "done?") != true {
		// :cursor is present exactly when :done? is false — the server omits it
		// on a single-page (already-done) result, so we only read it here.
		cursor := get(body, "cursor")
		body, err = send(verb{edn.Symbol("continue"), cursor})
		if err != nil {
			return rows, pages, nil, err
		}
		if isFailure(body) {
			return rows, pages, body, nil
		}
		if r, ok := get(body, "rows").([]interface{}); ok {
			rows = append(rows, r...)
		}
		pages++
	}
	return rows, pages, nil, nil
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

	// 1a. Read the advertised capabilities and limits once, up front, so the rest
	//     of the round-trip can tailor itself to this server (SPEC §10). The caps
	//     line mirrors python's: a sorted, comma-joined cap-name set and the
	//     :max-message-bytes limit (or "none" when unadvertised).
	info := readWelcome(body)
	printServerInfo(info)

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

	// 7. The paginated section is gated on the server advertising cursor
	//    pagination — without it, draining pages via (continue #cursor …) is
	//    unsupported, so we skip the whole block rather than guess. The watch
	//    section below has its own gate, so this one wraps only the paged path
	//    rather than returning out of the whole walk.
	if info.supports(edn.Keyword("lemma/cursor-pagination")) {
		// Seed three subset-of facts in one propose, then assert the batch, so the
		// paginated query below has more rows than a single page holds. subset-of is
		// a pure-EDB (stored-fact) predicate, so a query over it has stable
		// (tx-id, ref-id) ordering and can be paginated; a rule-headed predicate like
		// member-of cannot be the sole outer :where pattern (the server rejects it
		// :bad-args :unsupported-rule-call-ordering).
		f1 := fact(edn.Symbol("subset-of"), entity("sub-a"), entity("group"))
		f2 := fact(edn.Symbol("subset-of"), entity("sub-b"), entity("group"))
		f3 := fact(edn.Symbol("subset-of"), entity("sub-c"), entity("group"))
		proposeForm := verb{edn.Symbol("propose"), f1, f2, f3}

		// The batch propose is the largest representative message we send, so it is
		// the one worth checking against :max-message-bytes. A real client checks
		// every outbound message; this demo checks this one. withinMessageLimit
		// measures the marshalled EDN in UTF-8 bytes.
		if !withinMessageLimit(info, render(proposeForm)) {
			fmt.Println("limit-exceeded: message exceeds max-message-bytes; skipping")
			return
		}

		body, err = named(proposeForm)
		if err != nil {
			fmt.Println(err)
			return
		}
		if isFailure(body) {
			fmt.Printf("propose (3x subset-of) refused: %s\n", describeFailure(body))
			return
		}
		proposal = get(body, "proposal")
		fmt.Printf("propose (3x subset-of ? group) -> %s  proposal=%s\n",
			render(get(body, "event")), render(proposal))
		body, err = named(verb{edn.Symbol("assert"), proposal})
		if err != nil {
			fmt.Println(err)
			return
		}
		if isFailure(body) {
			fmt.Printf("assert (3x subset-of) refused: %s\n", describeFailure(body))
			return
		}
		fmt.Printf("assert proposal -> %s\n", render(get(body, "event")))

		// 8. Paginated query: :limit 2 over 3 matching rows yields two pages (2 + 1).
		//    queryAll drains them via (continue #cursor …). It wants a form -> body
		//    closure; named already has that shape — it dropped the (header-threaded)
		//    session id internally — so it passes straight through.
		qform := verb{edn.Symbol("query"), map[edn.Keyword]interface{}{
			edn.Keyword("find"):  []interface{}{edn.Symbol("?x")},
			edn.Keyword("where"): []interface{}{
				[]interface{}{edn.Symbol("subset-of"), edn.Symbol("?x"), entity("group")},
			},
			edn.Keyword("limit"): 2,
		}}
		rows, pages, failure, err := queryAll(named, qform)
		if err != nil {
			fmt.Println(err)
			return
		}
		if failure != nil {
			fmt.Printf("paged query refused: %s\n", describeFailure(failure))
			return
		}
		fmt.Printf("paged query (subset-of ? group), limit 2 -> %d rows over %d page(s): %s\n",
			len(rows), pages, render(rows))
	} else {
		fmt.Println("server does not advertise cursor pagination; skipping paged query")
	}

	// 9. Watch: register a standing pattern and observe a matching change pushed
	//    back on the SSE event stream. Gated on the server advertising
	//    :lemma/watch — without it the (watch-pattern …) verb is unsupported, so
	//    we skip the demo rather than guess.
	if !info.supports(edn.Keyword("lemma/watch")) {
		fmt.Println("server does not advertise watch; skipping watch demo")
		return
	}

	// (watch-pattern :pattern [[subset-of ?x #entity "group"]]) — the args are
	// FLAT keyword args (the :pattern keyword then the where-vector), NOT a
	// wrapping map. The reply hands back a #watch handle to unwatch with.
	pattern := []interface{}{
		[]interface{}{edn.Symbol("subset-of"), edn.Symbol("?x"), entity("group")},
	}
	body, err = named(verb{edn.Symbol("watch-pattern"), edn.Keyword("pattern"), pattern})
	if err != nil {
		fmt.Println(err)
		return
	}
	if isFailure(body) {
		fmt.Printf("watch-pattern refused: %s\n", describeFailure(body))
		return
	}
	watch := get(body, "watch")
	fmt.Printf("watch (subset-of ? group) -> %s  watch=%s\n",
		render(get(body, "event")), render(watch))

	// Ordering is load-bearing: Dianoia registers this session's SSE sink lazily,
	// when the GET /events headers are written, and delivers a :watch-event only
	// to sinks present at emit time (no backlog replay). So OPEN the stream first
	// (registering the sink), THEN trigger the change, THEN drain — otherwise the
	// push can fire within milliseconds of the assert, before our sink exists,
	// and be silently lost.
	stream, err := openSSEStream(base, sid, sseTimeout)
	if err != nil {
		fmt.Println(err)
		return
	}
	// The server pushes only DELTAS, so the change must be new: a fact re-asserted
	// verbatim is a no-op and fires nothing. We key the probe entity to this
	// process so each run asserts a genuinely fresh fact.
	probe := entity(fmt.Sprintf("watch-probe-%d", os.Getpid()))
	body, err = named(verb{edn.Symbol("propose"), fact(edn.Symbol("subset-of"), probe, entity("group"))})
	if err != nil {
		stream.close()
		fmt.Println(err)
		return
	}
	if isFailure(body) {
		stream.close()
		fmt.Printf("watch-probe propose refused: %s\n", describeFailure(body))
		return
	}
	body, err = named(verb{edn.Symbol("assert"), get(body, "proposal")})
	if err != nil {
		stream.close()
		fmt.Println(err)
		return
	}
	if isFailure(body) {
		stream.close()
		fmt.Printf("watch-probe assert refused: %s\n", describeFailure(body))
		return
	}

	// Drain a single event, then ALWAYS release the SSE socket so the server
	// drops the stream, whether or not an event arrived.
	events := readSSEEvents(stream, 1)
	stream.close()
	if len(events) > 0 {
		evt := events[0]
		fmt.Printf("watch (subset-of ? group) -> %s type=%s data=%s\n",
			render(get(evt, "event")), render(get(evt, "type")), render(get(evt, "data")))
	} else {
		fmt.Println("watch: no event observed before timeout")
	}

	// Tear the watch down. (unwatch #watch "w-N") -> :ok.
	body, err = named(verb{edn.Symbol("unwatch"), watch})
	if err != nil {
		fmt.Println(err)
		return
	}
	if isFailure(body) {
		fmt.Printf("unwatch refused: %s\n", describeFailure(body))
		return
	}
	fmt.Printf("unwatch %s -> %s\n", render(watch), render(get(body, "event")))
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

// udsAwaitWatchEvent reads framed replies until a :watch-event arrives and
// returns it, or nil if none does within the bound. Over UDS there is no
// separate event channel: watch pushes interleave with ordinary command
// responses on the SAME frame stream (uds.clj fans both onto the one
// connection). So after triggering a change we read frames in a loop, skipping
// command replies (the :asserted echo, etc.) until we see the :watch-event
// envelope. The loop is bounded two ways so a missing push can never hang: by
// maxFrames, and by a read deadline set on the connection before the loop — a
// timed-out read surfaces as an error from udsRecvFrame, which ends the loop
// and yields nil ("no event observed"). Mirrors python's _uds_await_watch_event.
func udsAwaitWatchEvent(conn net.Conn, maxFrames int) interface{} {
	// Bound every read so a missing push degrades to nil rather than blocking.
	conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	for i := 0; i < maxFrames; i++ {
		raw, err := udsRecvFrame(conn)
		if err != nil {
			// EOF, a timeout, or a truncated frame: stop and report nothing.
			return nil
		}
		var body interface{}
		if err := edn.Unmarshal([]byte(raw), &body); err != nil {
			continue // unparseable frame — skip and keep reading
		}
		if get(body, "event") == edn.Keyword("watch-event") {
			return body
		}
	}
	return nil
}

// ---------------------------------------------------------------------------
// Watch over HTTP:  the SSE event stream  ->  parsed :watch-event envelopes
//
// A (watch-pattern …) call registers a standing query; matching changes are
// then *pushed* to the session rather than polled. Over HTTP those pushes
// arrive on a separate Server-Sent-Events stream, GET /v1/sessions/{id}/events
// (SPEC §9). SSE is a one-way text stream: each event is one or more `data:`
// lines terminated by a blank line; `:`-prefixed lines are keep-alive comments
// to be ignored.
//
// Why a raw net.Conn instead of net/http? Dianoia (http-kit) serves the stream
// with Transfer-Encoding: chunked and writes an immediate size-0 chunk to flush
// the response headers before any event exists. A standard chunked reader
// (net/http) treats that size-0 chunk as end-of-body and reports EOF, closing
// the stream before the first event ever arrives. So we speak HTTP by hand over
// a raw net.Conn — the same plumbing the UDS transport already uses — and treat
// a size-0 chunk as a keep-alive flush (skip it, keep reading) rather than as
// the end of the stream. Every read is bounded by a deadline so a quiet stream
// can never hang the demo.
//
// ORDERING IS LOAD-BEARING. Dianoia registers the per-session SSE sink LAZILY,
// at the moment the GET /events connection's headers are written — and the
// watch dispatcher delivers a :watch-event only to sinks present at emit time,
// with NO backlog replay. So the stream must be OPENED (sink registered) BEFORE
// the change that triggers the event, or the push races ahead of the sink and
// is lost. We therefore split the work in two:
//
//   - openSSEStream — dial, send the GET, read PAST the status line and headers
//     (writing the request + draining headers is what makes Dianoia register
//     the sink), and hand back an open handle. Call this BEFORE the trigger.
//   - readSSEEvents — drain parsed events from an already-open handle, AFTER the
//     trigger. Bounded by the handle's read deadline.
//
// This is read-only and single-threaded by design. Stdlib only for the plumbing
// (net + net/url); the EDN codec is the same go-edn used everywhere above.
// ---------------------------------------------------------------------------

// sseTimeout bounds every read on the SSE stream — the per-read deadline that
// keeps a quiet stream from hanging the demo. Mirrors python's 10.0s.
const sseTimeout = 10 * time.Second

// sseStream is an open SSE connection: the raw conn plus the body bytes already
// read past the header terminator. openSSEStream builds one (connection live,
// headers consumed, server sink registered); readSSEEvents drains events from
// it; close releases the conn so the server drops the stream. buf holds any
// body bytes carried across the header read into the body decode so the chunked
// decoder does not lose them. Mirrors python's _SSEStream.
type sseStream struct {
	conn net.Conn
	buf  []byte
}

// close releases the connection, letting the server tear down the stream.
func (s *sseStream) close() {
	s.conn.Close()
}

// openSSEStream opens the SSE event stream for sessionID and returns an
// *sseStream. It parses host/port from base (e.g. "http://127.0.0.1:8080"),
// dials a raw connection, issues GET /v1/sessions/{id}/events with an
// Accept: text/event-stream header, and reads PAST the status line and response
// headers — stopping at the blank line that begins the body. It does NOT read
// any event bodies; that is readSSEEvents's job.
//
// The split matters because writing the GET and draining its headers is what
// makes Dianoia register this session's SSE sink, and the watch dispatcher only
// delivers to sinks that exist when an event is emitted (no replay). So a caller
// must open the stream BEFORE triggering the change it wants to observe, then
// read AFTER — otherwise the push races ahead of the sink and is lost.
//
// A connection-level dial failure is the one error returned (the caller has no
// handle to close). Once dialed, an early close or a timeout DURING the header
// read still returns a (degraded) handle with an empty buffer rather than an
// error, so the caller's close path stays uniform: the subsequent read will see
// EOF and yield no events. Mirrors python's open_sse_stream.
func openSSEStream(base, sessionID string, timeout time.Duration) (*sseStream, error) {
	parts, err := url.Parse(base)
	if err != nil {
		return nil, fmt.Errorf("parsing SSE base URL %q: %w", base, err)
	}
	host := parts.Hostname()
	port := parts.Port()
	if port == "" {
		port = "80"
	}

	conn, err := net.DialTimeout("tcp", net.JoinHostPort(host, port), timeout)
	if err != nil {
		return nil, fmt.Errorf(
			"could not open the Lemma SSE stream at %q (%v); is the server running?",
			base, err)
	}

	request := "GET /v1/sessions/" + sessionID + "/events HTTP/1.1\r\n" +
		"Host: " + net.JoinHostPort(host, port) + "\r\n" +
		"Accept: text/event-stream\r\n" +
		"X-Lemma-Session: " + sessionID + "\r\n" +
		"Connection: keep-alive\r\n\r\n"
	conn.SetReadDeadline(time.Now().Add(timeout))
	if _, err := conn.Write([]byte(request)); err != nil {
		// Write failed before the sink could register: hand back a degraded
		// handle (empty buf) so the caller's close path is uniform.
		return &sseStream{conn: conn, buf: nil}, nil
	}

	// Consume the status line and headers; the body starts after the blank
	// line. Anything already read past it is retained on the handle so the
	// chunked decoder in readSSEEvents does not lose those bytes. Draining the
	// headers here is the act that registers the server-side sink.
	var buf []byte
	chunk := make([]byte, 4096)
	for !bytes.Contains(buf, []byte("\r\n\r\n")) {
		conn.SetReadDeadline(time.Now().Add(timeout))
		n, err := conn.Read(chunk)
		buf = append(buf, chunk[:n]...)
		if err != nil {
			// Server closed (EOF), a timeout, or a broken connection before the
			// headers completed: hand back a degraded handle (empty body) so the
			// caller's close path stays uniform; the read will see EOF and report
			// no events.
			return &sseStream{conn: conn, buf: nil}, nil
		}
	}
	_, body, _ := bytes.Cut(buf, []byte("\r\n\r\n"))
	return &sseStream{conn: conn, buf: body}, nil
}

// readSSEEvents drains up to maxEvents parsed envelopes from an open *sseStream.
// The handle's conn is live and its headers already consumed. This hand-decodes
// the chunked transfer body and parses Server-Sent Events out of it: each
// event's `data:` lines are concatenated and run through the codec, so the
// returned slice is the parsed envelopes (typically :watch-event maps).
//
// A read deadline (re-armed before each conn.Read) or end of stream ends the
// read and returns whatever arrived so far, so a quiet stream degrades to an
// empty slice rather than hanging — it never blocks. A size-0 chunk is
// http-kit's header-flush keep-alive, NOT end-of-stream, so we skip it and keep
// reading. A genuine connection close (Read returns 0, err) ends the read. The
// caller owns the conn and closes it; this only reads. Mirrors python's
// read_sse_events.
func readSSEEvents(stream *sseStream, maxEvents int) []interface{} {
	conn := stream.conn

	// fill reads more bytes onto the stream buffer; a deadline or EOF surfaces
	// as a non-nil error so the read loops below stop cleanly.
	fill := func() error {
		conn.SetReadDeadline(time.Now().Add(sseTimeout))
		chunk := make([]byte, 4096)
		n, err := conn.Read(chunk)
		stream.buf = append(stream.buf, chunk[:n]...)
		if n == 0 && err != nil {
			return err
		}
		return nil
	}

	// readLine pulls one CRLF-delimited line (for chunk-size lines).
	readLine := func() ([]byte, error) {
		for !bytes.Contains(stream.buf, []byte("\r\n")) {
			if err := fill(); err != nil {
				return nil, err
			}
		}
		line, rest, _ := bytes.Cut(stream.buf, []byte("\r\n"))
		stream.buf = rest
		return line, nil
	}

	// readN pulls exactly n bytes (for chunk bodies, plus the trailing CRLF).
	readN := func(n int) ([]byte, error) {
		for len(stream.buf) < n {
			if err := fill(); err != nil {
				return nil, err
			}
		}
		out := stream.buf[:n]
		stream.buf = stream.buf[n:]
		return out, nil
	}

	var events []interface{}
	var text string // decoded body bytes awaiting SSE framing
	for len(events) < maxEvents {
		sizeLine, err := readLine()
		if err != nil {
			break // EOF or read deadline: return what we gathered
		}
		sizeStr := strings.TrimSpace(string(sizeLine))
		if sizeStr == "" {
			continue // stray blank line between chunks — ignore
		}
		size, err := strconv.ParseInt(sizeStr, 16, 64)
		if err != nil {
			break // malformed chunk-size line: stop cleanly
		}
		if size == 0 {
			continue // header-flush keep-alive, not end-of-stream
		}
		bodyBytes, err := readN(int(size))
		if err != nil {
			break
		}
		text += string(bodyBytes)
		if _, err := readN(2); err != nil { // the CRLF trailing every chunk body
			break
		}

		// An SSE event is the run of lines up to the next blank line.
		// Concatenate its `data:` payloads (dropping `:` comment lines) and
		// parse the result as one EDN envelope.
		for strings.Contains(text, "\n\n") {
			block, rest, _ := strings.Cut(text, "\n\n")
			text = rest
			var data []string
			for _, line := range strings.Split(block, "\n") {
				if strings.HasPrefix(line, "data:") {
					data = append(data, strings.TrimLeft(line[len("data:"):], " "))
				}
			}
			if len(data) > 0 {
				var evt interface{}
				if err := edn.Unmarshal([]byte(strings.Join(data, "\n")), &evt); err == nil {
					events = append(events, evt)
				}
				if len(events) >= maxEvents {
					break
				}
			}
		}
	}
	return events
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

	// 1a. Read the advertised capabilities and limits once, up front, so the rest
	//     of the round-trip can tailor itself to this server (SPEC §10) — same
	//     caps/limit line the HTTP path prints.
	info := readWelcome(body)
	printServerInfo(info)

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

	// 6. The paginated section is gated on the server advertising cursor
	//    pagination — same as the HTTP path. Without it we skip just the paged
	//    block (the watch section below has its own gate) rather than guess at
	//    draining pages via (continue #cursor …).
	if info.supports(edn.Keyword("lemma/cursor-pagination")) {
		// Seed three subset-of facts in one propose, then assert the batch, so the
		// paginated query below spans more than one page. subset-of is a pure-EDB
		// (stored-fact) predicate, so a query over it has stable (tx-id, ref-id)
		// ordering and can be paginated; a rule-headed predicate like member-of
		// cannot be the sole outer :where pattern (the server rejects it :bad-args
		// :unsupported-rule-call-ordering).
		f1 := fact(edn.Symbol("subset-of"), entity("sub-a"), entity("group"))
		f2 := fact(edn.Symbol("subset-of"), entity("sub-b"), entity("group"))
		f3 := fact(edn.Symbol("subset-of"), entity("sub-c"), entity("group"))
		proposeForm := verb{edn.Symbol("propose"), f1, f2, f3}

		// The batch propose is the largest representative message we send, so it is
		// the one worth checking against :max-message-bytes (measured in UTF-8 bytes).
		if !withinMessageLimit(info, render(proposeForm)) {
			fmt.Println("limit-exceeded: message exceeds max-message-bytes; skipping")
			return
		}

		body, err = call(proposeForm)
		if err != nil {
			// Over UDS the connection carries the whole sequence; a read/write error
			// here (e.g. the server closed after the basic query) is a transport
			// failure, so print it and stop cleanly rather than hang.
			fmt.Println(err)
			return
		}
		if isFailure(body) {
			fmt.Printf("propose (3x subset-of) refused: %s\n", describeFailure(body))
			return
		}
		proposal = get(body, "proposal")
		fmt.Printf("propose (3x subset-of ? group) -> %s  proposal=%s\n",
			render(get(body, "event")), render(proposal))
		body, err = call(verb{edn.Symbol("assert"), proposal})
		if err != nil {
			fmt.Println(err)
			return
		}
		if isFailure(body) {
			fmt.Printf("assert (3x subset-of) refused: %s\n", describeFailure(body))
			return
		}
		fmt.Printf("assert proposal -> %s\n", render(get(body, "event")))

		// 7. Paginated query: :limit 2 over 3 matching rows yields two pages (2 + 1).
		//    queryAll drains them via (continue #cursor …). The UDS call closure is
		//    already form -> body, so it is passed directly.
		qform := verb{edn.Symbol("query"), map[edn.Keyword]interface{}{
			edn.Keyword("find"):  []interface{}{edn.Symbol("?x")},
			edn.Keyword("where"): []interface{}{
				[]interface{}{edn.Symbol("subset-of"), edn.Symbol("?x"), entity("group")},
			},
			edn.Keyword("limit"): 2,
		}}
		rows, pages, failure, err := queryAll(call, qform)
		if err != nil {
			fmt.Println(err)
			return
		}
		if failure != nil {
			fmt.Printf("paged query refused: %s\n", describeFailure(failure))
			return
		}
		fmt.Printf("paged query (subset-of ? group), limit 2 -> %d rows over %d page(s): %s\n",
			len(rows), pages, render(rows))
	} else {
		fmt.Println("server does not advertise cursor pagination; skipping paged query")
	}

	// 8. Watch over UDS. Same standing-pattern idea as the HTTP path, but the push
	//    has nowhere separate to go: it interleaves with command replies on this
	//    one socket. Gated on the server advertising :lemma/watch.
	if !info.supports(edn.Keyword("lemma/watch")) {
		fmt.Println("server does not advertise watch; skipping watch demo")
		return
	}

	// (watch-pattern :pattern [[subset-of ?x #entity "group"]]) — flat keyword
	// args, as on HTTP (the :pattern keyword then the where-vector, NOT a wrapping
	// map). The reply carries the #watch handle.
	pattern := []interface{}{
		[]interface{}{edn.Symbol("subset-of"), edn.Symbol("?x"), entity("group")},
	}
	body, err = call(verb{edn.Symbol("watch-pattern"), edn.Keyword("pattern"), pattern})
	if err != nil {
		fmt.Println(err)
		return
	}
	if isFailure(body) {
		fmt.Printf("watch-pattern refused: %s\n", describeFailure(body))
		return
	}
	watch := get(body, "watch")
	fmt.Printf("watch (subset-of ? group) -> %s  watch=%s\n",
		render(get(body, "event")), render(watch))

	// Trigger a fresh delta (a verbatim re-assert is a no-op and fires nothing),
	// keyed to this process so each run is genuinely new. The :asserted reply and
	// the :watch-event push both land on this socket; we read the assert reply
	// here via call, then demux the push below via udsAwaitWatchEvent.
	probe := entity(fmt.Sprintf("watch-probe-%d", os.Getpid()))
	body, err = call(verb{edn.Symbol("propose"), fact(edn.Symbol("subset-of"), probe, entity("group"))})
	if err != nil {
		fmt.Println(err)
		return
	}
	if isFailure(body) {
		fmt.Printf("watch-probe propose refused: %s\n", describeFailure(body))
		return
	}
	body, err = call(verb{edn.Symbol("assert"), get(body, "proposal")})
	if err != nil {
		fmt.Println(err)
		return
	}
	if isFailure(body) {
		fmt.Printf("watch-probe assert refused: %s\n", describeFailure(body))
		return
	}

	// Demux the watch push off the same frame stream. udsAwaitWatchEvent sets its
	// own read deadline before the loop, so a missing push degrades to nil rather
	// than hanging.
	if evt := udsAwaitWatchEvent(conn, 8); evt != nil {
		fmt.Printf("watch (subset-of ? group) -> %s type=%s data=%s\n",
			render(get(evt, "event")), render(get(evt, "type")), render(get(evt, "data")))
	} else {
		fmt.Println("watch: no event observed before timeout")
	}

	// Tear the watch down. (unwatch #watch "w-N") -> :ok. udsAwaitWatchEvent left
	// an absolute read deadline on the conn (possibly already elapsed if the push
	// was missed); re-arm it so this final read is bounded afresh rather than
	// failing instantly on a stale deadline.
	conn.SetReadDeadline(time.Now().Add(sseTimeout))
	body, err = call(verb{edn.Symbol("unwatch"), watch})
	if err != nil {
		fmt.Println(err)
		return
	}
	if isFailure(body) {
		fmt.Printf("unwatch refused: %s\n", describeFailure(body))
		return
	}
	fmt.Printf("unwatch %s -> %s\n", render(watch), render(get(body, "event")))
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
