# Python Lemma Client

A single-file, standard-library-only Python 3 client that demonstrates the
Lemma wire protocol against a local [Dianoia](https://github.com/to-lose-letrec/lemma)
server over HTTP. It carries no dependencies: a minimal EDN reader/writer is
hand-rolled in the same file. Read `lemma_client.py` end to end — it is a
recipe, not a library.

## Prerequisites

- Python 3 (standard library only; no `pip install` step).
- A running Dianoia server reachable over HTTP.

## Boot a local Dianoia

From the `dianoia` repository, with JDK 21 on `JAVA_HOME`:

```sh
LEMMA_HOME=/tmp/dianoia-worlds \
JAVA_HOME=/home/james/.local/share/jdk-21.0.11+10 \
clj -M -m dianoia.main
```

This binds an HTTP listener on `127.0.0.1:8080` and opens the world `default`.
Dianoia also exposes a Unix-domain-socket transport, but this demo talks plain
HTTP.

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

## Run

```sh
python3 lemma_client.py
```

The default base URL is `http://127.0.0.1:8080` (`DEFAULT_BASE`). Pass a
single argument to override it:

```sh
python3 lemma_client.py http://host:port
```

`main()` reads `sys.argv[1]` when present and otherwise falls back to
`DEFAULT_BASE`. No network I/O occurs at import time.

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

## How it maps to the wire

| Concern | Implementation |
|---|---|
| Body format | EDN text, encoded UTF-8 by `post_edn` |
| Content type | `content-type: application/edn` |
| Anonymous call | `POST /v1/messages` with `(hello)` |
| Named call | `POST /v1/sessions/{id}/messages`, echoing the id in the `x-lemma-session` request header |
| Session id | Read from the `X-Lemma-Session` response header |

Verb forms are EDN **lists** `( ... )`, modeled by the `Lst` wrapper so the
writer emits parentheses. Arguments are **vectors** (plain Python `list`),
maps, and tagged literals (`Tagged`) — never lists. For example `:find [?o]`
and `:where [[...]]` are vectors; only the verb head is an `Lst`.

The codec lives in the same file: `edn_write` serializes Python values to EDN,
and `edn_read` / the `_Reader` recursive-descent parser turns EDN responses
back into Python values. Keywords, symbols, and tagged literals map to the
`Keyword`, `Symbol`, and `Tagged` types. Every `#tag payload` is wrapped
uniformly as `Tagged`, so unknown tags (e.g. `#inst`) round-trip without
special-casing.

A non-2xx response still carries a valid Lemma EDN error envelope; `post_edn`
parses and returns it rather than raising, so the caller can inspect `:event`.
A connection-level failure is re-raised as a `ConnectionError` naming the base
URL.

## Tests

`test_lemma_client.py` is a standard-library `unittest` suite (no third-party
dependency — it exercises the hand-rolled codec directly). From this `python/`
directory:

```sh
python3 -m unittest test_lemma_client
```

It covers the EDN writer and reader (including round-trips, real response
envelopes, and the unknown-tag fallback) and drives `main()` over a mocked
transport so the handshake is verified without a live server.

## References

- `lemma_client.py` — the client, codec, transport, and runnable `main()`.
- `../README.md` — project framing: these are from-scratch single-file
  recipes, not libraries.
