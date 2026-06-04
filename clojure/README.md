# Clojure Lemma Client

A single-file Clojure client that demonstrates the Lemma wire protocol against a
local [Dianoia](https://github.com/to-lose-letrec/lemma) server over HTTP.
Clojure is the protocol's **native** language: Lemma speaks EDN (Clojure's own
data syntax), so there is **zero** to install. `clojure.edn` is the codec and
`java.net.http` (JDK 11+) is the transport — both built in. `deps.edn` carries
no client dependencies. This is the smallest of the demos. Read
`lemma_client.clj` end to end; it is a recipe, not a library.

## Prerequisites

- The Clojure CLI (`clojure` / `clj`) and a JDK. The client needs JDK 11+ for
  `java.net.http`; the system Java is fine. Only Dianoia itself needs JDK 21.
- A running Dianoia server reachable over HTTP.

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

Loading the file performs no network I/O; only `-main` touches the network.

## What it does

`-main` runs one linear round-trip, printing a single line per step:

1. `(hello)` — anonymous `POST /v1/messages`. The `:welcome` reply carries the
   new session id in the `X-Lemma-Session` response header, which `post-edn`
   surfaces.
2. `(use-world #world "default")` — enter the world on the session.
3. `(propose #fact{...})` — propose `equivalent morningstar venus`. The reply
   returns a `#proposal` handle in `:proposal`.
4. `(assert <proposal>)` — assert the proposed fact into the world.
5. `(query {:find [?o] :where [[equivalent #entity "morningstar" ?o]]})` — query
   it back and print `:rows` and `:done?`.

After every reply the code checks `:event`; an `:error` or `:rejected` envelope
is printed via `describe-failure` and the sequence stops cleanly. A
connection-level failure (server down/refused) is re-thrown by `post-edn` as an
`ex-info` naming the base URL; `-main` catches it, prints the actionable line,
and exits nonzero.

Expected output (the session and proposal ids increment per run):

```text
hello -> :welcome  version=1  session=s-1  world=nil
use-world "default" -> :world-selected  world=#world "default"
propose (equivalent morningstar venus) -> :proposed  proposal=#proposal "p-1"
assert proposal -> :asserted
query (equivalent morningstar ?o) -> rows=[["venus"]]  done?=true
```

The query binds `?o` to the matching entity's name, which Dianoia returns as a
plain string — hence `rows=[["venus"]]`, not a tagged `#entity` literal.

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

This is a hello-world starter. Richer features — the Unix-domain-socket
transport, cursor pagination, capabilities/limits negotiation, and watch/SSE
streaming — are demonstrated in [`../python`](../python) and
[`../typescript`](../typescript) and may be ported here later.

## References

- `lemma_client.clj` — the tag constructors (`ent`, `wrld`, `fct`), the HTTP
  transport (`http-send`, `post-edn`), the envelope predicates (`failure?`,
  `describe-failure`), and the runnable `-main` round-trip.
- `deps.edn` — the zero-dependency classpath and the `:test` alias.
- `../README.md` — project framing: these are from-scratch single-file recipes,
  not libraries.
