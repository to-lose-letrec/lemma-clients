/**
 * A single-file TypeScript client for the Lemma wire protocol.
 *
 * This module is a *recipe*, not a library: it is meant to be read end to end.
 * The first thing any Lemma client needs is a way to turn host values into the
 * EDN text the server speaks, and to turn the server's EDN responses back into
 * host values. Rather than hand-roll that codec, this client leans on the
 * `jsedn` library (the one third-party dependency); everything else is the
 * platform's global `fetch`. On top of the codec sit an HTTP transport and a
 * runnable `main()` that walks the full hello/propose/assert/query round-trip.
 *
 * It is written to run unchanged on Node (>= 18, for global `fetch`) and on
 * Bun. The only runtime-specific touch is the `import.meta.main` guard at the
 * bottom, which Bun sets and Node leaves undefined (harmless either way).
 *
 * EDN in a nutshell
 * -----------------
 * EDN (Extensible Data Notation) is Clojure's data syntax. Lemma uses a small,
 * well-defined subset of it (see `lemma/grammar/lemma.lark`). The pieces we
 * care about and their `jsedn` mappings:
 *
 *     nil true false              -- null / true / false
 *     42  -3  3.14                -- number
 *     "a string\n"               -- string
 *     :event  :verbs/core         -- edn.Keyword
 *     equivalent  member-of  ?o   -- edn.Symbol (`?`-vars are symbols too)
 *     ( a b c )                   -- edn.List   (an EDN LIST)
 *     [ a b c ]                   -- edn.Vector (an EDN VECTOR)
 *     { k v, k v }                -- edn.Map
 *     #tag payload                -- edn.Tagged
 *
 * Lists versus vectors -- the one design decision
 * -----------------------------------------------
 * EDN distinguishes lists `( ... )` from vectors `[ ... ]`, and Lemma relies
 * on the distinction (grammar §3): a *list* appears **only** as the top-level
 * verb form -- `(propose ...)`, `(query ...)`, `(hello)`. Everywhere inside
 * the arguments, collections are *vectors* (`:find [?x]`, `:where [[...]]`),
 * maps, or sets -- never lists. `jsedn` encodes this distinction in its type
 * system: `edn.List` round-trips to `( ... )`, `edn.Vector` to `[ ... ]`, so
 * we simply pick the right constructor rather than inventing a wrapper.
 *
 * Tagged literals
 * ---------------
 * The core Lemma tags (`#fact #entity #world #proposal ...`, grammar §5)
 * round-trip through `jsedn` with NO reader registration: `edn.parse` yields
 * an `edn.Tagged` carrying the tag name and payload, and `edn.encode` re-emits
 * the exact `#tag payload` wire text. So a `#proposal "p-1"` handed back by the
 * server can be fed straight into the next `(assert ...)` untouched, and the
 * `#fact{...}` / `#entity "..."` / `#world "..."` forms we build with
 * `edn.Tagged` encode to grammar-valid strings.
 *
 * Importing this module performs no network I/O -- only `main` (and the
 * `import.meta.main` guard) touch the network.
 */

import * as edn from "jsedn";
import * as net from "node:net";

// ---------------------------------------------------------------------------
// Tagged-literal & collection constructors
//
// Thin wrappers so the round-trip code below reads as prose rather than as a
// wall of `new edn.Tagged(new edn.Tag(...), ...)` constructions. They mirror
// the grammar's payload shapes. `edn.Tagged`'s first argument MUST be an
// `edn.Tag` instance (a bare string throws on encode), so we build one here.
// ---------------------------------------------------------------------------

/** Build a `#<tag> <payload>` tagged literal, e.g. `#entity "alice"`. */
function tagged(tag: string, payload: unknown): edn.Tagged {
  return new edn.Tagged(new edn.Tag(tag), payload);
}

/** Build an `#entity "<name>"` handle (grammar §5.3). */
export function entity(name: string): edn.Tagged {
  return tagged("entity", name);
}

/** Build a `#world "<name>"` handle (grammar §5). */
export function world(name: string): edn.Tagged {
  return tagged("world", name);
}

/**
 * Build a `#fact {...}` binary fact: `(predicate subject object)` (grammar
 * §5.1). `predicate` is a Symbol; `subject` / `object` are typically `#entity`
 * handles. The keys are the grammar's reserved fact keys. The payload is an
 * `edn.Map` so it encodes as `{:predicate ... :subject ... :object ...}`.
 */
export function fact(
  predicate: edn.Symbol,
  subject: unknown,
  object: unknown,
): edn.Tagged {
  return tagged(
    "fact",
    new edn.Map([
      new edn.Keyword(":predicate"), predicate,
      new edn.Keyword(":subject"), subject,
      new edn.Keyword(":object"), object,
    ]),
  );
}

// ---------------------------------------------------------------------------
// Envelope helpers:  reading keyword-keyed maps & comparing keywords
//
// Every Lemma reply is an EDN map keyed by keywords. `jsedn`'s `Map.at` THROWS
// when a key is absent, so we read through guarded helpers that return a
// default instead. Keyword identity is compared by canonical text (`:event`
// vs `:welcome`), which `edn.encode` renders deterministically -- the robust
// way to compare two `edn.Keyword`s without relying on object identity.
// ---------------------------------------------------------------------------

const KW_EVENT = new edn.Keyword(":event");

/** Canonical text of a keyword/value (`:welcome`), used for equality + display. */
function kwText(value: unknown): string {
  try {
    return edn.encode(value);
  } catch {
    return String(value);
  }
}

/** True iff `value` is an `edn.Keyword` whose text equals `name` (e.g. ":welcome"). */
function isKeyword(value: unknown, name: string): boolean {
  return value instanceof edn.Keyword && kwText(value) === name;
}

/** True iff `body` is an `edn.Map` (the shape every Lemma reply takes). */
function isMap(body: unknown): body is edn.Map {
  return body instanceof edn.Map;
}

/**
 * Read `:<key>` from a reply map, or return `fallback` if the body is not a
 * map or the key is absent. Wraps `Map.at`, which would otherwise throw on a
 * missing key.
 */
function field(body: unknown, key: string, fallback: unknown = null): unknown {
  if (!isMap(body)) return fallback;
  const k = new edn.Keyword(key);
  return body.exists(k) ? body.at(k) : fallback;
}

/** The `:event` keyword of a reply, or `null` if absent / not a map. */
function eventOf(body: unknown): unknown {
  return field(body, ":event", null);
}

/**
 * Format the salient parts of an `:error` / `:rejected` envelope for printing.
 * Pulls whichever of `:reason` / `:message` the server included -- the fields
 * that explain *why* a call was refused.
 */
function describeFailure(body: unknown): string {
  const parts: string[] = [];
  for (const key of [":reason", ":message"]) {
    if (isMap(body) && body.exists(new edn.Keyword(key))) {
      parts.push(`${key} ${edn.encode(body.at(new edn.Keyword(key)))}`);
    }
  }
  return parts.length ? parts.join("; ") : "(no detail provided)";
}

/** True iff the reply's `:event` is `:error` or `:rejected`. */
function isFailure(body: unknown): boolean {
  const event = eventOf(body);
  return isKeyword(event, ":error") || isKeyword(event, ":rejected");
}

// ---------------------------------------------------------------------------
// HTTP transport:  EDN form  ->  POST  ->  parsed EDN response
//
// With the codec in hand, talking to a Lemma server is just "encode, POST,
// decode". A flat helper over the global `fetch`, not an abstraction. The
// session protocol (SPEC §3) is:
//
//   * The first call is anonymous: POST /v1/messages with (hello). The
//     :welcome response carries the new session id in the X-Lemma-Session
//     response header.
//   * Subsequent calls reuse that id by echoing it back in the x-lemma-session
//     request header (and posting to the named endpoint).
//
// This helper handles one round-trip; the caller threads the returned session
// id into the next call. See examples/hello-http.clj for the full
// propose/assert/query sequence this enables.
// ---------------------------------------------------------------------------

/** Where a locally booted Dianoia HTTP listener lives by default. */
export const DEFAULT_BASE = "http://127.0.0.1:8080";

/** One round-trip's result: the parsed reply and the session id header. */
export interface PostResult {
  /** The parsed EDN response (typically an `edn.Map` envelope). */
  body: unknown;
  /** The `X-Lemma-Session` response header value, or `null` if absent. */
  sessionId: string | null;
}

/**
 * POST an EDN `form` to `base + path` and return `{ body, sessionId }`.
 *
 * `form` is any value `edn.encode` accepts -- typically an `edn.List` verb
 * call such as `(hello)`. It is encoded to EDN text, sent as `application/edn`,
 * and the response text is parsed back into host values with `edn.parse`.
 *
 * Error handling
 * --------------
 * An HTTP error status (4xx/5xx) still carries a valid Lemma EDN *error
 * envelope* in its body, so we parse and return that as `body` rather than
 * discarding it -- the caller inspects `:event` to tell a welcome from an
 * error. A connection-level failure (server down, refused) is re-raised as an
 * `Error` that names the `base` URL so the failure is actionable.
 */
export async function post_edn(
  path: string,
  form: unknown,
  session?: string | null,
  base: string = DEFAULT_BASE,
): Promise<PostResult> {
  const body = edn.encode(form);

  const headers: Record<string, string> = { "content-type": "application/edn" };
  if (session) headers["x-lemma-session"] = session;

  let res: Response;
  try {
    res = await fetch(base + path, { method: "POST", headers, body });
  } catch (err) {
    // No HTTP status at all -- we never reached a Lemma server. Name the
    // endpoint so the failure is actionable rather than a bare network error.
    const reason = err instanceof Error ? err.message : String(err);
    throw new Error(
      `could not reach the Lemma server at ${base} (${reason}); ` +
        "is the server running?",
    );
  }

  // Both 2xx and non-2xx replies carry a structured EDN envelope: a non-2xx is
  // an error envelope, not a transport failure, so we parse and return it too
  // (the caller reads :event). The session header is surfaced regardless.
  const text = await res.text();
  return { body: edn.parse(text), sessionId: res.headers.get("x-lemma-session") };
}

// ---------------------------------------------------------------------------
// UDS transport:  EDN form  ->  length-prefixed frame  ->  parsed EDN response
//
// A second transport that speaks the same EDN codec over a Unix domain socket
// instead of HTTP. It sits alongside post_edn rather than replacing it -- same
// "encode, send, decode" shape, different plumbing. Two things differ from HTTP:
//
//   * Framing. There is no HTTP envelope, so each message is delimited
//     explicitly: a 4-byte big-endian UNSIGNED length prefix followed by that
//     many UTF-8 bytes of EDN. This matches Dianoia's transport/uds.clj
//     write-frame / read-frame exactly (DataOutputStream.writeInt is a 4-byte
//     big-endian int).
//   * Session binding. Over HTTP the client threads the session id back into
//     each request header. Over UDS the server binds the session to the
//     *connection*: it captures the id from the welcome envelope and attaches
//     it to the socket (see uds.clj handle-frame / build-ctx). So the client
//     must NOT echo the session id into later frames -- it just keeps sending
//     on the same socket, and the server already knows who it is.
//
// Node's `net` sockets are streaming, not message-oriented: `'data'` arrives as
// arbitrary Buffer chunks that may split one frame across reads or coalesce
// several frames into one. `FrameReader` below absorbs that, handing back one
// length-prefixed frame at a time as a Promise. The only plumbing dependency is
// node:net; the EDN codec is still jsedn.
// ---------------------------------------------------------------------------

/** Where a locally booted Dianoia UDS listener binds by default (uds.clj). */
export const DEFAULT_SOCKET = "/tmp/dianoia.sock";

/**
 * A length-prefixed frame demultiplexer over a streaming Node socket.
 *
 * Node delivers `'data'` as raw Buffer chunks with no respect for our message
 * boundaries: a single frame may straddle several chunks, and several frames
 * may land in one chunk. So we buffer every byte that arrives and, on each
 * `recv()` request, try to peel exactly one frame off the front -- a 4-byte
 * big-endian length followed by that many body bytes -- leaving the remainder
 * buffered for the next call.
 *
 * `recv()` returns a Promise that resolves with one frame's UTF-8 body. If a
 * whole frame is already buffered it resolves synchronously on the microtask
 * queue; otherwise it parks until enough bytes arrive. Exactly one `recv()` may
 * be outstanding at a time, which matches the strictly request/response UDS
 * protocol (one frame in, one frame out). EOF before a full frame -- the socket
 * emitting `'end'`, `'close'`, or `'error'` -- rejects the pending `recv()` (and
 * every later one) so a truncated reply surfaces as an error rather than hanging.
 */
class FrameReader {
  private chunks: Buffer = Buffer.alloc(0);
  private pending: {
    resolve: (body: string) => void;
    reject: (err: Error) => void;
  } | null = null;
  private failure: Error | null = null;

  constructor(socket: net.Socket) {
    socket.on("data", (chunk: Buffer) => {
      this.chunks = Buffer.concat([this.chunks, chunk]);
      this.deliver();
    });
    // Any of these means the peer is gone. A still-pending recv() can never be
    // satisfied, so fail it (and remember the failure for later recv() calls).
    socket.on("end", () => this.fail("connection closed by peer (EOF)"));
    socket.on("close", () => this.fail("connection closed"));
    socket.on("error", (err: Error) => this.fail(err.message));
  }

  /** Resolve with the next length-prefixed frame body, or reject on EOF/error. */
  recv(): Promise<string> {
    if (this.pending) {
      return Promise.reject(
        new Error("FrameReader.recv() called while a read is already pending"),
      );
    }
    return new Promise<string>((resolve, reject) => {
      this.pending = { resolve, reject };
      // Bytes may already be buffered (a coalesced frame, or one that arrived
      // before this call); try to satisfy immediately. Otherwise we've already
      // seen EOF/error -- fail now rather than wait for a chunk that won't come.
      if (!this.deliver() && this.failure) {
        this.pending = null;
        reject(this.failure);
      }
    });
  }

  /**
   * If a waiter is parked and a full frame is buffered, hand it over and
   * consume those bytes. Returns true iff a frame was delivered.
   */
  private deliver(): boolean {
    if (!this.pending) return false;
    if (this.chunks.length < 4) return false;
    const length = this.chunks.readUInt32BE(0);
    if (this.chunks.length < 4 + length) return false;

    const body = this.chunks.subarray(4, 4 + length).toString("utf-8");
    this.chunks = this.chunks.subarray(4 + length);
    const waiter = this.pending;
    this.pending = null;
    waiter.resolve(body);
    return true;
  }

  /** Record an EOF/error and reject any parked recv() with it. */
  private fail(message: string): void {
    if (!this.failure) this.failure = new Error(message);
    if (this.pending) {
      const waiter = this.pending;
      this.pending = null;
      waiter.reject(this.failure);
    }
  }
}

/**
 * Frame `ednStr` and write it: a 4-byte big-endian length prefix, then the
 * UTF-8 body. Mirrors uds.clj write-frame (DataOutputStream.writeInt is a
 * 4-byte big-endian int). `socket.write` buffers internally, so a single call
 * puts the whole frame on the wire in order.
 */
export function uds_send_frame(socket: net.Socket, ednStr: string): void {
  const body = Buffer.from(ednStr, "utf-8");
  const frame = Buffer.alloc(4 + body.length);
  frame.writeUInt32BE(body.length, 0);
  body.copy(frame, 4);
  socket.write(frame);
}

/**
 * Read one length-prefixed frame from `reader` and return its body as a string.
 *
 * The inverse of `uds_send_frame`: the `FrameReader` already handles the 4-byte
 * length, the body read, and the UTF-8 decode -- including frames split across
 * or coalesced within socket chunks -- so this is a thin, named await over it
 * that mirrors python's uds_recv_frame.
 */
export function uds_recv_frame(reader: FrameReader): Promise<string> {
  return reader.recv();
}

// ---------------------------------------------------------------------------
// Runnable recipe:  the full Lemma round-trip
//
// A flat, linear retelling of examples/hello-http.clj: say hello, enter a
// world, propose a fact, assert it, query it back. Each step prints one
// human-readable line so a reader can follow the wire conversation by running
// the file. Everything network-y lives here (or in the import.meta.main
// guard) -- importing the module performs no I/O. After each response we
// inspect `:event`; an `:error` / `:rejected` envelope is printed and the
// sequence stops cleanly rather than crashing.
// ---------------------------------------------------------------------------

/** Run the full hello/propose/assert/query round-trip against a Lemma server. */
export async function main(base: string = DEFAULT_BASE): Promise<void> {
  // 1. Anonymous hello. The welcome reply carries the new session id in the
  //    X-Lemma-Session response header, which post_edn surfaces for us.
  const hello = await post_edn(
    "/v1/messages",
    new edn.List([new edn.Symbol("hello")]),
    null,
    base,
  );
  const welcome = hello.body;
  const sid = hello.sessionId;
  if (!isKeyword(eventOf(welcome), ":welcome")) {
    console.log(
      `hello: expected :welcome, got ${kwText(eventOf(welcome))}` +
        ` -- ${describeFailure(welcome)}`,
    );
    return;
  }
  console.log(
    `hello -> :welcome  version=${kwText(field(welcome, ":version"))}` +
      `  session=${sid}  world=${kwText(field(welcome, ":world"))}`,
  );

  // 2. Every later call rides the same session: post to the named endpoint and
  //    echo the session id back in the request header.
  const named = (form: unknown) =>
    post_edn(`/v1/sessions/${sid}/messages`, form, sid, base);

  // 3. Enter the world. (use-world #world "default")
  let res = await named(new edn.List([new edn.Symbol("use-world"), world("default")]));
  if (isFailure(res.body)) {
    console.log(`use-world refused: ${describeFailure(res.body)}`);
    return;
  }
  console.log(
    `use-world "default" -> ${kwText(eventOf(res.body))}` +
      `  world=${kwText(field(res.body, ":world"))}`,
  );

  // 4. Propose a fact: morningstar is equivalent to venus. The reply hands
  //    back a #proposal handle we feed straight into the assert.
  const f = fact(new edn.Symbol("equivalent"), entity("morningstar"), entity("venus"));
  res = await named(new edn.List([new edn.Symbol("propose"), f]));
  if (isFailure(res.body)) {
    console.log(`propose refused: ${describeFailure(res.body)}`);
    return;
  }
  const proposal = field(res.body, ":proposal");
  console.log(
    `propose (equivalent morningstar venus) -> ${kwText(eventOf(res.body))}` +
      `  proposal=${kwText(proposal)}`,
  );

  // 5. Assert the proposed fact into the world. The #proposal handle from the
  //    propose reply round-trips back onto the wire untouched.
  res = await named(new edn.List([new edn.Symbol("assert"), proposal]));
  if (isFailure(res.body)) {
    console.log(`assert refused: ${describeFailure(res.body)}`);
    return;
  }
  console.log(`assert proposal -> ${kwText(eventOf(res.body))}`);

  // 6. Query it back. Note :find / :where are VECTORS, and the where-clause is
  //    a vector of vectors; only the verb head is a List. Query variables like
  //    ?o are Symbols.
  const query = new edn.List([
    new edn.Symbol("query"),
    new edn.Map([
      new edn.Keyword(":find"), new edn.Vector([new edn.Symbol("?o")]),
      new edn.Keyword(":where"), new edn.Vector([
        new edn.Vector([
          new edn.Symbol("equivalent"),
          entity("morningstar"),
          new edn.Symbol("?o"),
        ]),
      ]),
    ]),
  ]);
  res = await named(query);
  if (isFailure(res.body)) {
    console.log(`query refused: ${describeFailure(res.body)}`);
    return;
  }
  console.log(
    `query (equivalent morningstar ?o) -> rows=${kwText(field(res.body, ":rows"))}` +
      `  done?=${kwText(field(res.body, ":done?"))}`,
  );
}

/**
 * Run the same hello/propose/assert/query round-trip over a Unix domain socket.
 *
 * Step for step this is the HTTP `main` -- hello, enter a world, propose a
 * fact, assert it, query it back -- but spoken over a UDS frame stream. The one
 * protocol difference is session handling: the server binds the session to the
 * connection from the welcome envelope (uds.clj handle-frame), so we do NOT
 * thread the session id into later frames. Every call after hello simply rides
 * the same open socket.
 *
 * After each response we check `:event`; an `:error` / `:rejected` envelope is
 * printed and the sequence stops cleanly rather than crashing. The socket is
 * always closed on the way out, success or failure.
 */
export async function main_uds(socketPath: string = DEFAULT_SOCKET): Promise<void> {
  // Connect and wait for the connection (or a connect-time error). An ENOENT /
  // ECONNREFUSED before 'connect' means no listener at the path -- reject with
  // an Error naming it so the failure is actionable rather than a bare errno.
  const socket = net.createConnection({ path: socketPath });
  const reader = new FrameReader(socket);
  await new Promise<void>((resolve, reject) => {
    socket.once("connect", resolve);
    socket.once("error", (err: Error) =>
      reject(
        new Error(
          `could not connect to the Lemma UDS server at ${socketPath} ` +
            `(${err.message}); is the server running?`,
        ),
      ),
    );
  });

  try {
    // One round-trip: frame out, frame in, decode. The session lives on the
    // connection -- no id is echoed back, unlike the HTTP transport.
    const call = async (form: unknown): Promise<unknown> => {
      uds_send_frame(socket, edn.encode(form));
      return edn.parse(await uds_recv_frame(reader));
    };

    // 1. Anonymous hello. The welcome reply carries the session id, which the
    //    server has already pinned to this connection for us; we read it for
    //    display only and do NOT echo it into later frames.
    const welcome = await call(new edn.List([new edn.Symbol("hello")]));
    if (!isKeyword(eventOf(welcome), ":welcome")) {
      console.log(
        `hello: expected :welcome, got ${kwText(eventOf(welcome))}` +
          ` -- ${describeFailure(welcome)}`,
      );
      return;
    }
    console.log(
      `hello -> :welcome  version=${kwText(field(welcome, ":version"))}` +
        `  session=${kwText(field(welcome, ":session"))}` +
        `  world=${kwText(field(welcome, ":world"))}`,
    );

    // 2. Enter the world. (use-world #world "default")
    let body = await call(
      new edn.List([new edn.Symbol("use-world"), world("default")]),
    );
    if (isFailure(body)) {
      console.log(`use-world refused: ${describeFailure(body)}`);
      return;
    }
    console.log(
      `use-world "default" -> ${kwText(eventOf(body))}` +
        `  world=${kwText(field(body, ":world"))}`,
    );

    // 3. Propose a fact: morningstar is equivalent to venus. The reply hands
    //    back a #proposal handle we feed straight into the assert.
    const f = fact(new edn.Symbol("equivalent"), entity("morningstar"), entity("venus"));
    body = await call(new edn.List([new edn.Symbol("propose"), f]));
    if (isFailure(body)) {
      console.log(`propose refused: ${describeFailure(body)}`);
      return;
    }
    const proposal = field(body, ":proposal");
    console.log(
      `propose (equivalent morningstar venus) -> ${kwText(eventOf(body))}` +
        `  proposal=${kwText(proposal)}`,
    );

    // 4. Assert the proposed fact into the world. The #proposal handle from the
    //    propose reply round-trips back onto the wire untouched.
    body = await call(new edn.List([new edn.Symbol("assert"), proposal]));
    if (isFailure(body)) {
      console.log(`assert refused: ${describeFailure(body)}`);
      return;
    }
    console.log(`assert proposal -> ${kwText(eventOf(body))}`);

    // 5. Query it back. As in the HTTP path, :find / :where are VECTORS and the
    //    where-clause is a vector of vectors; only the verb head is a List.
    const query = new edn.List([
      new edn.Symbol("query"),
      new edn.Map([
        new edn.Keyword(":find"), new edn.Vector([new edn.Symbol("?o")]),
        new edn.Keyword(":where"), new edn.Vector([
          new edn.Vector([
            new edn.Symbol("equivalent"),
            entity("morningstar"),
            new edn.Symbol("?o"),
          ]),
        ]),
      ]),
    ]);
    body = await call(query);
    if (isFailure(body)) {
      console.log(`query refused: ${describeFailure(body)}`);
      return;
    }
    console.log(
      `query (equivalent morningstar ?o) -> rows=${kwText(field(body, ":rows"))}` +
        `  done?=${kwText(field(body, ":done?"))}`,
    );
  } finally {
    // Always release the socket, success or failure. Closing it also lets the
    // server's reader thread observe EOF and drop the session.
    socket.destroy();
  }
}

/**
 * Route CLI arguments (`process.argv.slice(2)`) to a transport.
 *
 * With no arguments we keep the original HTTP behaviour against the local
 * default. A leading `"uds"` selects the Unix-domain-socket transport (with an
 * optional socket path). Any other leading argument is an HTTP base URL.
 */
export function _dispatch(argv: string[]): Promise<void> {
  if (argv[0] === "uds") {
    return main_uds(argv[1] ?? DEFAULT_SOCKET);
  }
  if (argv.length > 0) {
    return main(argv[0]);
  }
  return main(DEFAULT_BASE);
}

// Runnable guard: Bun sets `import.meta.main` for a directly-run entry file;
// Node leaves it undefined, so this is a harmless no-op when imported (and the
// optional-chaining keeps it from throwing under Node's typings). No network
// happens at import time -- only here.
if ((import.meta as { main?: boolean }).main) {
  _dispatch(process.argv.slice(2)).catch((err) => {
    console.error(err instanceof Error ? err.message : String(err));
    process.exit(1);
  });
}
