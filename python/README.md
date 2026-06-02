# Python Lemma Client

A single-file Python 3 client that demonstrates the Lemma wire protocol against
a local [Dianoia](https://github.com/to-lose-letrec/lemma) server. It speaks
**both** transports Dianoia exposes: HTTP (the default) and a Unix domain
socket. It leans on one third-party library — the `edn_format` EDN
reader/writer — for the wire codec; everything else is the standard library.
The same `edn_format.loads` / `edn_format.dumps` codec serializes both
transports. Read `lemma_client.py` end to end — it is a recipe, not a library.

## Prerequisites

- Python 3.
- `edn_format`, the only third-party dependency:

  ```sh
  pip install edn_format
  ```

  Everything else the client uses is the standard library. This matches the
  parent project's "standard library plus one EDN reader" demo budget.
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
python3 lemma_client.py
```

The default base URL is `http://127.0.0.1:8080` (`DEFAULT_BASE`). Pass a
single URL argument to override it:

```sh
python3 lemma_client.py http://host:port
```

## Run (UDS)

```sh
python3 lemma_client.py uds
```

The `uds` argument selects the Unix-domain-socket transport and connects to
`/tmp/dianoia.sock` (`DEFAULT_SOCKET`). Pass a path after `uds` to override the
socket:

```sh
python3 lemma_client.py uds /path/to/dianoia.sock
```

The `__main__` dispatch reads `sys.argv`: a leading `uds` runs `main_uds(...)`,
any other argument is an HTTP base URL passed to `main(...)`, and no argument
falls back to `main(DEFAULT_BASE)`. No network I/O occurs at import time.

## What it does

`main()` runs one linear propose/assert/query round-trip, printing a single
line per step:

1. `(hello)` — anonymous `POST /v1/messages`. The `:welcome` reply carries the
   new session id in the `X-Lemma-Session` response header, surfaced by
   `post_edn`.
2. `(use-world #world "default")` — enter the world on the session.
3. `(propose #fact{...})` — propose `equivalent morningstar venus`. The reply
   returns a `#proposal` handle.
4. `(assert <proposal>)` — assert the proposed fact into the world.
5. `(query {:find [?o] :where [[equivalent #entity "morningstar" ?o]]})` —
   query it back and print `:rows` and `:done?`.

After every response the code inspects `:event`; an `:error` or `:rejected`
envelope is printed via `_describe_failure` and the sequence stops cleanly.

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
Tagged handles such as `#world` and `#proposal` do appear in the other replies
shown above.

`main_uds()` runs the identical verb sequence and prints the same per-step
lines; only the transport differs.

## How HTTP maps to the wire

| Concern | Implementation |
|---|---|
| Body format | EDN text, encoded UTF-8 by `post_edn` |
| Content type | `content-type: application/edn` |
| Anonymous call | `POST /v1/messages` with `(hello)` |
| Named call | `POST /v1/sessions/{id}/messages`, echoing the id in the `x-lemma-session` request header |
| Session id | Read from the `X-Lemma-Session` response header |

## How UDS maps to the wire

UDS carries the same EDN over a single persistent connection, with explicit
framing instead of an HTTP envelope: a **4-byte big-endian length** prefix
followed by that many **UTF-8 bytes** of EDN. `uds_send_frame` writes the frame
(`struct.pack(">I", len(body))` then the body); `uds_recv_frame` reads the
length, then exactly that many body bytes, then decodes UTF-8.

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
| Connection | One persistent `AF_UNIX` socket for every frame |
| Anonymous call | `(hello)` as the first frame |
| Session id | Bound to the connection by the server; never echoed by the client |

## EDN encoding

The client does not hand-roll a parser. It calls `edn_format.dumps` to encode
Python values to EDN text and `edn_format.loads` to parse responses back. The
Python-to-EDN type mapping is:

| EDN | Python (`edn_format`) |
|---|---|
| List `( ... )` — verb forms only | `tuple` |
| Vector `[ ... ]` — the common case | `list` |
| Map `{ k v }` | `dict` (read back as `ImmutableDict`) |
| Keyword `:event` | `edn_format.Keyword` |
| Symbol `equivalent`, `?o` | `edn_format.Symbol` |
| Tagged literal `#tag payload` | `edn_format.TaggedElement` |

The list/vector split is the one design decision the codec leans on: a **list**
`( ... )` appears only as the top-level verb form (`(propose ...)`,
`(query ...)`, `(hello)`), so it maps to a Python `tuple`; every collection
inside the arguments is a **vector** `[ ... ]`, mapping to a Python `list`.
For example `:find [?o]` and `:where [[...]]` are lists (vectors); only the
verb head is a tuple.

The ten core Lemma tagged literals — `#entity #world #fact #violation
#proposal #tx #ref #cursor #watch #session` — are registered as
`TaggedElement` classes so they round-trip in both directions: `loads`
reconstructs the object from wire text and `dumps` re-emits the exact wire
text. The eight string-payload tags (`#entity`, `#world`, `#proposal`, `#tx`,
`#ref`, `#cursor`, `#watch`, `#session`) share one `_Handle` class; `#fact` and
`#violation` carry a map. Tags that are not registered (e.g. `#inst`) fall back
to `edn_format`'s built-in handlers, so an unexpected tag never breaks a
response.

Thin constructor helpers keep the round-trip code readable: `entity(name)` and
`world(name)` build the corresponding `#entity` / `#world` handles, and
`fact(predicate, subject, object)` builds a `#fact{...}` binary fact whose
`:predicate` is a `Symbol` and whose `:subject` / `:object` are typically
`#entity` handles.

A non-2xx response still carries a valid Lemma EDN error envelope; `post_edn`
parses and returns it rather than raising, so the caller can inspect `:event`.
A connection-level failure is re-raised as a `ConnectionError`: `post_edn` names
the base URL, and `main_uds` names the socket path.

## Tests

`test_lemma_client.py` is a `unittest` suite. It imports `lemma_client`, so
`edn_format` must be installed (see Prerequisites). From this `python/`
directory:

```sh
python3 -m unittest test_lemma_client
```

It covers the `edn_format` round-trips (including real response envelopes and
the unregistered-tag fallback) and drives `main()` over a mocked transport so
the handshake is verified without a live server.

## References

- `lemma_client.py` — the `edn_format` tag registrations and constructor
  helpers, both transports (`post_edn` for HTTP, `uds_send_frame` /
  `uds_recv_frame` for UDS), and the runnable `main()` / `main_uds()`
  round-trips.
- [`edn_format`](https://pypi.org/project/edn_format/) — the EDN reader/writer
  the codec is built on.
- `../README.md` — project framing: these are from-scratch single-file
  recipes, not libraries.
