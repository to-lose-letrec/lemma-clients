# lemma-clients

Reference clients for the [Lemma](https://github.com/to-lose-letrec/lemma) wire protocol. Each subdirectory is a small, single-file demonstration that implements the protocol from scratch in one language.

## Goal

**Show that implementing a Lemma client is straightforward.**

The pitch is that anyone fluent in the language can read the demo end-to-end and see exactly what the wire interaction looks like — how the framing works, how EDN flows through, how the response envelopes parse, where the verb-call shape lives. The demos aren't packaged libraries; they're recipes. The artifact is the protocol fluency they demonstrate.

This is deliberately distinct from libraries-people-install-and-depend-on. When a language proves load-bearing under real use, its demo here graduates to a dedicated repo (`lemma-python`, etc.) with proper package-manager identity, independent versioning, and the rest of the apparatus that comes with being a library. Until then, every language stays here as a demo.

## Starter languages

Initial three, ranked by audience reach more than by author preference:

- `python/` — most likely first graduate; ML/AI integration audience lives here. At parity with `typescript/` (both transports, pagination, capabilities/limits, watch/SSE).
- `typescript/` — anything browser- or Node-side; matters for any web client. At parity with `python/` (both transports, pagination, capabilities/limits, watch/SSE).
- `clojure/` — the protocol's native language; the smallest demo by line count.

Rust and Go come later if a sustained user surfaces. Building libraries for languages that have only the maintainer as a user is maintenance burden, not an asset.

## Demo constraints

Each `<language>/` directory contains, at minimum:

- One hello-world client. Connect to a running Dianoia server, send `hello`, read `:welcome`, send `use-world`, send a trivial `query`, print results.
- A `README.md` explaining how to run it against a local Dianoia instance.
- No transitive dependency surface beyond what the standard library plus one EDN reader provides.

The dep-surface constraint exists because a client that pulls in thirty transitive packages obscures the protocol behind a framework. The goal is for a reader to see the protocol itself, not a wrapper around it.

## What's not in scope (yet)

- Rich, long-lived watch/SSE streams. The `python/` and `typescript/` demos both now show a minimal watch round-trip — a single `watch-pattern` subscription and one `:watch-event`, over both transports (interleaved on the UDS socket, on the separate SSE stream over HTTP) — so the protocol's push shape is demonstrated. The richer streaming surface stays out of scope: watch-gap / slow-consumer `:watch-closed` handling, multiple concurrent watches, and reconnection. These are nontrivial in some languages and would inflate the demos past the readability threshold.
- Authentication / TLS framing. v1 Lemma doesn't advertise TLS over the wire; deployments terminate at a reverse proxy. Demos talk to a local Dianoia over UDS or plain HTTP.
- Convenience surface (verb wrappers, pooled connections, retry semantics). These are library concerns. Demos are recipes.

## When to graduate a demo to a library

A language graduates when at least one of these is true:

- A real project (not the maintainer's) is consuming the demo as if it were a library.
- The demo has accumulated enough surface (convenience wrappers, retry logic, type definitions, async support) that the recipe-versus-library distinction stops holding.
- An independent contributor wants to maintain a packaged version.

At graduation: spin out a dedicated repo, keep the demo here as a one-screen "see `<language>-lemma` for the maintained library" pointer.
