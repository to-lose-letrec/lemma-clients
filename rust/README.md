# Rust Lemma Client

A single-file Rust client that demonstrates the Lemma wire protocol against a
local [Dianoia](https://github.com/to-lose-letrec/lemma) server. It speaks
**both** transports Dianoia exposes: HTTP (the default) and a Unix domain
socket. It leans on one third-party library — the
[`edn-format`](https://crates.io/crates/edn-format) reader/writer — for the wire
codec; everything else is the standard library. The same `emit_str` / `parse_str`
codec serializes both transports. Read `src/lemma_client.rs` end to end — it is a
recipe, not a library.

The recipe lives at `src/lemma_client.rs` and is wired as the crate's binary via
a `[[bin]]` path entry in `Cargo.toml`, so `cargo run` walks the round-trip
straight out of that one file.

**A Rust-specific divergence to note up front.** Rust's standard library has no
HTTP client. Pulling in `reqwest`/`hyper` (and its async runtime) to POST a few
hundred bytes would dwarf the recipe it serves and hide the very thing the repo
exists to show: the wire. So this demo speaks **HTTP/1.1 by hand over a
`std::net::TcpStream`** — in keeping with the repo's see-the-wire spirit, and
matching every sibling, each of which already hand-rolls the SSE stream for the
same reason. Here that spirit extends to the request path too. It is a few dozen
readable lines and the whole conversation stays visible. Read the file end to
end; it is a recipe, not a library.

## Prerequisites

- A `rustup` toolchain. The one-liner from [rustup.rs](https://rustup.rs):

  ```sh
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
  ```

- `edn-format`, the only third-party dependency. It is declared in `Cargo.toml`
  and resolves from crates.io on the first build — `cargo run` or `cargo test`
  fetches it for you. Everything else the client uses is the standard library.
  This matches the parent project's "standard library plus one EDN reader" demo
  budget.
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
cargo run
```

The default base URL is `http://127.0.0.1:8080` (`DEFAULT_BASE`). Pass a single
URL argument to override it:

```sh
cargo run -- http://host:port
```

## Run (UDS)

```sh
cargo run -- uds
```

A leading `uds` argument selects the Unix-domain-socket transport and connects
to `/tmp/dianoia.sock` (`DEFAULT_SOCKET`). Pass a path after `uds` to override
the socket:

```sh
cargo run -- uds /path/to/dianoia.sock
```

`dispatch` routes the arguments after the program name to a transport: no
argument runs `main_run(DEFAULT_BASE)`; a leading `uds` runs `main_uds(...)`
(against `DEFAULT_SOCKET`, or the path given after `uds`); any other leading
argument is an HTTP base URL passed to `main_run(...)`. No network I/O occurs at
load time — only `main` / `dispatch` touch the network.

## What it does

`main_run` runs one linear propose/assert/query round-trip, printing a single
line per step:

1. `(hello)` — anonymous `POST /v1/messages`. The `:welcome` reply carries the
   new session id in the `X-Lemma-Session` response header, surfaced by
   `post_edn`. `read_welcome` parses the reply into a `ServerInfo`, and a
   `server: caps=… max-message-bytes=…` summary line is printed by
   `print_server_info` (see [Capabilities and limits](#capabilities-and-limits)).
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
   `info.supports("lemma/cursor-pagination")`; before sending, the batch is
   checked against `:max-message-bytes` with `within_message_limit`.
7. `(query {... :limit 2})` — run a paginated query through `query_all`, which
   drains every page via `(continue #cursor ...)` and prints the total row count
   and page count. See [Pagination](#pagination).
8. `(watch-pattern :pattern [[subset-of ?x #entity "group"]])` — register a
   standing pattern, open the SSE stream, assert one fresh matching fact, observe
   the resulting `:watch-event` push, then `(unwatch #watch "...")`. Gated on
   `info.supports("lemma/watch")`. See [Watch / streaming](#watch--streaming).

Steps 6–7 run only when the server advertises `:lemma/cursor-pagination`;
otherwise the client prints `server does not advertise cursor pagination;
skipping paged query`. Step 8 runs only when the server advertises
`:lemma/watch`; otherwise the client prints `server does not advertise watch;
skipping watch demo`.

After every response the code inspects `:event`; an `:error` or `:rejected`
envelope is printed via `describe_failure` and the sequence returns cleanly —
no `unwrap()`/`expect()` on server-controlled data. A connection-level failure
(server down / refused) is caught at the hello, where `post_edn` names the base
URL, and the actionable line is printed.

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
above.

`main_uds` runs the identical verb sequence and prints the same per-step lines;
only the transport differs (see below).

## How HTTP maps to the wire

EDN bodies are produced and consumed with `edn-format`, but the HTTP envelope is
hand-rolled over a `std::net::TcpStream` — there is no `std` HTTP client, and a
heavyweight one would hide the wire. `post_edn` writes the request line and
headers by hand, sets `Connection: close`, and reads the response to EOF:

| Concern | Implementation |
|---|---|
| Body format | EDN text, encoded UTF-8 by `post_edn` (`emit_str` out, `parse_str` in) |
| Content type | `Content-Type: application/edn` |
| Transport | HTTP/1.1 written by hand over `std::net::TcpStream` (`parse_host_port` splits `host:port`) |
| Anonymous call | `POST /v1/messages` with `(hello)` |
| Named call | `POST /v1/sessions/{id}/messages`, echoing the id in the `x-lemma-session` request header |
| Session id | Read from the `X-Lemma-Session` response header (case-insensitively, via `split_http_response`) |

A non-2xx response still carries a valid Lemma EDN error envelope; `post_edn`
parses and returns it rather than failing, so the caller can inspect `:event`.
A connection-level failure (server down, refused, or an unparseable response) is
the one transport error: `post_edn` returns an `Err(String)` naming the base
URL.

The list-vs-vector split is the one design decision the codec leans on: a
**list** `( ... )` appears only as the top-level verb form (`(propose ...)`,
`(query ...)`, `(hello)`); every collection inside the arguments is a **vector**
`[ ... ]`. Rust is the simplest sibling here — `edn-format` has a distinct
`Value::List` variant that emits a bare top-level list natively, so the `verb`
helper is a one-line `Value::List(...)` constructor rather than a custom type
(Python leaned on the tuple/list split and Go carried a `verb` type because
go-edn cannot emit a bare top-level list). Argument collections are
`Value::Vector(...)`; for example `:find [?o]` and `:where [[...]]` are vectors,
only the verb head is a list. The ten core Lemma tagged literals — `#entity
#world #fact #violation #proposal #tx #ref #cursor #watch #session` — round-trip
in both directions with no registration at all: `Value::TaggedElement(Symbol,
Box<Value>)` handles every tag, so `parse_str` reconstructs a tag from wire text
and `emit_str` re-emits the exact `#tag payload`. A tag the client never names
(e.g. `#inst`) still parses, so an unexpected tag never breaks a response. Thin
constructor helpers keep the round-trip readable: `entity(name)` and
`world(name)` build the corresponding `#entity` / `#world` handles, and
`fact_value(predicate, subject, object)` builds a `#fact{...}` binary fact whose
`:predicate` is a `Symbol`.

## How UDS maps to the wire

UDS carries the same EDN over a single persistent `std::os::unix::net::UnixStream`,
with explicit framing instead of an HTTP envelope: a **4-byte big-endian
unsigned length** prefix followed by that many **UTF-8 bytes** of EDN.
`uds_send_frame` writes the frame (the body length as a big-endian `u32` via
`u32::to_be_bytes`, then the body bytes); `uds_recv_frame` `read_exact`s the
4-byte length (`u32::from_be_bytes`), then exactly that many body bytes, then
decodes UTF-8. A peer that closes mid-frame surfaces as `UnexpectedEof` naming
how many bytes were expected, rather than a silent short read. This matches
Dianoia's `transport/uds.clj` `write-frame` / `read-frame` exactly.

The session handling is the key contrast with HTTP. Over UDS the **session is
bound to the connection**: the server captures the id from the `:welcome`
envelope and pins it to the socket (`uds.clj` `handle-frame`), so the client
sends no `X-Lemma-Session` header and never echoes a session id back — it just
keeps sending frames on the same socket. The other difference: the `:welcome`
carries the session in its **body** (`:session`), which `main_uds` reads with
`get(&body, "session")`, not in a response header as HTTP does.

| Concern | Implementation |
|---|---|
| Body format | EDN text (`emit_str` out, `parse_str` in) |
| Framing | 4-byte big-endian length prefix, then the body bytes |
| Connection | One persistent `UnixStream` for every frame |
| Anonymous call | `(hello)` as the first frame |
| Session id | Bound to the connection by the server; read from `:session` for display; never echoed by the client |

Connecting to a missing socket returns an error naming the path. The connection
is closed by RAII: `main_uds`'s `stream` is dropped at the end of scope — and on
every early `return` — which also lets the server observe EOF and drop the
session. Where Python needed a try/finally and Go a `defer conn.Close()`, Rust
needs neither.

## Capabilities and limits

The `:welcome` envelope advertises what the server can do (SPEC §10): a
`:capabilities` set of namespaced flag keywords (e.g. `:lemma/cursor-pagination`,
`:lemma/watch`), a `:limits` map of resource caps (e.g. `:max-message-bytes`),
and `:verbs` / `:predicates` each shaped as `{:core #{...} :extensions {pack
#{...}}}`.

`read_welcome(body)` parses that surface into a `ServerInfo` record:

| Field / method | Meaning |
|---|---|
| `capabilities` | `BTreeSet<Keyword>` of capability flags (sorted, for a stable caps line) |
| `limits` | `BTreeMap<Keyword, Value>` of cap value |
| `verbs`, `predicates` | flat `BTreeSet<Symbol>` sets with `:core` and all `:extensions` merged (via `flatten_surface`) |
| `supports(cap)` | `true` iff the namespaced capability (e.g. `"lemma/cursor-pagination"`) is advertised |
| `max_message_bytes()` | the `:max-message-bytes` limit as `Some(i64)`, or `None` when unadvertised |

`supports` takes the **bare namespaced name** (`"lemma/cursor-pagination"`),
splits it on `/`, and builds the matching `Keyword` internally — callers never
construct the keyword themselves. Every section is optional; an omitted one
yields an empty default rather than an error — a bare `{:event :welcome}` parses
to all-empty defaults with no panic on server data. The client reads the welcome
once and tailors itself to it:

- It prints the `server: caps=… max-message-bytes=…` summary line after the
  hello line via `print_server_info`. Cap names print as the bare
  `namespace/name` (no leading colon), `", "`-joined; an unadvertised
  `:max-message-bytes` prints as `none`.
- It gates the paginated-query demo on `info.supports("lemma/cursor-pagination")`,
  skipping the block with `server does not advertise cursor pagination; skipping
  paged query` when the server does not advertise it.
- Before sending the batch-propose it checks the message against
  `:max-message-bytes` with `within_message_limit(&info, &emit_str(form))`,
  which compares the **UTF-8 byte length** to the cap. Because a Rust `&str` is
  already UTF-8, the byte measurement is simply `str::len()` — no re-encoding.
  An unadvertised limit means unlimited, so any message passes. This respects
  the limit locally rather than relying on a `:limit-exceeded` rejection. The
  demo checks only the representative batch-propose; a real client checks every
  outbound message.

## Pagination

A `(query ...)` with `:limit N` returns a first page. If the page is full the
reply carries `:done? false` and a `#cursor` handle; the client sends
`(continue #cursor "...")` to fetch each next page until a reply has
`:done? true`. A short result (fewer than `N` rows) or a query without `:limit`
comes back with `:done? true` and no `#cursor` — `:cursor` is present exactly
when `:done?` is not true, so `query_all` reads it only inside the drain loop and
feeds the parsed `#cursor` `TaggedElement` back verbatim, never reconstructing it
from text.

`query_all(send, query_form)` is the transport-agnostic helper that drains all
pages. `send` is a `FnMut(Value) -> Result<Value, E>` closure that sends one verb
form and returns the parsed reply body. It returns `(rows, pages, failure)`:
`rows` are concatenated across pages in page order, `pages` counts the result
envelopes drained, and `failure` is `None` on success or the offending
error/rejection envelope (the rows gathered so far are still returned); a
transport-level error from `send` propagates as `Err` so the caller stops
cleanly. Each transport adapts its own call into the closure: HTTP wraps `named`
in a `|form| named(&form)` closure, and UDS wraps `call` in
`|form| call(&mut stream, &form)`, threading the one open socket.

A `#cursor` is a server-side bookmark with a ~300-second idle TTL, refreshed on
each `(continue ...)`. An expired cursor returns `:error :unknown-handle`; a real
client re-issues the original query to start a fresh page, but this demo
propagates the failure through `query_all`'s `failure` return.

Pagination needs a **stably ordered** result, which requires a pure-EDB
(stored-fact) predicate at the outer `:where` level. The demo paginates over
`subset-of` for that reason; a rule-headed predicate such as `member-of` as the
sole `:where` pattern is rejected `:bad-args :unsupported-rule-call-ordering`.

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
(`watch-probe-<pid>`, via `std::process::id()`), so each run produces a fresh
`:added` event. Reads are bounded by `SSE_TIMEOUT` (10s) so a missing push
degrades to `watch: no event observed before timeout` rather than hanging.
`(unwatch #watch "...")` ends the subscription and replies `:ok`.

The two transports differ in **where the push arrives**:

| Transport | Push delivery | Consumer |
|---|---|---|
| UDS | Interleaved on the same socket as command replies | `uds_await_watch_event` |
| HTTP | A separate SSE stream, `GET /v1/sessions/{id}/events` | `open_sse_stream` / `read_sse_events` |

**UDS.** There is no separate event channel; the server fans watch pushes and
ordinary command replies onto the one connection (`uds.clj`). After triggering
the change, `uds_await_watch_event(stream, max_frames)` reads frames (via
`uds_recv_frame`) in a bounded loop, demultiplexing the `:watch-event` envelope
out of the command stream — skipping command replies (the `:asserted` echo,
etc.) and any unparseable frame until it sees the push, then returning it. The
loop is bounded two ways: by `max_frames`, and by a read timeout set on the
socket before the loop, so a timed-out read ends the loop and yields `None`
instead of blocking. After it returns, `main_uds` clears the read timeout
(`set_read_timeout(None)`) before the final `(unwatch ...)` so that read is not
poisoned by a stale per-read bound.

**HTTP.** Pushes arrive out-of-band on a Server-Sent-Events stream:
`GET /v1/sessions/{id}/events`, one or more `data:` lines per event terminated by
a blank line, with `:`-prefixed keep-alive comment lines. The stream is consumed
over a **raw `TcpStream`** rather than a chunked HTTP reader, for two reasons:

- Dianoia (http-kit) serves the stream `Transfer-Encoding: chunked` and writes
  an immediate **size-0 chunk** to flush the response headers before any event
  exists. A standard chunked reader treats that size-0 chunk as end-of-body and
  reports immediate EOF, closing the stream before the first event arrives. The
  raw reader treats a size-0 chunk as an http-kit keep-alive flush — skip it and
  keep reading — and only ends on a genuine connection close.
- Speaking the chunked transfer by hand lets the reader transfer-decode the
  frames and parse the `data:` lines itself. Each event's `data:` payloads are
  concatenated and run through `parse_str`, so handles like `#watch` round-trip.

The work is split in two because **ordering is load-bearing**: Dianoia registers
this session's SSE sink *lazily*, when the `GET /events` headers are written, and
delivers a `:watch-event` only to sinks present at emit time (no backlog replay).
So `open_sse_stream(base, session_id, timeout)` dials, sends the GET, and reads
past the status line and headers (the act that registers the sink), returning an
open `SseStream` handle — call it **before** triggering the change. Then
`read_sse_events(stream, max_events)` transfer-decodes the chunked body and
drains parsed envelopes from the open handle **after** the trigger. Every read is
bounded by the handle's read timeout (`SSE_TIMEOUT`), so a quiet stream returns
the events gathered so far — both `WouldBlock` and `TimedOut` are handled, as the
kind is platform-dependent. `SseStream` carries the live `TcpStream` plus any
body bytes already read past the header terminator, so the chunked decoder does
not lose them. Closing is RAII: dropping the handle closes the socket and lets
the server tear the stream down, so unlike Python's explicit `close` and Go's
`close` method, no teardown call is needed — every scope exit in the watch demo
drops the handle.

Both paths are read-only and single-threaded: the watch is established and the
change triggered first, then the consumer drains the push the server has queued
for the session.

## Tests

`cargo test` runs the suite. It lives in a `#[cfg(test)] mod tests` block at the
**bottom of the same `src/lemma_client.rs`** — 70 tests, no running server
required. The HTTP transport is driven against a scripted `std::net::TcpListener`
bound to `127.0.0.1:0` (an OS-assigned free port) with a scripted thread peer;
the UDS framing is exercised over `std::os::unix::net::UnixStream::pair()`
socketpairs and `main_uds` against a scripted `UnixListener`; and the SSE / watch
consumers are fed canned chunked streams. Everything is deterministic — no sleeps
anywhere, every wait is a blocking read or a thread join on a real I/O event, and
no real network beyond the loopback listeners.

```sh
cargo test
```

These tests live in-file by necessity. This recipe is a *binary* crate (see the
`[[bin]]` entry in `Cargo.toml`), and a binary crate exposes no library surface:
an out-of-tree `tests/` integration file cannot `use lemma_client::post_edn`
because there is nothing to import — the items are private to the crate's `main`.
The idiomatic Rust answer is an in-file `#[cfg(test)] mod tests` block, which
compiles only under `cargo test` and reaches the file's private fns via
`use super::*`. So the in-file module is this sibling's equivalent of the others'
separate test file — the whole client, codec, transport, and its tests stay
readable end to end in one place.

The suite covers the `edn-format` round-trips (including real response envelopes
and the unregistered-tag fallback), `read_welcome` / `ServerInfo` /
`within_message_limit`, `query_all`'s multi-page drain, both transports
(`post_edn` / `uds_send_frame` / `uds_recv_frame`), the watch consumers
(`open_sse_stream` / `read_sse_events` for the HTTP SSE stream,
`uds_await_watch_event` for the interleaved UDS stream), and drives `main_run` /
`main_uds` over the fakes so the handshake is verified without a live server.

## References

- `src/lemma_client.rs` — the constructor helpers (`entity`, `world`,
  `fact_value`, `verb`), the `:welcome` parser and `ServerInfo` helpers
  (`read_welcome`, `supports`, `max_message_bytes`, `within_message_limit`,
  `print_server_info`), both transports (`post_edn` for HTTP, `uds_send_frame` /
  `uds_recv_frame` for UDS), the envelope predicates (`is_failure`,
  `describe_failure`), the pagination helper (`query_all`), the watch consumers
  (`open_sse_stream` / `read_sse_events` and the `SseStream` handle for the HTTP
  SSE stream, `uds_await_watch_event` for the interleaved UDS stream), and the
  runnable `main_run` / `main_uds` round-trips dispatched by `dispatch`.
- `Cargo.toml` — the single `edn-format` dependency and the `[[bin]]` entry that
  points the binary at `src/lemma_client.rs`.
- [`edn-format`](https://crates.io/crates/edn-format) — the EDN reader/writer the
  codec is built on (the one third-party dependency).
- `../README.md` — project framing: these are from-scratch single-file recipes,
  not libraries.
