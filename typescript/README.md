# TypeScript Lemma Client

A single-file, Node-compatible TypeScript client that demonstrates the Lemma
wire protocol against a local [Dianoia](https://github.com/to-lose-letrec/lemma)
server. It speaks **both** transports Dianoia exposes: HTTP (the default) and a
Unix domain socket. It uses the `jsedn` library for the EDN codec; HTTP rides
the platform's global `fetch` and UDS rides Node's `net` module. The same
`edn.encode` / `edn.parse` codec serializes both transports. This is the
browser/Node-side counterpart to the [`python/`](../python) demo. Read
`lemma_client.ts` end to end: it is a recipe, not a library.

## Prerequisites

- A JavaScript runtime: Node (>= 18, for global `fetch`) or Bun. The demo is
  verified with Bun in this environment, and the code is Node-compatible — it
  uses global `fetch` plus `jsedn`, and the only runtime-specific touch is the
  `import.meta.main` guard at the bottom of `lemma_client.ts`, which is harmless
  under Node.
- A running Dianoia server reachable over HTTP.
- Install the one dependency (`jsedn`):

  ```sh
  bun install
  # or: npm install
  ```

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
create the world directory out-of-band:

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
bun run lemma_client.ts
```

The client also runs under Node (`node lemma_client.ts`). The default base URL
is `http://127.0.0.1:8080` (`DEFAULT_BASE`). Pass a single URL argument to
override it:

```sh
bun run lemma_client.ts http://host:port
```

## Run (UDS)

```sh
bun run lemma_client.ts uds
```

The `uds` argument selects the Unix-domain-socket transport and connects to
`/tmp/dianoia.sock` (`DEFAULT_SOCKET`). Pass a path after `uds` to override the
socket:

```sh
bun run lemma_client.ts uds /path/to/dianoia.sock
```

`_dispatch` reads `process.argv.slice(2)`: a leading `uds` runs `main_uds(...)`,
any other argument is an HTTP base URL passed to `main(...)`, and no argument
falls back to `main(DEFAULT_BASE)`. No network I/O occurs at import time.

## What it does

`main()` runs one linear round-trip, printing a single line per step. After each
response the code inspects `:event`; an `:error` or `:rejected` envelope is
printed via `describeFailure` and the sequence stops cleanly.

1. `(hello)` — anonymous `POST /v1/messages`. The `:welcome` reply carries the
   new session id in the `X-Lemma-Session` response header, surfaced by
   `post_edn`.
2. `(use-world #world "default")` — enter the world on the session.
3. `(propose #fact{...})` — propose `equivalent morningstar venus`. The reply
   returns a `#proposal` handle.
4. `(assert <proposal>)` — assert the proposed fact into the world; the
   `#proposal` handle round-trips back onto the wire untouched.
5. `(query {:find [?o] :where [[equivalent #entity "morningstar" ?o]]})` —
   query it back and print `:rows` and `:done?`.

Expected output (the session and proposal ids increment per run):

```text
hello -> :welcome  version=1  session=s-1  world=#world "default"
use-world "default" -> :world-selected  world=#world "default"
propose (equivalent morningstar venus) -> :proposed  proposal=#proposal "p-1"
assert proposal -> :asserted
query (equivalent morningstar ?o) -> rows=[["venus"]]  done?=true
```

The query binds `?o` to the matching entity's name, which Dianoia returns as a
plain string — hence `rows=[["venus"]]`, not a tagged `#entity` literal.

`main_uds()` runs the identical verb sequence and prints the same per-step
lines; only the transport differs.

## How HTTP maps to the wire

Request and response bodies are EDN text, encoded and parsed by `jsedn`
(`edn.encode` / `edn.parse`). The verb forms are EDN **lists**
(`new edn.List([...])`); the arguments inside are **vectors**, **maps**, and
**tagged literals** — only the verb head is a list. The core Lemma tags round-
trip through `jsedn` natively, with no reader registration: `edn.parse` yields
an `edn.Tagged` and `edn.encode` re-emits the exact `#tag payload` wire text, so
a `#proposal` handed back by the server feeds straight into the next `(assert
...)`. The `entity`, `world`, and `fact` helpers build the `#entity`, `#world`,
and `#fact{...}` forms.

| Concern | Implementation |
|---|---|
| Body format | EDN text via `jsedn` |
| Content type | `content-type: application/edn` |
| Anonymous call | `POST /v1/messages` with `(hello)` |
| Named call | `POST /v1/sessions/{id}/messages`, echoing the id in the `x-lemma-session` request header |
| Session id | Read from the `X-Lemma-Session` response header |

A non-2xx response still carries a valid Lemma EDN error envelope; `post_edn`
parses and returns it rather than throwing, so the caller inspects `:event`. A
connection-level failure is re-raised as an `Error` naming the base URL.

## How UDS maps to the wire

UDS carries the same EDN over a single persistent Node `net` socket, with
explicit framing instead of an HTTP envelope: a **4-byte big-endian length**
prefix followed by that many **UTF-8 bytes** of EDN. `uds_send_frame` writes the
frame (`frame.writeUInt32BE(body.length, 0)` then the body); `uds_recv_frame`
reads one frame back through a `FrameReader`, which reassembles the length, the
body, and the UTF-8 decode across however the socket chunked the bytes.

The session handling is the key contrast with HTTP. Over UDS the **session is
bound to the connection**: the server captures the id from the `:welcome`
envelope and pins it to the socket, so the client sends no `X-Lemma-Session`
header and never echoes a session id back — it just keeps sending frames on the
same socket. (Over HTTP the id is threaded explicitly through the
`X-Lemma-Session` header and the named `/v1/sessions/{id}/messages` endpoint.)

| Concern | Implementation |
|---|---|
| Body format | EDN text, encoded UTF-8 by `uds_send_frame` |
| Framing | 4-byte big-endian length prefix, then the body bytes |
| Connection | One persistent `net` socket for every frame |
| Anonymous call | `(hello)` as the first frame |
| Session id | Bound to the connection by the server; never echoed by the client |

A connect-time failure (no listener at the path) is re-raised as an `Error`
naming the socket path; the socket is always closed on the way out.

## Tests

`lemma_client.test.ts` is a `bun:test` suite. Run it from this `typescript/`
directory:

```sh
bun test
```

It covers the `post_edn` request shape and error-envelope recovery, drives
`main()` over a scripted `fetch` so the handshake is verified without a live
server, and asserts the verb forms encode to grammar-valid wire text.

## Scope note

Both the HTTP and Unix-domain-socket transports are now supported. The richer
features — cursor pagination, capabilities/limits negotiation, and watch/SSE
streaming — are still pending here; they are demonstrated in
[`../python`](../python) and may be ported later.

## References

- `lemma_client.ts` — the `jsedn` constructor helpers (`entity`, `world`,
  `fact`), the envelope readers, both transports (`post_edn` for HTTP,
  `uds_send_frame` / `uds_recv_frame` / `FrameReader` for UDS), and the runnable
  `main()` / `main_uds()` round-trips dispatched by `_dispatch`.
- [`jsedn`](https://www.npmjs.com/package/jsedn) — the EDN reader/writer the
  codec is built on.
- [`../python/`](../python) — the reference implementation covering the full
  feature set.
- `../README.md` — project framing: these are from-scratch single-file recipes,
  not libraries.
