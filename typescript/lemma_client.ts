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
// Capabilities & limits:  the :welcome surface  ->  ServerInfo
//
// Every session opens with a (hello) whose :welcome reply advertises what the
// server can do (SPEC §10): a :capabilities set of namespaced flag keywords, a
// :limits map of resource caps, and the :verbs / :predicates the world exposes.
// A well-behaved client reads this once and tailors itself to it -- skipping
// features the server doesn't advertise and staying under the byte caps it
// enforces. ServerInfo is the parsed, queryable form of that surface; it is a
// plain data record (not a new abstraction layer), so the round-trip code below
// can ask "does this server paginate?" or "is my message small enough?" in one
// readable call.
//
// jsedn note: a parsed `:capabilities` set is an `edn.Set`, a `:limits` map is
// an `edn.Map`, and a `:verbs` / `:predicates` surface is an `edn.Map` of the
// shape `{:core #{...} :extensions {pack #{...}}}`. We canonicalise keywords to
// their wire text (via `edn.encode`) for membership tests, since `edn.Keyword`
// has no reliable object identity across parses.
// ---------------------------------------------------------------------------

/**
 * The parsed :welcome surface: what this server advertises (SPEC §10).
 *
 * `capabilities` is a Set of capability canonical-texts (e.g.
 * `":lemma/cursor-pagination"`); `limits` maps a limit's canonical-text
 * keyword to its value; `verbs` / `predicates` are flat Sets of name
 * canonical-texts with the :core and :extensions surfaces merged. A record,
 * not a class hierarchy -- it just answers "does this server support X?" and
 * "what is the byte cap?".
 */
export interface ServerInfo {
  /** :version value as the raw parsed `edn` value (or `null` if absent). */
  version: unknown;
  /** Advertised capability flags, keyed by canonical text (`:lemma/...`). */
  capabilities: Set<string>;
  /** :limits map, keyed by the limit keyword's canonical text. */
  limits: Map<string, unknown>;
  /** Every verb name this server understands (core ∪ all extension packs). */
  verbs: Set<string>;
  /** Every predicate name this server understands (core ∪ extension packs). */
  predicates: Set<string>;
  /** True iff `capability` (canonical text, e.g. ":lemma/watch") is advertised. */
  supports(capability: string): boolean;
  /**
   * The :max-message-bytes limit, or `undefined` if the server didn't
   * advertise one. `undefined` means "unadvertised" -- treated as unlimited by
   * `within_message_limit`.
   */
  maxMessageBytes: number | undefined;
}

/**
 * Merge a `{:core #{…} :extensions {pack #{…}}}` surface into one flat Set of
 * name canonical-texts.
 *
 * The :verbs and :predicates entries of a welcome split names into a :core set
 * plus per-pack :extensions sets (SPEC §10). A client mostly just wants "every
 * name this server understands", so we union :core with all the extension sets.
 * Missing keys default to empty -- a minimal welcome need not carry every
 * section -- so this never throws on a sparse surface.
 */
function flattenSurface(surface: unknown): Set<string> {
  const names = new Set<string>();
  if (!(surface instanceof edn.Map)) return names;

  const core = new edn.Keyword(":core");
  if (surface.exists(core)) {
    const coreSet = surface.at(core);
    if (coreSet instanceof edn.Set) {
      for (const name of coreSet.val) names.add(kwText(name));
    }
  }

  const extensions = new edn.Keyword(":extensions");
  if (surface.exists(extensions)) {
    const packs = surface.at(extensions);
    if (packs instanceof edn.Map) {
      // each((value, key) => ...): every value is a pack's name Set.
      packs.each((packSet: unknown) => {
        if (packSet instanceof edn.Set) {
          for (const name of packSet.val) names.add(kwText(name));
        }
      });
    }
  }
  return names;
}

/**
 * Parse a :welcome envelope into a `ServerInfo`.
 *
 * `body` is the parsed welcome map (an `edn.Map`). We pull :version,
 * :capabilities (an `edn.Set` of Keywords, canonicalised to their wire text),
 * :limits (an `edn.Map`, copied into a plain JS Map keyed by each limit's
 * canonical text), and the flattened :verbs / :predicates surfaces. Every key
 * is optional: a server that omits a section yields an empty default rather
 * than an error, so this stays robust against minimal welcomes.
 */
export function read_welcome(body: unknown): ServerInfo {
  const capabilities = new Set<string>();
  if (isMap(body)) {
    const capsKey = new edn.Keyword(":capabilities");
    if (body.exists(capsKey)) {
      const caps = body.at(capsKey);
      if (caps instanceof edn.Set) {
        for (const cap of caps.val) capabilities.add(kwText(cap));
      }
    }
  }

  const limits = new Map<string, unknown>();
  if (isMap(body)) {
    const limitsKey = new edn.Keyword(":limits");
    if (body.exists(limitsKey)) {
      const lim = body.at(limitsKey);
      if (lim instanceof edn.Map) {
        lim.each((value: unknown, key: unknown) => {
          limits.set(kwText(key), value);
        });
      }
    }
  }

  const maxBytes = limits.get(":max-message-bytes");
  const maxMessageBytes = typeof maxBytes === "number" ? maxBytes : undefined;

  return {
    version: isMap(body) ? field(body, ":version", null) : null,
    capabilities,
    limits,
    verbs: flattenSurface(field(body, ":verbs", null)),
    predicates: flattenSurface(field(body, ":predicates", null)),
    supports(capability: string): boolean {
      return this.capabilities.has(capability);
    },
    maxMessageBytes,
  };
}

/**
 * True iff `ednText` fits under the server's :max-message-bytes cap.
 *
 * The limit is measured in UTF-8 bytes (SPEC §10). An unadvertised limit
 * (`maxMessageBytes === undefined`) means unlimited, so any message passes.
 */
export function within_message_limit(info: ServerInfo, ednText: string): boolean {
  if (info.maxMessageBytes === undefined) return true;
  return Buffer.byteLength(ednText, "utf8") <= info.maxMessageBytes;
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

/**
 * Read framed replies until a `:watch-event` arrives; return it (or `null`).
 *
 * Over UDS there is no separate event channel: watch pushes interleave with
 * ordinary command responses on the *same* frame stream (uds.clj fans both onto
 * the one connection). So after triggering a change we read frames in a loop,
 * skipping command replies (the `:asserted` echo, etc.) until we see the
 * `:watch-event` envelope, demultiplexing the push out of the command stream
 * with the same `FrameReader`. The loop is bounded by `maxFrames` and by the
 * socket's timeout (the caller arms `socket.setTimeout` before triggering), so
 * a missing push can never hang -- a timeout/EOF rejects `recv()` and we return
 * `null`, which the caller reports as "no event observed".
 *
 * `reader` is the connection's `FrameReader`; `maxFrames` caps how many frames
 * we will drain before giving up. The parsed `:watch-event` map is returned, or
 * `null` if none arrived within the budget.
 */
export async function uds_await_watch_event(
  reader: FrameReader,
  maxFrames: number = 8,
): Promise<unknown> {
  for (let i = 0; i < maxFrames; i += 1) {
    let body: unknown;
    try {
      body = edn.parse(await uds_recv_frame(reader));
    } catch {
      // Socket timeout, EOF, or a parse failure: no push is coming on this
      // connection within the budget. Report "no event observed".
      return null;
    }
    if (isKeyword(eventOf(body), ":watch-event")) return body;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Watch over HTTP:  the SSE event stream  ->  parsed :watch-event envelopes
//
// A (watch-pattern ...) call registers a standing query; matching changes are
// then *pushed* to the session rather than polled. Over HTTP those pushes
// arrive on a separate Server-Sent-Events stream, GET /v1/sessions/{id}/events
// (SPEC §9). SSE is a one-way text stream: each event is one or more `data:`
// lines terminated by a blank line; `:`-prefixed lines are keep-alive comments
// to be ignored.
//
// Why a raw socket instead of `fetch`? Dianoia (http-kit) serves the stream
// with `Transfer-Encoding: chunked` and writes an immediate size-0 chunk to
// flush the response headers before any event exists. A standard chunked reader
// -- including the WHATWG stream behind `fetch(...).body` on both Node and Bun
// -- treats that size-0 chunk as end-of-body and reports EOF, closing the
// stream before the first event ever arrives. So we speak HTTP by hand over a
// raw node:net socket -- the same plumbing the UDS transport already uses --
// and treat a size-0 chunk as a keep-alive flush (skip it, keep reading) rather
// than as the end of the stream. Every read is bounded by `timeout` so a quiet
// stream can never hang the demo.
//
// ORDERING IS LOAD-BEARING. Dianoia registers the per-session SSE sink LAZILY,
// at the moment the GET /events connection's headers are written -- and the
// watch dispatcher delivers a :watch-event only to sinks present at emit time,
// with NO backlog replay. So the stream must be OPENED (sink registered) BEFORE
// the change that triggers the event, or the push races ahead of the sink and
// is lost. We therefore split the work in two:
//
//   * open_sse_stream -- connect, send the GET, read PAST the status line and
//     headers (writing the request + draining headers is what makes Dianoia
//     register the sink), and hand back an open handle. Call this BEFORE the
//     trigger.
//   * read_sse_events -- drain parsed events from an already-open handle, AFTER
//     the trigger. Bounded by the handle's read timeout.
//
// This is read-only and single-threaded by design. The only plumbing
// dependency is node:net; the EDN codec is still jsedn.
// ---------------------------------------------------------------------------

/**
 * An open SSE connection: a raw socket whose bytes are buffered as they arrive,
 * exposed as awaitable line/byte reads with a per-read timeout.
 *
 * Node delivers `'data'` as arbitrary Buffer chunks, so -- exactly as
 * `FrameReader` does for length-prefixed frames -- we accumulate every byte and
 * peel structure off the front on demand. `open_sse_stream` builds one
 * (connection live, headers consumed, server sink registered); `read_sse_events`
 * drains events from it; `close` releases the socket so the server drops the
 * stream. The chunked-transfer and SSE decoding both run against this one
 * buffer, so no bytes read past one boundary are lost before the next.
 *
 * A read parks a single waiter until either enough bytes are buffered to
 * satisfy it, the per-read `timeout` elapses, or the peer closes (`'end'` /
 * `'close'` / `'error'`). Timeout and EOF both reject the pending read so a
 * quiet or torn-down stream surfaces as an error the reader turns into "no
 * events", never a hang. At most one read may be outstanding at a time, which
 * matches the strictly sequential decode in `read_sse_events`.
 */
export class SSEStream {
  private chunks: Buffer = Buffer.alloc(0);
  private pending: {
    want: number | "line";
    resolve: (buf: Buffer) => void;
    reject: (err: Error) => void;
    timer: ReturnType<typeof setTimeout>;
  } | null = null;
  private failure: Error | null = null;
  private readonly timeoutMs: number;

  constructor(private readonly socket: net.Socket, timeoutMs: number) {
    this.timeoutMs = timeoutMs;
    socket.on("data", (chunk: Buffer) => {
      this.chunks = Buffer.concat([this.chunks, chunk]);
      this.deliver();
    });
    socket.on("end", () => this.fail("connection closed by peer (EOF)"));
    socket.on("close", () => this.fail("connection closed"));
    socket.on("error", (err: Error) => this.fail(err.message));
  }

  /** Buffer the bytes already read past the header terminator (see open). */
  prime(initial: Buffer): void {
    if (initial.length) {
      this.chunks = Buffer.concat([this.chunks, initial]);
    }
  }

  /** Resolve with the next CRLF-delimited line (without the CRLF). */
  readLine(): Promise<Buffer> {
    return this.read("line");
  }

  /** Resolve with exactly `n` bytes off the wire. */
  readN(n: number): Promise<Buffer> {
    return this.read(n);
  }

  /** Release the socket, letting the server tear down the stream. */
  close(): void {
    this.socket.destroy();
  }

  private read(want: number | "line"): Promise<Buffer> {
    if (this.pending) {
      return Promise.reject(
        new Error("SSEStream.read() called while a read is already pending"),
      );
    }
    return new Promise<Buffer>((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this.pending) {
          this.pending = null;
          reject(new Error("SSE read timed out"));
        }
      }, this.timeoutMs);
      this.pending = { want, resolve, reject, timer };
      // Bytes may already be buffered; try to satisfy immediately. Otherwise we
      // may already have seen EOF/error -- fail now rather than wait forever.
      if (!this.deliver() && this.failure) {
        clearTimeout(timer);
        this.pending = null;
        reject(this.failure);
      }
    });
  }

  /** If a waiter is parked and its bytes are buffered, hand them over. */
  private deliver(): boolean {
    if (!this.pending) return false;
    const { want } = this.pending;

    if (want === "line") {
      const idx = this.chunks.indexOf("\r\n", 0, "latin1");
      if (idx < 0) return false;
      const line = this.chunks.subarray(0, idx);
      this.chunks = this.chunks.subarray(idx + 2);
      return this.settle(line);
    }

    if (this.chunks.length < want) return false;
    const out = this.chunks.subarray(0, want);
    this.chunks = this.chunks.subarray(want);
    return this.settle(out);
  }

  private settle(buf: Buffer): boolean {
    const waiter = this.pending!;
    this.pending = null;
    clearTimeout(waiter.timer);
    waiter.resolve(buf);
    return true;
  }

  /** Record an EOF/error and reject any parked read with it. */
  private fail(message: string): void {
    if (!this.failure) this.failure = new Error(message);
    if (this.pending) {
      const waiter = this.pending;
      this.pending = null;
      clearTimeout(waiter.timer);
      waiter.reject(this.failure);
    }
  }
}

/**
 * Open the SSE event stream for `sessionId` and return an open `SSEStream`.
 *
 * Connects a raw socket to the host/port parsed from `base` (e.g.
 * `"http://127.0.0.1:8080"`), issues `GET /v1/sessions/{id}/events` with an
 * `Accept: text/event-stream` header, and reads PAST the status line and
 * response headers -- stopping at the blank line that begins the body. It does
 * NOT read any event bodies; that is `read_sse_events`'s job. Bytes already
 * read past the header terminator are primed onto the handle so the chunked
 * decoder does not lose them.
 *
 * The split matters because writing the GET and draining its headers is what
 * makes Dianoia register this session's SSE sink, and the watch dispatcher only
 * delivers to sinks that exist when an event is emitted (no replay). So a caller
 * must open the stream BEFORE triggering the change it wants to observe, then
 * read AFTER -- otherwise the push races ahead of the sink and is lost.
 *
 * `timeout` (seconds) is the per-read budget stored on the handle, so the
 * subsequent `read_sse_events` inherits it. The returned handle must be closed
 * by the caller when done. If the server closes the connection before the header
 * terminator arrives, the returned handle is still valid but empty; the
 * subsequent read sees EOF and yields no events.
 */
export async function open_sse_stream(
  base: string,
  sessionId: string,
  timeout: number = 10.0,
): Promise<SSEStream> {
  const url = new URL(base);
  const host = url.hostname;
  const port = url.port ? Number(url.port) : 80;
  const timeoutMs = timeout * 1000;

  const socket = net.createConnection({ host, port });
  // Buffer header bytes ourselves until the blank-line terminator: we cannot
  // hand the socket to SSEStream's 'data' handler yet, or it would swallow the
  // header read. We attach a temporary collector, then transfer leftovers.
  let header = Buffer.alloc(0);
  const onConnect = new Promise<void>((resolve, reject) => {
    socket.once("connect", () => resolve());
    socket.once("error", (err: Error) =>
      reject(
        new Error(
          `could not reach the Lemma server at ${base} (${err.message}); ` +
            "is the server running?",
        ),
      ),
    );
  });
  await onConnect;

  const request =
    `GET /v1/sessions/${sessionId}/events HTTP/1.1\r\n` +
    `Host: ${host}:${port}\r\n` +
    `Accept: text/event-stream\r\n` +
    `X-Lemma-Session: ${sessionId}\r\n` +
    `Connection: keep-alive\r\n\r\n`;
  socket.write(request);

  // Read PAST the status line and headers; the body starts after the blank
  // line. Draining the headers here is the act that registers the server-side
  // sink. Bounded by the same timeout so a stalled handshake cannot hang.
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => {
      cleanup();
      // A quiet handshake: hand back whatever we have (likely nothing). The
      // later read will see EOF/timeout and report no events.
      resolve();
    }, timeoutMs);
    const onData = (chunk: Buffer) => {
      header = Buffer.concat([header, chunk]);
      if (header.indexOf("\r\n\r\n", 0, "latin1") >= 0) {
        cleanup();
        resolve();
      }
    };
    const onClose = () => {
      cleanup();
      // Server closed before headers completed: resolve with an empty body so
      // the caller's try/finally can close the handle uniformly.
      resolve();
    };
    const onError = (err: Error) => {
      cleanup();
      reject(
        new Error(
          `could not reach the Lemma server at ${base} (${err.message}); ` +
            "is the server running?",
        ),
      );
    };
    function cleanup() {
      clearTimeout(timer);
      socket.removeListener("data", onData);
      socket.removeListener("close", onClose);
      socket.removeListener("error", onError);
    }
    socket.on("data", onData);
    socket.on("close", onClose);
    socket.on("error", onError);
  });

  const stream = new SSEStream(socket, timeoutMs);
  const sep = header.indexOf("\r\n\r\n", 0, "latin1");
  if (sep >= 0) {
    // Hand any bytes already read past the header terminator to the handle so
    // the chunked decoder picks up exactly where the header read stopped.
    stream.prime(header.subarray(sep + 4));
  }
  return stream;
}

/**
 * Drain up to `maxEvents` parsed envelopes from an open `SSEStream`.
 *
 * `stream` is the handle returned by `open_sse_stream` (its socket is live and
 * its headers already consumed). This transfer-decodes the chunked body and
 * parses Server-Sent Events out of it: each event's `data:` lines are
 * concatenated and run through `edn.parse`, so the return value is a list of
 * parsed envelopes (typically `:watch-event` maps).
 *
 * A size-0 chunk is http-kit's header-flush keep-alive, NOT end-of-stream, so
 * we skip it and keep reading. A genuine connection close or the per-read
 * timeout ends the read and returns whatever arrived so far -- a quiet stream
 * degrades to an empty list rather than hanging. The caller owns the socket and
 * closes it; this only reads.
 */
export async function read_sse_events(
  stream: SSEStream,
  maxEvents: number = 1,
): Promise<unknown[]> {
  const events: unknown[] = [];
  let text = ""; // decoded body bytes awaiting SSE framing

  try {
    while (events.length < maxEvents) {
      const sizeLine = (await stream.readLine()).toString("latin1").trim();
      if (sizeLine === "") continue; // stray blank line between chunks
      const size = parseInt(sizeLine, 16);
      if (Number.isNaN(size)) break; // not a chunk-size line -- give up cleanly
      if (size === 0) continue; // header-flush keep-alive, not end-of-stream
      text += (await stream.readN(size)).toString("utf-8");
      await stream.readN(2); // the CRLF that trails every chunk body

      // An SSE event is the run of lines up to the next blank line. Concatenate
      // its `data:` payloads (dropping `:` comments) and parse the result as one
      // EDN envelope.
      let nl = text.indexOf("\n\n");
      while (nl >= 0) {
        const block = text.slice(0, nl);
        text = text.slice(nl + 2);
        const data = block
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice("data:".length).replace(/^\s+/, ""));
        if (data.length) {
          events.push(edn.parse(data.join("\n")));
          if (events.length >= maxEvents) break;
        }
        nl = text.indexOf("\n\n");
      }
    }
  } catch {
    // End of stream or a quiet period (read timeout / EOF): return what we
    // gathered. The caller treats an empty list as "no event observed in time".
  }
  return events;
}

// ---------------------------------------------------------------------------
// Cursor pagination:  drain a (query :limit N) across (continue #cursor) pages
//
// A (query ...) with a :limit returns a full first page whose :done? is false
// and which carries a #cursor handle; each (continue #cursor) returns the next
// page (more :rows, a fresh :cursor) until :done? is true (SPEC §8). query_all
// walks that sequence to completion, mirroring python's query_all so the two
// clients have parity. Both transports' send closures are `form -> body`, so
// query_all is transport-agnostic: the caller supplies the closure.
// ---------------------------------------------------------------------------

/** The outcome of draining a paginated query: all rows, page count, failure. */
export interface PagedResult {
  /** Every row gathered across all pages (the raw `edn` values from `:rows`). */
  rows: unknown[];
  /** How many pages were fetched (the first query counts as page 1). */
  pages: number;
  /** The offending `:error` / `:rejected` body, or `null` on success. */
  failure: unknown;
}

/**
 * Run a `(query ...)` and drain every page via `(continue #cursor ...)`.
 *
 * `send` is a `form -> Promise<body>` closure (the per-transport adapter).
 * Returns `{ rows, pages, failure }`: `failure` is `null` on success or the
 * offending `:error` / `:rejected` body. A `:limit` query returns a full first
 * page with `:done? false` and a `#cursor`; we `(continue #cursor)` until
 * `:done?` is true.
 *
 * The `#cursor` carried on each page is an opaque `edn.Tagged` that round-trips
 * back onto the wire untouched. Note an expired cursor (the server's idle TTL
 * is ~300s, SPEC §8) comes back as `:error :unknown-handle`; this demo
 * propagates that failure as-is, whereas a real client would re-issue the
 * original query to start a fresh page.
 */
export async function query_all(
  send: (form: unknown) => Promise<unknown>,
  queryForm: unknown,
): Promise<PagedResult> {
  let body = await send(queryForm);
  if (isFailure(body)) {
    return { rows: [], pages: 0, failure: body };
  }

  // `:rows` is an `edn.Vector`; its elements live in `.val`. We accumulate the
  // raw values so the caller can re-encode them exactly as the server sent.
  const rows: unknown[] = [...(field(body, ":rows") as edn.Vector).val];
  let pages = 1;
  // `:done?` parses to a JS boolean. While it is false the server has more
  // pages and (only then) includes a `:cursor` to fetch the next one.
  while (field(body, ":done?") === false) {
    // `:cursor` is present exactly when `:done?` is false -- the server omits
    // it on an already-done result, so we read it only inside this loop.
    const cursor = field(body, ":cursor");
    body = await send(new edn.List([new edn.Symbol("continue"), cursor]));
    if (isFailure(body)) {
      return { rows, pages, failure: body };
    }
    rows.push(...(field(body, ":rows") as edn.Vector).val);
    pages += 1;
  }
  return { rows, pages, failure: null };
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

  // 1a. Read the advertised capabilities and limits once, up front, so the
  //     rest of the round-trip can tailor itself to this server (SPEC §10).
  const info = read_welcome(welcome);
  const caps = [...info.capabilities].sort().join(", ");
  console.log(`server: caps={${caps}} max-message-bytes=${info.maxMessageBytes}`);

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

  // 7. The paginated section is gated on the server advertising cursor
  //    pagination -- without it, draining pages via (continue #cursor ...) is
  //    unsupported, so we skip the whole block rather than guess.
  if (info.supports(":lemma/cursor-pagination")) {
    // Seed three subset-of facts in one propose, assert the batch, then query
    // them back with :limit 2 so the result spans two pages (2 + 1) that
    // query_all drains via (continue #cursor ...). subset-of is a pure-EDB
    // (stored-fact) predicate, so a query over it has stable (tx-id, ref-id)
    // ordering and can be paginated; a rule-headed predicate like member-of
    // cannot be the sole outer :where pattern (the server rejects it
    // :bad-args :unsupported-rule-call-ordering).
    const f1 = fact(new edn.Symbol("subset-of"), entity("sub-a"), entity("group"));
    const f2 = fact(new edn.Symbol("subset-of"), entity("sub-b"), entity("group"));
    const f3 = fact(new edn.Symbol("subset-of"), entity("sub-c"), entity("group"));
    const proposeForm = new edn.List([new edn.Symbol("propose"), f1, f2, f3]);
    // The batch propose is the largest representative message we send, so it is
    // the one worth checking against :max-message-bytes. A real client checks
    // every outbound message; this demo checks this one.
    if (!within_message_limit(info, edn.encode(proposeForm))) {
      console.log("limit-exceeded: message exceeds max-message-bytes; skipping");
      return;
    }
    res = await named(proposeForm);
    if (isFailure(res.body)) {
      console.log(`propose (3x subset-of) refused: ${describeFailure(res.body)}`);
      return;
    }
    const batch = field(res.body, ":proposal");
    console.log(
      `propose (3x subset-of ? group) -> ${kwText(eventOf(res.body))}` +
        `  proposal=${kwText(batch)}`,
    );
    res = await named(new edn.List([new edn.Symbol("assert"), batch]));
    if (isFailure(res.body)) {
      console.log(`assert (3x subset-of) refused: ${describeFailure(res.body)}`);
      return;
    }
    console.log(`assert proposal -> ${kwText(eventOf(res.body))}`);

    // The paged query itself: :limit 2 over 3 matching rows yields two pages.
    // query_all wants a `form -> Promise<body>` closure; post_edn returns a
    // PostResult, so we adapt it by taking just the body.
    const pagedQuery = new edn.List([
      new edn.Symbol("query"),
      new edn.Map([
        new edn.Keyword(":find"), new edn.Vector([new edn.Symbol("?x")]),
        new edn.Keyword(":where"), new edn.Vector([
          new edn.Vector([
            new edn.Symbol("subset-of"),
            new edn.Symbol("?x"),
            entity("group"),
          ]),
        ]),
        new edn.Keyword(":limit"), 2,
      ]),
    ]);
    const send = (form: unknown) =>
      post_edn(`/v1/sessions/${sid}/messages`, form, sid, base).then((r) => r.body);
    const paged = await query_all(send, pagedQuery);
    if (paged.failure) {
      console.log(`paged query refused: ${describeFailure(paged.failure)}`);
      return;
    }
    console.log(
      `paged query (subset-of ? group), limit 2 -> ${paged.rows.length} rows over ` +
        `${paged.pages} page(s): ${edn.encode(new edn.Vector(paged.rows))}`,
    );
  } else {
    console.log("server does not advertise cursor pagination; skipping paged query");
  }

  // 8. Watch: register a standing pattern and observe a matching change pushed
  //    back on the SSE event stream. Gated on the server advertising
  //    :lemma/watch -- without it the (watch-pattern ...) verb is unsupported.
  if (info.supports(":lemma/watch")) {
    // (watch-pattern :pattern [[subset-of ?x #entity "group"]]) -- the args are
    // FLAT keyword args (the :pattern keyword then the where-vector), not a
    // wrapping map. The reply hands back a #watch handle to unwatch with.
    const pattern = new edn.Vector([
      new edn.Vector([
        new edn.Symbol("subset-of"),
        new edn.Symbol("?x"),
        entity("group"),
      ]),
    ]);
    res = await named(
      new edn.List([new edn.Symbol("watch-pattern"), new edn.Keyword(":pattern"), pattern]),
    );
    if (isFailure(res.body)) {
      console.log(`watch-pattern refused: ${describeFailure(res.body)}`);
      return;
    }
    const watch = field(res.body, ":watch");
    console.log(
      `watch (subset-of ? group) -> ${kwText(eventOf(res.body))}` +
        `  watch=${kwText(watch)}`,
    );

    // Ordering is load-bearing: Dianoia registers this session's SSE sink
    // lazily, when the GET /events headers are written, and delivers a
    // :watch-event only to sinks present at emit time (no backlog replay). So
    // OPEN the stream first (registering the sink), THEN trigger the change,
    // THEN drain -- otherwise the push can fire within milliseconds of the
    // assert, before our sink exists, and be silently lost.
    const stream = await open_sse_stream(base, sid!, 10.0);
    try {
      // The server pushes only DELTAS, so the change must be new: a fact
      // re-asserted verbatim is a no-op and fires nothing. We key the probe
      // entity to this process so each run asserts a genuinely fresh fact.
      const probe = entity(`watch-probe-${process.pid}`);
      res = await named(
        new edn.List([
          new edn.Symbol("propose"),
          fact(new edn.Symbol("subset-of"), probe, entity("group")),
        ]),
      );
      if (isFailure(res.body)) {
        console.log(`watch-probe propose refused: ${describeFailure(res.body)}`);
        return;
      }
      res = await named(
        new edn.List([new edn.Symbol("assert"), field(res.body, ":proposal")]),
      );
      if (isFailure(res.body)) {
        console.log(`watch-probe assert refused: ${describeFailure(res.body)}`);
        return;
      }

      const events = await read_sse_events(stream, 1);
      if (events.length) {
        const evt = events[0];
        console.log(
          `watch (subset-of ? group) -> ${kwText(eventOf(evt))}` +
            ` type=${kwText(field(evt, ":type"))}` +
            ` data=${kwText(field(evt, ":data"))}`,
        );
      } else {
        console.log("watch: no event observed before timeout");
      }
    } finally {
      // Release the SSE socket so the server drops the stream, whether or not
      // an event arrived.
      stream.close();
    }

    // Tear the watch down. (unwatch #watch "w-N") -> :ok.
    res = await named(new edn.List([new edn.Symbol("unwatch"), watch]));
    if (isFailure(res.body)) {
      console.log(`unwatch refused: ${describeFailure(res.body)}`);
      return;
    }
    console.log(`unwatch ${kwText(watch)} -> ${kwText(eventOf(res.body))}`);
  } else {
    console.log("server does not advertise watch; skipping watch demo");
  }
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

    // 1a. Read the advertised capabilities and limits once, up front, so the
    //     rest of the round-trip can tailor itself to this server (SPEC §10).
    const info = read_welcome(welcome);
    const caps = [...info.capabilities].sort().join(", ");
    console.log(`server: caps={${caps}} max-message-bytes=${info.maxMessageBytes}`);

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

    // 6. The paginated section is gated on the server advertising cursor
    //    pagination -- without it, draining pages via (continue #cursor ...) is
    //    unsupported, so we skip the whole block rather than guess.
    if (info.supports(":lemma/cursor-pagination")) {
      // Seed three subset-of facts in one propose, assert the batch, then query
      // them back with :limit 2 so the result spans two pages (2 + 1) that
      // query_all drains via (continue #cursor ...). subset-of is a pure-EDB
      // predicate (stable tx-id/ref-id ordering), so it can be paginated; a
      // rule-headed predicate like member-of cannot be the sole outer :where
      // pattern (server :bad-args :unsupported-rule-call-ordering).
      const f1 = fact(new edn.Symbol("subset-of"), entity("sub-a"), entity("group"));
      const f2 = fact(new edn.Symbol("subset-of"), entity("sub-b"), entity("group"));
      const f3 = fact(new edn.Symbol("subset-of"), entity("sub-c"), entity("group"));
      const proposeForm = new edn.List([new edn.Symbol("propose"), f1, f2, f3]);
      // The batch propose is the largest representative message we send, so it
      // is the one worth checking against :max-message-bytes. A real client
      // checks every outbound message; this demo checks this one.
      if (!within_message_limit(info, edn.encode(proposeForm))) {
        console.log("limit-exceeded: message exceeds max-message-bytes; skipping");
        return;
      }
      body = await call(proposeForm);
      if (isFailure(body)) {
        console.log(`propose (3x subset-of) refused: ${describeFailure(body)}`);
        return;
      }
      const batch = field(body, ":proposal");
      console.log(
        `propose (3x subset-of ? group) -> ${kwText(eventOf(body))}` +
          `  proposal=${kwText(batch)}`,
      );
      body = await call(new edn.List([new edn.Symbol("assert"), batch]));
      if (isFailure(body)) {
        console.log(`assert (3x subset-of) refused: ${describeFailure(body)}`);
        return;
      }
      console.log(`assert proposal -> ${kwText(eventOf(body))}`);

      // The paged query itself: :limit 2 over 3 matching rows yields two pages.
      // The UDS `call` closure is already `form -> Promise<body>`, so query_all
      // takes it directly.
      const pagedQuery = new edn.List([
        new edn.Symbol("query"),
        new edn.Map([
          new edn.Keyword(":find"), new edn.Vector([new edn.Symbol("?x")]),
          new edn.Keyword(":where"), new edn.Vector([
            new edn.Vector([
              new edn.Symbol("subset-of"),
              new edn.Symbol("?x"),
              entity("group"),
            ]),
          ]),
          new edn.Keyword(":limit"), 2,
        ]),
      ]);
      const paged = await query_all(call, pagedQuery);
      if (paged.failure) {
        console.log(`paged query refused: ${describeFailure(paged.failure)}`);
        return;
      }
      console.log(
        `paged query (subset-of ? group), limit 2 -> ${paged.rows.length} rows over ` +
          `${paged.pages} page(s): ${edn.encode(new edn.Vector(paged.rows))}`,
      );
    } else {
      console.log("server does not advertise cursor pagination; skipping paged query");
    }

    // 7. Watch over UDS. Same standing-pattern idea as the HTTP path, but the
    //    push has nowhere separate to go: it interleaves with command replies on
    //    this one socket. Gated on the server advertising :lemma/watch.
    if (info.supports(":lemma/watch")) {
      // Bound every subsequent read so a missing push cannot hang the demo.
      // Node's socket timeout fires a 'timeout' event but does NOT close the
      // socket; we destroy it so the FrameReader's parked recv() rejects (via
      // 'close'), which uds_await_watch_event turns into "no event observed".
      socket.setTimeout(10000, () => socket.destroy());

      // (watch-pattern :pattern [[subset-of ?x #entity "group"]]) -- flat
      // keyword args, as on HTTP. The reply carries the #watch handle.
      const pattern = new edn.Vector([
        new edn.Vector([
          new edn.Symbol("subset-of"),
          new edn.Symbol("?x"),
          entity("group"),
        ]),
      ]);
      body = await call(
        new edn.List([new edn.Symbol("watch-pattern"), new edn.Keyword(":pattern"), pattern]),
      );
      if (isFailure(body)) {
        console.log(`watch-pattern refused: ${describeFailure(body)}`);
        return;
      }
      const watch = field(body, ":watch");
      console.log(
        `watch (subset-of ? group) -> ${kwText(eventOf(body))}` +
          `  watch=${kwText(watch)}`,
      );

      // Trigger a fresh delta (a verbatim re-assert is a no-op and fires
      // nothing), keyed to this process so each run is genuinely new. The
      // :asserted reply and the :watch-event push both land on this socket; we
      // read the assert reply here via call(), then demux the push below.
      const probe = entity(`watch-probe-${process.pid}`);
      body = await call(
        new edn.List([
          new edn.Symbol("propose"),
          fact(new edn.Symbol("subset-of"), probe, entity("group")),
        ]),
      );
      if (isFailure(body)) {
        console.log(`watch-probe propose refused: ${describeFailure(body)}`);
        return;
      }
      body = await call(
        new edn.List([new edn.Symbol("assert"), field(body, ":proposal")]),
      );
      if (isFailure(body)) {
        console.log(`watch-probe assert refused: ${describeFailure(body)}`);
        return;
      }

      const evt = await uds_await_watch_event(reader);
      if (evt !== null) {
        console.log(
          `watch (subset-of ? group) -> ${kwText(eventOf(evt))}` +
            ` type=${kwText(field(evt, ":type"))}` +
            ` data=${kwText(field(evt, ":data"))}`,
        );
      } else {
        console.log("watch: no event observed before timeout");
      }

      // Tear the watch down. (unwatch #watch "w-N") -> :ok.
      body = await call(new edn.List([new edn.Symbol("unwatch"), watch]));
      if (isFailure(body)) {
        console.log(`unwatch refused: ${describeFailure(body)}`);
        return;
      }
      console.log(`unwatch ${kwText(watch)} -> ${kwText(eventOf(body))}`);
    } else {
      console.log("server does not advertise watch; skipping watch demo");
    }
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
