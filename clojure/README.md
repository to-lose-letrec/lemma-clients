# Clojure Lemma Client

A single-file Clojure client that demonstrates the Lemma wire protocol against a
local [Dianoia](https://github.com/to-lose-letrec/lemma) server over **both**
transports Dianoia exposes: HTTP (the default) and a Unix domain socket.
Clojure is the protocol's **native** language: Lemma speaks EDN (Clojure's own
data syntax), so there is **zero** to install. `clojure.edn` is the codec;
`java.net.http` (JDK 11+) is the HTTP transport and a `java.nio` `SocketChannel`
is the UDS transport — both built in. `deps.edn` carries no client dependencies.
This is the smallest of the demos. Read `lemma_client.clj` end to end; it is a
recipe, not a library.

## Prerequisites

- The Clojure CLI (`clojure` / `clj`) and a JDK. The client needs JDK 11+ for
  `java.net.http`; the system Java is fine. Only Dianoia itself needs JDK 21.
- A running Dianoia server reachable over HTTP or its Unix domain socket.

## Boot a local Dianoia

From the `dianoia` repository, with JDK 21:

```sh
LEMMA_HOME=/tmp/dianoia-worlds \
JAVA_CMD=/home/james/.local/share/jdk-21.0.11+10/bin/java \
clj -M -m dianoia.main
```

This binds an HTTP listener on `127.0.0.1:8080` and opens the world `default`.

The server *discovers* worlds; it does not create them. Before the first boot,
create the world directory out-of-band under `$LEMMA_HOME/worlds/default/` with
an empty `log.edn` and a minimal `meta.edn`:

```sh
mkdir -p /tmp/dianoia-worlds/worlds/default
cat > /tmp/dianoia-worlds/worlds/default/meta.edn <<'EOF'
{:uuid #uuid "00000000-0000-0000-0000-000000000001"
 :packs [{:name "core" :version "0.1.0"}]}
EOF
: > /tmp/dianoia-worlds/worlds/default/log.edn
```

## Run

```sh
clojure -M -m lemma-client
```

The default base URL is `http://127.0.0.1:8080` (`default-base`). Pass a single
URL argument to override it:

```sh
clojure -M -m lemma-client http://host:port
```

## Run (UDS)

```sh
clojure -M -m lemma-client uds
```

A leading `uds` argument selects the Unix-domain-socket transport and connects
to `/tmp/dianoia.sock` (`default-socket`). Pass a path after `uds` to override
the socket:

```sh
clojure -M -m lemma-client uds /path/to/dianoia.sock
```

`-main` routes `argv` to a transport: a leading `uds` runs `run-uds` (against
`default-socket`, or the path given after `uds`); a base URL runs `run-http`
against it; no argument runs `run-http` against `default-base`.

Loading the file performs no network I/O; only `-main` touches the network.

## What it does

`-main` runs one linear round-trip, printing a single line per step:

1. `(hello)` — anonymous `POST /v1/messages`. The `:welcome` reply carries the
   new session id in the `X-Lemma-Session` response header, which `post-edn`
   surfaces. `read-welcome` parses the reply into a ServerInfo map, and a
   `server: caps=… max-message-bytes=…` summary line is printed (see
   [Capabilities and limits](#capabilities-and-limits)).
2. `(use-world #world "default")` — enter the world on the session.
3. `(propose #fact{...})` — propose `equivalent morningstar venus`. The reply
   returns a `#proposal` handle in `:proposal`.
4. `(assert <proposal>)` — assert the proposed fact into the world.
5. `(query {:find [?o] :where [[equivalent #entity "morningstar" ?o]]})` — query
   it back and print `:rows` and `:done?`.
6. `(propose #fact{...})` ×3, `(assert <proposal>)` — batch-propose three
   `subset-of` facts (`sub-a`/`sub-b`/`sub-c` → `group`) in one `(propose f1 f2 f3)`
   and assert the batch. Gated on `(supports? info :lemma/cursor-pagination)`;
   before sending, the batch is checked against `:max-message-bytes` with
   `within-message-limit?`.
7. `(query {... :limit 2})` — run a paginated query through `query-all`, which
   drains every page via `(continue #cursor ...)` and prints the total row count
   and page count. See [Pagination](#pagination).

Steps 6–7 run only when the server advertises `:lemma/cursor-pagination`;
otherwise the client prints `server does not advertise cursor pagination;
skipping paged query`.

After every reply the code checks `:event`; an `:error` or `:rejected` envelope
is printed via `describe-failure` and the sequence stops cleanly. A
connection-level failure (server down/refused) is re-thrown by `post-edn` as an
`ex-info` naming the base URL; `-main` catches it, prints the actionable line,
and exits nonzero.

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
```

The query binds `?o` to the matching entity's name, which Dianoia returns as a
plain string — hence `rows=[["venus"]]`, not a tagged `#entity` literal. The
paged row count reflects accumulated world state: each run asserts another batch,
so a re-run against the same world reports more `subset-of` rows over more pages.

`run-uds` runs the identical verb sequence and prints the same per-step lines;
only the transport differs (see below).

## How it maps to the wire

EDN bodies are produced and consumed with `clojure.edn` and `pr-str`, no
codec to hand-roll:

| Concern | Implementation |
|---|---|
| Body format | EDN text via `pr-str` (out) and `(edn/read-string {:default tagged-literal} ...)` (in) |
| Content type | `content-type: application/edn` |
| Anonymous call | `POST /v1/messages` with `(hello)` |
| Named call | `POST /v1/sessions/{id}/messages`, echoing the id in the `x-lemma-session` request header |
| Session id | Read from the `X-Lemma-Session` response header |

**EDN is native.** This is the load-bearing difference from the sibling demos:
Python hand-feeds the `edn_format` codec and TypeScript imports `jsedn`, but
Clojure reads and writes EDN out of the box. `pr-str` renders any Clojure value
to EDN on the way out; `(edn/read-string {:default tagged-literal} ...)` parses
the response on the way in, with an unknown-tag `:default` of `tagged-literal`
so every Lemma tag — even one this client has never seen — round-trips back onto
the wire cleanly with no reader registration.

Verb forms are EDN **lists** built with `list`; everything inside the arguments
is a **vector**, map, or tagged literal. This is the one load-bearing
distinction: a list appears only as the top-level verb form — `(hello)`,
`(propose ...)`, `(query ...)` — while `:find`, `:where`, and the where-clause
are vectors. Tagged literals are built with `clojure.core/tagged-literal` via
the thin constructors `ent` (`#entity`), `wrld` (`#world`), and `fct`
(`#fact`).

## How UDS maps to the wire

UDS carries the same EDN over a single persistent `java.nio` `SocketChannel`
(opened with `StandardProtocolFamily/UNIX`), with explicit framing instead of an
HTTP envelope: a **4-byte big-endian length** prefix followed by that many
**UTF-8 bytes** of EDN. `uds-send-frame` writes the frame — a `ByteBuffer`
`.putInt` of the body length, then the body bytes, each buffer looped on
`.hasRemaining` to drain partial writes. `uds-recv-frame` reads exactly 4 bytes
for the length, then exactly that many body bytes, then decodes UTF-8; a partial
`.read` is looped and an early EOF surfaces as an `ex-info` rather than a short
read.

The session handling is the key contrast with HTTP. Over UDS the **session is
bound to the connection**: the server captures the id from the `:welcome`
envelope and pins it to the socket, so the client sends no `X-Lemma-Session`
header and never echoes a session id back — it just keeps sending frames on the
same open channel. (Over HTTP the id is threaded explicitly through the
`x-lemma-session` header and the named `/v1/sessions/{id}/messages` endpoint.)

| Concern | Implementation |
|---|---|
| Body format | EDN text via `pr-str` (out) and `(edn/read-string {:default tagged-literal} ...)` (in) |
| Framing | 4-byte big-endian length prefix, then the body bytes |
| Connection | One persistent `java.nio` `SocketChannel` (`StandardProtocolFamily/UNIX`) for every frame |
| Anonymous call | `(hello)` as the first frame |
| Session id | Bound to the connection by the server; never echoed by the client |

`uds-send-frame` / `uds-recv-frame` are the channel I/O seam, pulled out as
their own `defn`s so tests can `with-redefs` them with a canned exchange and
exercise `run-uds` without a live socket. Connecting to a missing socket throws
an `ex-info` naming the path; the channel is always closed in a `finally`.

## Capabilities and limits

The `:welcome` envelope advertises what the server can do: a `:capabilities`
set of namespaced flag keywords (e.g. `:lemma/cursor-pagination`,
`:lemma/watch`), a `:limits` map of resource caps (e.g. `:max-message-bytes`),
and `:verbs` / `:predicates` each shaped as `{:core #{...} :extensions {pack
#{...}}}`.

`read-welcome` parses that surface into a ServerInfo map. Because Lemma speaks
EDN — Clojure's own data syntax — there is no codec to cross, so ServerInfo is
just a plain map, not a new abstraction:

| Key / fn | Meaning |
|---|---|
| `:capabilities` | a set of capability keywords (`:lemma/...`) |
| `:limits` | a map of `:limit-keyword` → cap value |
| `:verbs`, `:predicates` | flat sets of symbols with `:core` and all `:extensions` merged |
| `(supports? info cap)` | `true` iff the `cap` keyword is advertised |
| `(max-message-bytes info)` | the `:max-message-bytes` limit, or `nil` if unadvertised |

Every section is optional; an omitted one yields an empty default (empty set /
empty map) rather than an error. The client reads the welcome once and tailors
itself to it:

- It prints the `server: caps=… max-message-bytes=…` summary line after the
  hello line.
- It gates the paginated-query demo on `(supports? info
  :lemma/cursor-pagination)`, skipping the block with `server does not advertise
  cursor pagination; skipping paged query` when the server does not advertise
  it.
- Before sending the batch-propose it checks the message against
  `:max-message-bytes` with `(within-message-limit? info (pr-str
  propose-form))`, which compares the UTF-8 byte length to the cap (`nil` means
  unlimited). This respects the limit locally rather than relying on a
  rejection. The demo checks only the representative batch-propose; a real
  client checks every outbound message.

## Pagination

A `(query ...)` with `:limit N` returns a first page. If the page is full the
reply carries `:done? false` and a `#cursor` handle; the client sends
`(continue #cursor "...")` to fetch each next page until a reply has
`:done? true`. A short result (fewer than `N` rows) or a query without `:limit`
comes back with `:done? true` and no `#cursor`.

`query-all` is the transport-agnostic helper that drains all pages:

```clojure
(query-all send query-form) ;=> {:rows <all-rows> :pages <n> :failure <body-or-nil>}
```

`send` is a `(fn [form] body)` closure that sends one verb LIST and returns the
parsed reply body; `query-all` concatenates `:rows` across pages and returns a
map of `:rows`, `:pages`, and `:failure` — `nil` on success, or the offending
error/rejection envelope (the rows gathered so far are still returned). Each
transport adapts its own call into the closure: HTTP's `named` returns a `:body`
map, so `run-http` passes `(fn [form] (:body (named form)))`; the UDS `call` is
already `form -> body`, so `run-uds` passes it directly.

A `#cursor` is a server-side bookmark with a ~300-second idle TTL. An expired
cursor returns `:error :unknown-handle`; a real client re-issues the original
query to start a fresh page, but this demo propagates the failure through
`query-all`'s `:failure`.

Pagination needs a **stably ordered** result, which requires a pure-EDB
(stored-fact) predicate with stable `(tx-id, ref-id)` ordering at the outer
`:where` level. The demo paginates over `subset-of` for that reason; a
rule-headed predicate such as `member-of` as the sole `:where` pattern is
rejected `:bad-args :unsupported-rule-call-ordering`.

## Tests

`lemma_client_test.clj` is a `clojure.test` suite. It rebinds the single I/O
seam `http-send` with `with-redefs` to return a canned response, so no real
network is touched. From this `clojure/` directory:

```sh
clojure -M:test
```

The `:test` alias's dependency-free `-M` path requires the test namespace, runs
`clojure.test`, and `System/exit`s nonzero on any failure, so it runs fully
offline. A `clojure -X:test` path using the cognitect test-runner also exists;
use `-M:test` if the git dep cannot resolve offline.

## Scope

This starter speaks both the HTTP and Unix-domain-socket transports,
demonstrates capabilities/limits awareness (see
[Capabilities and limits](#capabilities-and-limits)), and demonstrates cursor
pagination (see [Pagination](#pagination)). The one remaining feature —
watch/SSE streaming — is demonstrated in [`../python`](../python) and
[`../typescript`](../typescript) and may be ported here later.

## References

- `lemma_client.clj` — the tag constructors (`ent`, `wrld`, `fct`), the welcome
  parser and ServerInfo helpers (`read-welcome`, `supports?`,
  `max-message-bytes`, `within-message-limit?`), the HTTP transport
  (`http-send`, `post-edn`), the UDS transport (`uds-send-frame`,
  `uds-recv-frame`), the envelope predicates (`failure?`, `describe-failure`),
  the pagination helper (`query-all`), and the runnable `run-http` / `run-uds`
  round-trips dispatched by `-main`.
- `deps.edn` — the zero-dependency classpath and the `:test` alias.
- `../README.md` — project framing: these are from-scratch single-file recipes,
  not libraries.
