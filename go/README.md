# Go Lemma Client

A single-file Go client that demonstrates the Lemma wire protocol against a
local [Dianoia](https://github.com/to-lose-letrec/lemma) server. It speaks
**both** transports Dianoia exposes: HTTP (the default) and a Unix domain
socket. It leans on one third-party library — the `olympos.io/encoding/edn`
(go-edn) reader/writer — for the wire codec; everything else is the standard
library. The same `edn.Marshal` / `edn.Unmarshal` codec serializes both
transports. Read `lemma_client.go` end to end — it is a recipe, not a library.

## Prerequisites

- A Go toolchain at least as new as `go.mod`'s `go 1.26` directive.
- `olympos.io/encoding/edn`, the only third-party dependency. It is pinned in
  `go.mod` and resolves automatically — `go mod download` fetches it, or the
  first `go run .` / `go test ./...` will fetch it for you. Everything else the
  client uses is the standard library. This matches the parent project's
  "standard library plus one EDN reader" demo budget.
- A running Dianoia server reachable over HTTP or its Unix domain socket.

## Boot a local Dianoia

From the `dianoia` repository, with JDK 21 on `JAVA_HOME`:

```sh
LEMMA_HOME=/tmp/dianoia-worlds \
JAVA_HOME=/home/james/.local/share/jdk-21.0.11+10 \
clj -M -m dianoia.main
```

This binds an HTTP listener on `127.0.0.1:8080`, a Unix-domain-socket listener
at `/tmp/dianoia.sock`, and opens the world `default`.

The server *discovers* worlds; it does not create them. Before the first boot,
create the world directory out-of-band under `$LEMMA_HOME/worlds/default/`
with an empty `log.edn` and a minimal `meta.edn`:

```sh
mkdir -p /tmp/dianoia-worlds/worlds/default
cat > /tmp/dianoia-worlds/worlds/default/meta.edn <<'EOF'
{:uuid #uuid "00000000-0000-0000-0000-000000000001"
 :packs [{:name "core" :version "0.1.0"}]}
EOF
: > /tmp/dianoia-worlds/worlds/default/log.edn
```

## Run (HTTP)

```sh
go run .
```

The default base URL is `http://127.0.0.1:8080` (`DefaultBase`). Pass a single
URL argument to override it:

```sh
go run . http://host:port
```

## Run (UDS)

```sh
go run . uds
```

A leading `uds` argument selects the Unix-domain-socket transport and connects
to `/tmp/dianoia.sock` (`DefaultSocket`). Pass a path after `uds` to override
the socket:

```sh
go run . uds /path/to/dianoia.sock
```

`dispatch` routes `os.Args[1:]` to a transport: a leading `uds` runs
`mainUDS(...)` (against `DefaultSocket`, or the path given after `uds`); any
other argument is an HTTP base URL passed to `mainRun(...)`; no argument falls
back to `mainRun(DefaultBase)`. No network I/O occurs at import time — only
`main` / `dispatch` touch the network.

## What it does

`mainRun` runs one linear propose/assert/query round-trip, printing a single
line per step:

1. `(hello)` — anonymous `POST /v1/messages`. The `:welcome` reply carries the
   new session id in the `X-Lemma-Session` response header, surfaced by
   `postEDN`. `readWelcome` parses the reply into a `ServerInfo`, and a
   `server: caps=… max-message-bytes=…` summary line is printed by
   `printServerInfo` (see [Capabilities and limits](#capabilities-and-limits)).
2. `(use-world #world "default")` — enter the world on the session.
3. `(propose #fact{...})` — propose `equivalent morningstar venus`. The reply
   returns a `#proposal` handle.
4. `(assert <proposal>)` — assert the proposed fact into the world.
5. `(query {:find [?o] :where [[equivalent #entity "morningstar" ?o]]})` —
   query it back and print `:rows` and `:done?`.
6. `(propose #fact{...} #fact{...} #fact{...})` — batch-propose three
   `subset-of` facts (`sub-a`/`sub-b`/`sub-c` → `group`) in one
   `(propose f1 f2 f3)`, then `(assert <proposal>)` the batch, so the next
   query has more rows than a single page holds. Gated on
   `info.supports(edn.Keyword("lemma/cursor-pagination"))`; before sending, the
   batch is checked against `:max-message-bytes` with `withinMessageLimit`.
7. `(query {... :limit 2})` — run a paginated query through `queryAll`, which
   drains every page via `(continue #cursor ...)` and prints the total row
   count and page count. See [Pagination](#pagination).
8. `(watch-pattern :pattern [[subset-of ?x #entity "group"]])` — register a
   standing pattern, open the SSE stream, assert one fresh matching fact,
   observe the resulting `:watch-event` push, then `(unwatch #watch "...")`.
   Gated on `info.supports(edn.Keyword("lemma/watch"))`. See
   [Watch / streaming](#watch--streaming).

Steps 6–7 run only when the server advertises `:lemma/cursor-pagination`;
otherwise the client prints `server does not advertise cursor pagination;
skipping paged query`. Step 8 runs only when the server advertises
`:lemma/watch`; otherwise the client prints `server does not advertise watch;
skipping watch demo`.

After every response the code inspects `:event`; an `:error` or `:rejected`
envelope is printed via `describeFailure` and the sequence stops cleanly. A
connection-level failure (server down / refused) is caught at the hello, where
`postEDN` names the base URL, and the actionable line is printed.

Expected output (the session and proposal ids increment per run):

```text
hello -> :welcome  version=1  session=s-1  world=nil
server: caps={lemma/cursor-pagination, lemma/watch} max-message-bytes=1048576
use-world "default" -> :world-selected  world=#world "default"
propose (equivalent morningstar venus) -> :proposed  proposal=#proposal "p-1"
assert proposal -> :asserted
query (equivalent morningstar ?o) -> rows=[["venus"]]  done?=true
propose (3x subset-of ? group) -> :proposed  proposal=#proposal "p-2"
assert proposal -> :asserted
paged query (subset-of ? group), limit 2 -> 3 rows over 2 page(s): [["sub-a"] ["sub-b"] ["sub-c"]]
watch (subset-of ? group) -> :watch-established  watch=#watch "w-1"
watch (subset-of ? group) -> :watch-event type=:added data=[["watch-probe-12345"]]
unwatch #watch "w-1" -> :ok
```

The query binds `?o` to the matching entity's name, which Dianoia returns as a
plain string — hence `rows=[["venus"]]`, not a tagged `#entity` literal. Tagged
handles such as `#world` and `#proposal` do appear in the other replies shown
above. The paged row count reflects accumulated world state: each run asserts
another batch, so a re-run against the same world reports more `subset-of` rows
over more pages.

`mainUDS` runs the identical verb sequence and prints the same per-step lines;
only the transport differs (see below).

## How HTTP maps to the wire

EDN bodies are produced and consumed with go-edn, no codec to hand-roll:

| Concern | Implementation |
|---|---|
| Body format | EDN text, encoded UTF-8 by `postEDN` (`edn.Marshal` out, `edn.Unmarshal` in) |
| Content type | `Content-Type: application/edn` |
| Anonymous call | `POST /v1/messages` with `(hello)` |
| Named call | `POST /v1/sessions/{id}/messages`, echoing the id in the `x-lemma-session` request header |
| Session id | Read from the `X-Lemma-Session` response header |

A non-2xx response still carries a valid Lemma EDN error envelope; `postEDN`
parses and returns it rather than failing, so the caller can inspect `:event`.
A connection-level failure is the one transport error: `postEDN` returns a
non-nil `error` naming the base URL.

The list-vs-vector split is the one design decision the codec leans on: a
**list** `( ... )` appears only as the top-level verb form (`(propose ...)`,
`(query ...)`, `(hello)`). go-edn cannot emit a bare top-level list from a plain
Go value, so the client carries the verb form in a `verb []interface{}` type
whose `MarshalEDN` renders `( ... )` by hand; every collection inside the
arguments stays a plain `[]interface{}`, which go-edn marshals as a **vector**.
For example `:find [?o]` and `:where [[...]]` are vectors; only the verb head is
a list. The ten core Lemma tagged literals — `#entity #world #fact #violation
#proposal #tx #ref #cursor #watch #session` — round-trip in both directions: the
eight string-payload tags share one `Handle` type (with a registered `AddTagFn`
reader), and `#fact` / `#violation` carry a map (`Fact` / `Violation`). Tags
that are not registered (e.g. `#inst`) fall back to go-edn's generic `edn.Tag`,
so an unexpected tag never breaks a response. Thin constructor helpers keep the
round-trip readable: `entity(name)` and `world(name)` build the corresponding
`#entity` / `#world` handles, and `fact(predicate, subject, object)` builds a
`#fact{...}` binary fact whose `:predicate` is an `edn.Symbol`.

## How UDS maps to the wire

UDS carries the same EDN over a single persistent `net.Conn` (`AF_UNIX`), with
explicit framing instead of an HTTP envelope: a **4-byte big-endian length**
prefix followed by that many **UTF-8 bytes** of EDN. `udsSendFrame` writes the
frame (`binary.Write` of the body length as a big-endian `uint32`, then the body
bytes); `udsRecvFrame` reads the 4-byte length, then exactly that many body
bytes via `io.ReadFull`, then interprets them as a UTF-8 string. A peer that
closes mid-frame surfaces as `io.ErrUnexpectedEOF` rather than a short read.

The session handling is the key contrast with HTTP. Over UDS the **session is
bound to the connection**: the server captures the id from the `:welcome`
envelope and pins it to the socket, so the client sends no `X-Lemma-Session`
header and never echoes a session id back — it just keeps sending frames on the
same socket. (Over HTTP the id is threaded explicitly through the
`x-lemma-session` header and the named `/v1/sessions/{id}/messages` endpoint.)
The other difference: the `:welcome` carries the session in its **body**
(`:session`), which `mainUDS` reads with `get(body, "session")`, not in a
response header as HTTP does.

| Concern | Implementation |
|---|---|
| Body format | EDN text (`edn.Marshal` out, `edn.Unmarshal` in) |
| Framing | 4-byte big-endian length prefix, then the body bytes |
| Connection | One persistent `AF_UNIX` `net.Conn` for every frame |
| Anonymous call | `(hello)` as the first frame |
| Session id | Bound to the connection by the server; never echoed by the client |

Connecting to a missing socket returns an error naming the path; the connection
is always closed via `defer conn.Close()`, which also lets the server observe
EOF and drop the session.

## Capabilities and limits

The `:welcome` envelope advertises what the server can do: a `:capabilities`
set of namespaced flag keywords (e.g. `:lemma/cursor-pagination`,
`:lemma/watch`), a `:limits` map of resource caps (e.g. `:max-message-bytes`),
and `:verbs` / `:predicates` each shaped as `{:core #{...} :extensions {pack
#{...}}}`.

`readWelcome(body)` parses that surface into a `ServerInfo` record:

| Field / method | Meaning |
|---|---|
| `Capabilities` | `map[edn.Keyword]bool` set of capability flags |
| `Limits` | `map[edn.Keyword]interface{}` of cap value |
| `Verbs`, `Predicates` | flat `map[edn.Symbol]bool` sets with `:core` and all `:extensions` merged (via `flattenSurface`) |
| `supports(capability)` | `true` iff the `edn.Keyword` capability is advertised |
| `maxMessageBytes()` | returns the `:max-message-bytes` limit and `true`, or `(0, false)` when unadvertised |

Every section is optional; an omitted one yields an empty default (a non-nil
empty set / map) rather than an error — a bare `{:event :welcome}` parses to
all-empty defaults with no nil-map panic. The client reads the welcome once and
tailors itself to it:

- It prints the `server: caps=… max-message-bytes=…` summary line after the
  hello line.
- It gates the paginated-query demo on
  `info.supports(edn.Keyword("lemma/cursor-pagination"))`, skipping the block
  with `server does not advertise cursor pagination; skipping paged query` when
  the server does not advertise it.
- Before sending the batch-propose it checks the message against
  `:max-message-bytes` with `withinMessageLimit(info, render(proposeForm))`,
  which compares the **UTF-8 byte length** to the cap. Because a Go string is
  already UTF-8, the byte measurement is simply `len(ednText)`; an unadvertised
  limit means unlimited, so any message passes. This respects the limit locally
  rather than relying on a `:limit-exceeded` rejection. The demo checks only the
  representative batch-propose; a real client checks every outbound message.

## Pagination

A `(query ...)` with `:limit N` returns a first page. If the page is full the
reply carries `:done? false` and a `#cursor` handle; the client sends
`(continue #cursor "...")` to fetch each next page until a reply has
`:done? true`. A short result (fewer than `N` rows) or a query without `:limit`
comes back with `:done? true` and no `#cursor` — `:cursor` is present exactly
when `:done?` is false, so `queryAll` reads it only inside the drain loop.

`queryAll(send, queryForm)` is the transport-agnostic helper that drains all
pages. `send` is a `func(form interface{}) (interface{}, error)` closure that
sends one verb form and returns the parsed reply body. It returns
`(rows, pages, failure, err)`: `rows` are concatenated across pages, `failure`
is `nil` on success or the offending error/rejection envelope (the rows gathered
so far are still returned), and a transport-level `err` propagates so the caller
stops cleanly. Each transport adapts its own call into the closure: HTTP's
`named` already drops the header-threaded session id internally and is shaped
`form -> (body, err)`, so it passes straight through; UDS's `call` is already
`form -> (body, err)`, so `mainUDS` passes it directly.

A `#cursor` is a server-side bookmark with a ~300-second idle TTL, refreshed on
each `(continue ...)`. An expired cursor returns `:error :unknown-handle`; a
real client re-issues the original query to start a fresh page, but this demo
propagates the failure through `queryAll`'s `failure` return.

Pagination needs a **stably ordered** result, which requires a pure-EDB
(stored-fact) predicate with stable `(tx-id, ref-id)` ordering at the outer
`:where` level. The demo paginates over `subset-of` for that reason; a
rule-headed predicate such as `member-of` as the sole `:where` pattern is
rejected `:bad-args :unsupported-rule-call-ordering`.

## Watch / streaming

A `(watch-pattern :pattern [[subset-of ?x #entity "group"]])` call registers a
standing query on the session. The args are **flat keyword args** — the
`:pattern` keyword followed by the where-vector — not a wrapping map. The reply
is `{:event :watch-established :watch #watch "..."}`, returning a `#watch`
handle.

Watches are **deltas-only**: after the subscription, each matching *change* is
pushed as `{:event :watch-event :type :added|:retracted :data [...]}`. The
current contents are never replayed, and re-asserting a fact verbatim is a no-op
that fires nothing. The demo therefore opens the subscription first, then
triggers a genuinely new delta by asserting a fact keyed to the process
(`watch-probe-<pid>`, via `os.Getpid()`), so each run produces a fresh `:added`
event. Reads are bounded by a timeout so a missing push degrades to
`watch: no event observed before timeout` rather than hanging.
`(unwatch #watch "...")` ends the subscription and replies `:ok`.

The two transports differ in **where the push arrives**:

| Transport | Push delivery | Consumer |
|---|---|---|
| UDS | Interleaved on the same socket as command replies | `udsAwaitWatchEvent` |
| HTTP | A separate SSE stream, `GET /v1/sessions/{id}/events` | `openSSEStream` / `readSSEEvents` |

**UDS.** There is no separate event channel; the server fans watch pushes and
ordinary command replies onto the one connection. After triggering the change,
`udsAwaitWatchEvent(conn, maxFrames)` reads frames (via `udsRecvFrame`) in a
bounded loop, demultiplexing the `:watch-event` envelope out of the command
stream — skipping command replies (the `:asserted` echo, etc.) until it sees the
push, then returning it. The loop is bounded two ways: by `maxFrames`, and by a
read deadline set on the connection before the loop, so a timed-out read ends
the loop and yields `nil` instead of blocking. After it returns, `mainUDS`
re-arms the connection's read deadline before the final `(unwatch ...)` so that
read is bounded afresh rather than failing on a stale deadline.

**HTTP.** Pushes arrive out-of-band on a Server-Sent-Events stream:
`GET /v1/sessions/{id}/events`, one or more `data:` lines per event terminated
by a blank line, with `:`-prefixed keep-alive comment lines. The stream is
consumed over a **raw `net.Conn`** rather than `net/http`, for two reasons:

- Dianoia (http-kit) serves the stream `Transfer-Encoding: chunked` and writes
  an immediate **size-0 chunk** to flush the response headers before any event
  exists. `net/http`'s chunked reader treats that size-0 chunk as end-of-body
  and reports immediate EOF, closing the stream before the first event arrives.
  The raw reader treats a size-0 chunk as an http-kit keep-alive flush — skip it
  and keep reading — and only ends on a genuine connection close.
- Speaking the chunked transfer by hand lets the reader transfer-decode the
  frames and parse the `data:` lines itself. Each event's `data:` payloads are
  concatenated and run through `edn.Unmarshal`, so handles like `#watch`
  round-trip.

The work is split in two because **ordering is load-bearing**: Dianoia registers
this session's SSE sink *lazily*, when the `GET /events` headers are written, and
delivers a `:watch-event` only to sinks present at emit time (no backlog replay).
So `openSSEStream(base, sessionID, timeout)` dials, sends the GET, and reads past
the status line and headers (the act that registers the sink), returning an open
`*sseStream` handle — call it **before** triggering the change. Then
`readSSEEvents(stream, maxEvents)` transfer-decodes the chunked body and drains
parsed envelopes from the open handle **after** the trigger. Every read is
bounded by a re-armed read deadline (`sseTimeout`, 10s), so a quiet stream
returns the events gathered so far rather than hanging; the caller calls
`stream.close()` to drop the stream, whether or not an event arrived.

Both paths are read-only and single-threaded: the watch is established and the
change triggered first, then the consumer drains the push the server has queued
for the session.

## Tests

`lemma_client_test.go` is a standard-library `testing` suite — 59 tests, no
running server required. The HTTP transport is driven against an
`net/http/httptest` loopback server; the UDS framing and `mainUDS` round-trip
are exercised over an in-memory `net.Pipe` fake; and the SSE / watch consumers
are fed canned chunked streams. Everything is deterministic — no real network
beyond the `httptest` loopback. From this `go/` directory:

```sh
go test ./...
```

The suite covers the go-edn round-trips (including real response envelopes and
the unregistered-tag fallback), `readWelcome` / `ServerInfo` /
`withinMessageLimit`, `queryAll`'s multi-page drain, both transports
(`postEDN` / `udsSendFrame` / `udsRecvFrame`), the watch consumers
(`openSSEStream` / `readSSEEvents` for the HTTP SSE stream,
`udsAwaitWatchEvent` for the interleaved UDS stream), and drives `mainRun` /
`mainUDS` over the fakes so the handshake is verified without a live server.

## References

- `lemma_client.go` — the go-edn tag registrations and constructor helpers
  (`entity`, `world`, `fact`), the `verb` form, the `:welcome` parser and
  `ServerInfo` helpers (`readWelcome`, `supports`, `maxMessageBytes`,
  `withinMessageLimit`), both transports (`postEDN` for HTTP, `udsSendFrame` /
  `udsRecvFrame` for UDS), the envelope predicates (`isFailure`,
  `describeFailure`), the pagination helper (`queryAll`), the watch consumers
  (`openSSEStream` / `readSSEEvents` for the HTTP SSE stream,
  `udsAwaitWatchEvent` for the interleaved UDS stream), and the runnable
  `mainRun` / `mainUDS` round-trips dispatched by `dispatch`.
- [`olympos.io/encoding/edn`](https://github.com/go-edn/edn) — the EDN
  reader/writer the codec is built on (the one third-party dependency, pinned in
  `go.mod`).
- `../README.md` — project framing: these are from-scratch single-file recipes,
  not libraries.
```
