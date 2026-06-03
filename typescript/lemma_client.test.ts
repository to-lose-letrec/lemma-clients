/**
 * Unit tests for the TypeScript Lemma client (bun:test).
 *
 * The client leans on `jsedn` for the EDN codec, so we do NOT re-test the
 * third-party parser. Instead we cover the surface this client OWNS:
 *
 *   (A) post_edn -- the HTTP transport: the outbound Request shape (URL,
 *       method, content-type, body, session-header presence), the happy 2xx
 *       path (parsed body + session id from the response header), and the
 *       non-2xx error-envelope recovery (parsed + returned, never thrown).
 *   (B) main()  -- the full hello/use-world/propose/assert/query handshake,
 *       driven against a scripted `fetch` so no socket is opened. We assert it
 *       completes without throwing, that the session id from the welcome
 *       header is threaded into the named-session endpoint, and that the
 *       #proposal handle is threaded from the propose reply into the assert.
 *   (C) EDN sanity -- a verb form encodes to the exact grammar-valid wire text.
 *
 * Everything is deterministic: no network, no sleeps. The single seam is the
 * global `fetch`, monkeypatched and restored in afterEach.
 */

import { test, expect, mock, afterEach, describe } from "bun:test";
import { EventEmitter } from "node:events";
import * as edn from "jsedn";

// ---------------------------------------------------------------------------
// node:net seam.
//
// The UDS transport reaches the network only through `net.createConnection`.
// The implementation does `import * as net from "node:net"` and calls
// `net.createConnection({ path })`, then drives the returned socket as an
// EventEmitter (`.on('data'|'end'|'close'|'error')`, `.once('connect'|'error')`,
// `.write(buf)`, `.destroy()`). We replace the whole module with `mock.module`
// so no real socket is ever opened. A module-level `currentSocket` lets each
// test hand `createConnection` the fake it wants to drive, and `lastPath`
// records the path the client asked to connect to.
//
// `mock.module` must run before `./lemma_client` is imported, so the import of
// the module under test comes AFTER this block.
// ---------------------------------------------------------------------------

/**
 * An EventEmitter standing in for a `net.Socket`. `.write()` captures the exact
 * bytes the client put on the wire; `.destroy()` records the close. Tests drive
 * the lifecycle by emitting `'connect'` / `'data'` / `'end'` / `'close'` /
 * `'error'` themselves, so every test is fully deterministic.
 */
class FakeSocket extends EventEmitter {
  writes: Buffer[] = [];
  destroyed = false;
  // The watch demo arms a socket-level read timeout. We record the requested
  // timeout but never fire it (tests are deterministic and feed every frame), so
  // a real timer can never leak across tests or stall the suite.
  socketTimeoutMs: number | null = null;

  write(chunk: Buffer | string): boolean {
    this.writes.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    return true;
  }

  setTimeout(ms: number, _onTimeout?: () => void): this {
    this.socketTimeoutMs = ms;
    return this;
  }

  destroy(): this {
    this.destroyed = true;
    return this;
  }

  /** Concatenate every captured write -- the full outbound byte stream. */
  written(): Buffer {
    return Buffer.concat(this.writes);
  }
}

/**
 * A self-driving stand-in for the SSE side's raw socket.
 *
 * The HTTP watch path connects with `net.createConnection({ host, port })`,
 * waits for `'connect'`, writes a `GET .../events` request, drains response
 * headers up to the blank line, then reads a chunked body. Unlike the UDS
 * `FakeSocket` (which tests drive by hand), `main()` runs the SSE handshake
 * with no seam to step it, so this fake DRIVES ITSELF: it emits `'connect'`
 * once a connect listener attaches, and emits its canned chunked HTTP response
 * as `'data'` once the client has written the GET. Everything is scheduled on
 * the microtask queue, so it stays deterministic (no timers, no sleeps).
 *
 * `response` is the exact bytes to hand back after the GET (status line +
 * headers + `\r\n\r\n` + chunked body). `autoConnect` / `autoRespond` can be
 * disabled to exercise the connect-error and quiet-stream branches by hand.
 */
class FakeSSESocket extends EventEmitter {
  writes: Buffer[] = [];
  destroyed = false;
  autoConnect = true;
  autoRespond = true;
  responded = false;

  constructor(public response: Buffer = Buffer.alloc(0)) {
    super();
  }

  // The impl does `socket.once("connect", ...)`. Fire 'connect' on a microtask
  // as soon as a listener is registered so the awaited connect resolves.
  once(event: string | symbol, listener: (...args: unknown[]) => void): this {
    super.once(event, listener);
    if (event === "connect" && this.autoConnect) {
      queueMicrotask(() => this.emit("connect"));
    }
    return this;
  }

  // After the client writes its GET request, hand back the canned response so
  // the header drain (and the subsequent chunked read) can proceed.
  write(chunk: Buffer | string): boolean {
    this.writes.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    if (this.autoRespond && !this.responded && this.response.length) {
      this.responded = true;
      queueMicrotask(() => this.emit("data", this.response));
    }
    return true;
  }

  destroy(): this {
    this.destroyed = true;
    return this;
  }

  written(): Buffer {
    return Buffer.concat(this.writes);
  }
}

// The fake `createConnection` returns for a UDS dial. Set per test.
let currentSocket: FakeSocket | null = null;
// The fake `createConnection` returns for an SSE (host/port) dial. Set per test.
let currentSSESocket: FakeSSESocket | null = null;
// The path the client most recently asked `createConnection` to dial (UDS).
let lastPath: string | null = null;
// The {host, port} the client most recently asked to dial (SSE). null if none.
let lastHostPort: { host: string; port: number } | null = null;
// How many times `createConnection` was called for a UDS path connection.
let connectCalls = 0;
// How many times `createConnection` was called for an SSE host/port connection.
let sseConnectCalls = 0;

mock.module("node:net", () => ({
  // Route by connection target. A `{ path }` opts is the UDS transport and
  // returns the UDS fake; a `{ host, port }` opts is the HTTP SSE GET and
  // returns the self-driving SSE fake. This keeps BOTH watch transports off any
  // real socket.
  createConnection: (opts: { path?: string; host?: string; port?: number }) => {
    if (opts.path !== undefined) {
      connectCalls += 1;
      lastPath = opts.path;
      const sock = currentSocket ?? new FakeSocket();
      currentSocket = sock;
      return sock;
    }
    sseConnectCalls += 1;
    lastHostPort = { host: opts.host!, port: opts.port! };
    const sock = currentSSESocket ?? new FakeSSESocket();
    currentSSESocket = sock;
    return sock;
  },
}));

import {
  post_edn,
  main,
  main_uds,
  _dispatch,
  query_all,
  read_welcome,
  within_message_limit,
  uds_send_frame,
  uds_await_watch_event,
  open_sse_stream,
  read_sse_events,
  DEFAULT_BASE,
  DEFAULT_SOCKET,
  entity,
  world,
  fact,
} from "./lemma_client";

// ---------------------------------------------------------------------------
// fetch seam: capture/replay helpers, restored after every test.
// ---------------------------------------------------------------------------

const realFetch = globalThis.fetch;
const realLog = console.log;

afterEach(() => {
  globalThis.fetch = realFetch;
  console.log = realLog;
  // Reset the net seam between tests so no fake socket / path / counter leaks.
  currentSocket = null;
  currentSSESocket = null;
  lastPath = null;
  lastHostPort = null;
  connectCalls = 0;
  sseConnectCalls = 0;
});

/** A captured outbound fetch call: its URL and RequestInit. */
interface Capture {
  url: string;
  init: RequestInit;
}

/**
 * Install a `fetch` that records every call into `captures` and returns the
 * supplied Response for each. The response factory is invoked per call so a
 * fresh Response (with an unconsumed body) is handed back each time.
 */
function captureFetch(
  captures: Capture[],
  makeResponse: () => Response,
): void {
  globalThis.fetch = mock(async (url: string | URL | Request, init?: RequestInit) => {
    captures.push({ url: String(url), init: init ?? {} });
    return makeResponse();
  }) as unknown as typeof fetch;
}

/** Header lookup that is robust to header-key casing. */
function headerOf(init: RequestInit, name: string): string | null {
  const headers = (init.headers ?? {}) as Record<string, string>;
  const want = name.toLowerCase();
  for (const [k, v] of Object.entries(headers)) {
    if (k.toLowerCase() === want) return v;
  }
  return null;
}

// Canned EDN reply bodies for a full successful handshake.
const WELCOME =
  '{:event :welcome :version 1 :session #session "s-1" :world #world "default"' +
  ' :capabilities #{:lemma/cursor-pagination :lemma/watch :lemma/v1}' +
  ' :limits {:max-message-bytes 1048576}}';
const WORLD_SELECTED = '{:event :world-selected :world #world "default"}';
const PROPOSED = '{:event :proposed :proposal #proposal "p-1"}';
const ASSERTED = '{:event :asserted}';
const RESULT = '{:event :result :rows [["venus"]] :done? true}';

// The paginated tail the round-trip now runs after the single query: a second
// propose (the 3x subset-of batch), its assert, then a :limit 2 query whose
// first page is NOT done and carries a #cursor, followed by a (continue #cursor)
// page that IS done. Two rows on page one, one on page two -> 3 rows / 2 pages.
const PROPOSED_BATCH = '{:event :proposed :proposal #proposal "p-2"}';
const PAGE_ONE =
  '{:event :result :rows [["sub-a"] ["sub-b"]] :done? false :cursor #cursor "c-1"}';
const PAGE_TWO = '{:event :result :rows [["sub-c"]] :done? true}';
// The four extra replies the paginated tail consumes, in order.
const PAGED_TAIL = [PROPOSED_BATCH, ASSERTED, PAGE_ONE, PAGE_TWO];

// ---------------------------------------------------------------------------
// Watch-tail fixtures.
//
// The default WELCOME advertises :lemma/watch, so main()/main_uds now run the
// watch demo after the paginated tail. The COMMAND replies (watch-pattern,
// probe propose, probe assert, unwatch) ride the normal reply channel -- fetch
// over HTTP, frames over UDS. The :watch-event itself does NOT: over HTTP it
// arrives on the mocked SSE socket's chunked body; over UDS it is interleaved
// as an extra frame among the command replies.
// ---------------------------------------------------------------------------

// watch-pattern -> :watch-established carrying a #watch handle to unwatch with.
const WATCH_ESTABLISHED = '{:event :watch-established :watch #watch "w-1"}';
// The unique probe propose -> a fresh #proposal handle, then its assert -> :ok.
const WATCH_PROBE_PROPOSED = '{:event :proposed :proposal #proposal "p-probe"}';
const UNWATCH_OK = '{:event :ok}';
// The pushed delta the watch fires. Over HTTP it comes via SSE; over UDS it is
// a frame demuxed out of the command stream by uds_await_watch_event.
const WATCH_EVENT =
  '{:event :watch-event :type :asserted' +
  ' :data #fact {:predicate subset-of :subject #entity "watch-probe-x"' +
  ' :object #entity "group"}}';

// The HTTP watch tail's four COMMAND replies, in fetch order. The :watch-event
// is delivered out-of-band on the SSE socket, so it is NOT in this list.
const HTTP_WATCH_TAIL = [
  WATCH_ESTABLISHED, WATCH_PROBE_PROPOSED, ASSERTED, UNWATCH_OK,
];
// The UDS watch tail interleaves the :watch-event frame after the probe assert
// (where uds_await_watch_event starts draining) and before the unwatch reply.
const UDS_WATCH_TAIL = [
  WATCH_ESTABLISHED, WATCH_PROBE_PROPOSED, ASSERTED, WATCH_EVENT, UNWATCH_OK,
];

/**
 * Build a chunked HTTP/1.1 SSE response body for `events`.
 *
 * Mirrors how Dianoia (http-kit) serves the stream: a status line, the
 * event-stream headers, the blank-line terminator, then a `Transfer-Encoding:
 * chunked` body. The FIRST chunk is a size-0 header-flush keep-alive (which a
 * naive reader would mistake for EOF -- the impl must treat it as a keep-alive
 * and keep reading). Each event becomes one chunk whose body is
 * `data: <edn>\n\n`. A trailing `:`-comment keep-alive chunk is included to
 * prove comment lines are skipped. No terminating size-0/EOF is written, so the
 * reader stops on maxEvents, not on stream end.
 */
function chunkedSSE(events: string[]): Buffer {
  const head =
    "HTTP/1.1 200 OK\r\n" +
    "Content-Type: text/event-stream\r\n" +
    "Transfer-Encoding: chunked\r\n" +
    "\r\n";
  const parts: string[] = [head];
  const chunk = (s: string) =>
    `${Buffer.byteLength(s, "utf-8").toString(16)}\r\n${s}\r\n`;
  // http-kit's immediate size-0 header-flush keep-alive.
  parts.push("0\r\n\r\n");
  for (const evt of events) {
    parts.push(chunk(`data: ${evt}\n\n`));
  }
  // A `:`-comment keep-alive line -- must be ignored, not parsed as an event.
  parts.push(chunk(": keep-alive\n\n"));
  return Buffer.from(parts.join(""), "utf-8");
}

// ===========================================================================
// (A) post_edn -- request shape, happy path, error-envelope recovery.
// ===========================================================================

describe("post_edn request shape", () => {
  test("posts to base+path with method POST", async () => {
    const captures: Capture[] = [];
    captureFetch(captures, () => new Response("{:event :result}"));

    await post_edn("/v1/messages", new edn.List([new edn.Symbol("hello")]),
      null, "http://example.test:9999");

    expect(captures).toHaveLength(1);
    expect(captures[0].url).toBe("http://example.test:9999/v1/messages");
    expect(captures[0].init.method).toBe("POST");
  });

  test("sends an application/edn content-type", async () => {
    const captures: Capture[] = [];
    captureFetch(captures, () => new Response("{:event :result}"));

    await post_edn("/v1/messages", new edn.List([new edn.Symbol("hello")]));

    expect(headerOf(captures[0].init, "content-type")).toBe("application/edn");
  });

  test("encodes the form as the request body via edn.encode", async () => {
    const captures: Capture[] = [];
    captureFetch(captures, () => new Response("{:event :result}"));
    const form = new edn.List([new edn.Symbol("hello")]);

    await post_edn("/v1/messages", form);

    expect(captures[0].init.body).toBe(edn.encode(form));
  });

  test("omits the x-lemma-session request header when no session is given", async () => {
    const captures: Capture[] = [];
    captureFetch(captures, () => new Response("{:event :result}"));

    await post_edn("/v1/messages", new edn.List([new edn.Symbol("hello")]));

    expect(headerOf(captures[0].init, "x-lemma-session")).toBeNull();
  });

  test("sends the x-lemma-session request header when a session is given", async () => {
    const captures: Capture[] = [];
    captureFetch(captures, () => new Response("{:event :result}"));

    await post_edn(
      "/v1/sessions/s-1/messages",
      new edn.List([new edn.Symbol("query")]),
      "s-1",
    );

    expect(headerOf(captures[0].init, "x-lemma-session")).toBe("s-1");
  });
});

describe("post_edn happy 2xx path", () => {
  test("parses the EDN response body into the same value edn.parse yields", async () => {
    captureFetch([], () => new Response(WELCOME, {
      headers: { "x-lemma-session": "s-1" },
    }));

    const { body } = await post_edn("/v1/messages",
      new edn.List([new edn.Symbol("hello")]));

    // Re-encoding both sides normalises any key/encoding-order differences.
    expect(edn.encode(body)).toBe(edn.encode(edn.parse(WELCOME)));
  });

  test("returns the session id from the X-Lemma-Session response header", async () => {
    captureFetch([], () => new Response(WELCOME, {
      headers: { "X-Lemma-Session": "s-1" },
    }));

    const { sessionId } = await post_edn("/v1/messages",
      new edn.List([new edn.Symbol("hello")]));

    expect(sessionId).toBe("s-1");
  });

  test("returns null sessionId when the response omits the header", async () => {
    captureFetch([], () => new Response("{:event :result}"));

    const { sessionId } = await post_edn("/v1/messages",
      new edn.List([new edn.Symbol("hello")]));

    expect(sessionId).toBeNull();
  });
});

describe("post_edn non-2xx error envelope", () => {
  test("parses and returns a 4xx EDN error envelope instead of throwing", async () => {
    const errorBody =
      '{:event :error :reason :malformed :message "bad verb form"}';
    captureFetch([], () => new Response(errorBody, { status: 400 }));

    const { body } = await post_edn("/v1/messages",
      new edn.List([new edn.Symbol("hello")]));

    expect(body instanceof edn.Map).toBe(true);
    const event = (body as edn.Map).at(new edn.Keyword(":event"));
    expect(edn.encode(event)).toBe(":error");
  });

  test("surfaces the session header even on a non-2xx response", async () => {
    const errorBody = '{:event :error :reason :malformed}';
    captureFetch([], () => new Response(errorBody, {
      status: 400,
      headers: { "x-lemma-session": "s-99" },
    }));

    const { sessionId } = await post_edn("/v1/messages",
      new edn.List([new edn.Symbol("hello")]));

    expect(sessionId).toBe("s-99");
  });
});

describe("post_edn connection failure", () => {
  test("translates a fetch rejection into an actionable error naming the base", async () => {
    globalThis.fetch = mock(async () => {
      throw new Error("Connection refused");
    }) as unknown as typeof fetch;

    const promise = post_edn("/v1/messages",
      new edn.List([new edn.Symbol("hello")]), null, "http://down.test:1234");

    await expect(promise).rejects.toThrow("http://down.test:1234");
    await expect(promise).rejects.toThrow("is the server running?");
  });
});

// ===========================================================================
// (B) main() handshake -- scripted fetch returning canned EDN in sequence.
// ===========================================================================

/**
 * Install a `fetch` that pops the next canned EDN body off `bodies` per call,
 * recording each call. Only the FIRST (welcome) reply carries the
 * X-Lemma-Session header, mimicking the server minting the session id.
 */
function scriptFetch(captures: Capture[], bodies: string[]): void {
  let i = 0;
  globalThis.fetch = mock(async (url: string | URL | Request, init?: RequestInit) => {
    captures.push({ url: String(url), init: init ?? {} });
    const raw = bodies[i];
    const headers: Record<string, string> = i === 0
      ? { "x-lemma-session": "s-1" }
      : {};
    i += 1;
    return new Response(raw, { headers });
  }) as unknown as typeof fetch;
}

const FULL_SEQUENCE = [
  WELCOME, WORLD_SELECTED, PROPOSED, ASSERTED, RESULT,
  ...PAGED_TAIL, ...HTTP_WATCH_TAIL,
];
// The full round-trip now makes thirteen fetch calls: the original five, the
// four paginated-tail calls (batch propose, assert, page one, continue page
// two), and the four watch-tail COMMAND calls (watch-pattern, probe propose,
// probe assert, unwatch). The :watch-event is delivered out-of-band on the SSE
// socket, so it is NOT a fetch call.
const FULL_SEQUENCE_LEN = FULL_SEQUENCE.length;

/**
 * Arm the SSE seam so the HTTP watch path's `open_sse_stream` gets a
 * self-driving socket that hands back a one-event chunked stream. Call before
 * any test that runs main() to completion through the watch demo.
 */
function armSSE(events: string[] = [WATCH_EVENT]): FakeSSESocket {
  const sse = new FakeSSESocket(chunkedSSE(events));
  currentSSESocket = sse;
  return sse;
}

describe("main() handshake", () => {
  test("completes the full round-trip (handshake + paged query + watch) without throwing", async () => {
    const captures: Capture[] = [];
    scriptFetch(captures, FULL_SEQUENCE);
    armSSE();

    await expect(main()).resolves.toBeUndefined();
    expect(captures).toHaveLength(FULL_SEQUENCE_LEN);
  });

  test("drains the paginated query across pages and prints rows-over-pages", async () => {
    const lines: string[] = [];
    console.log = ((...args: unknown[]) => {
      lines.push(args.map(String).join(" "));
    }) as typeof console.log;
    const captures: Capture[] = [];
    scriptFetch(captures, FULL_SEQUENCE);
    armSSE();

    await main();

    // The :limit 2 query (call index 7) is followed by exactly one
    // (continue #cursor "c-1") (index 8) before :done? -> two pages, three rows.
    const continueBody = captures[8].init.body as string;
    expect(continueBody).toBe(
      edn.encode(new edn.List([
        new edn.Symbol("continue"), edn.parse('#cursor "c-1"'),
      ])),
    );
    const output = lines.join("\n");
    expect(output).toContain("paged query (subset-of ? group), limit 2 -> 3 rows over 2 page(s)");
  });

  test("opens with an anonymous (hello) on the /v1/messages endpoint", async () => {
    const captures: Capture[] = [];
    scriptFetch(captures, FULL_SEQUENCE);
    armSSE();

    await main();

    expect(captures[0].url).toBe(`${DEFAULT_BASE}/v1/messages`);
    expect(captures[0].init.body).toBe(
      edn.encode(new edn.List([new edn.Symbol("hello")])),
    );
    expect(headerOf(captures[0].init, "x-lemma-session")).toBeNull();
  });

  test("threads the welcome-header session id into the named-session endpoint", async () => {
    const captures: Capture[] = [];
    scriptFetch(captures, FULL_SEQUENCE);
    armSSE();

    await main();

    // Every call after the hello targets the named-session endpoint built
    // from the X-Lemma-Session header value, and echoes it back.
    for (const cap of captures.slice(1)) {
      expect(cap.url).toBe(`${DEFAULT_BASE}/v1/sessions/s-1/messages`);
      expect(headerOf(cap.init, "x-lemma-session")).toBe("s-1");
    }
  });

  test("threads the #proposal handle from the propose reply into the assert body", async () => {
    const captures: Capture[] = [];
    scriptFetch(captures, FULL_SEQUENCE);
    armSSE();

    await main();

    // Call index 3 is the assert; its body is (assert #proposal "p-1") where
    // the handle is exactly what the propose reply (index 2) returned.
    const assertBody = captures[3].init.body as string;
    const expected = edn.encode(
      new edn.List([new edn.Symbol("assert"), edn.parse('#proposal "p-1"')]),
    );
    expect(assertBody).toBe(expected);
  });

  test("threads the base url through every call", async () => {
    const captures: Capture[] = [];
    scriptFetch(captures, FULL_SEQUENCE);
    armSSE();

    await main("http://example.test:9999");

    for (const cap of captures) {
      expect(cap.url.startsWith("http://example.test:9999")).toBe(true);
    }
  });

  test("prints the result line once the query returns rows", async () => {
    const lines: string[] = [];
    console.log = ((...args: unknown[]) => {
      lines.push(args.map(String).join(" "));
    }) as typeof console.log;
    scriptFetch([], FULL_SEQUENCE);
    armSSE();

    await main();

    const output = lines.join("\n");
    expect(output).toContain("rows=");
    expect(output).toContain('"venus"');
  });
});

describe("main() failure paths stop cleanly", () => {
  test("a non-:welcome first reply stops after one call without throwing", async () => {
    const captures: Capture[] = [];
    scriptFetch(captures, ['{:event :error :reason :malformed :message "bad"}']);

    await expect(main()).resolves.toBeUndefined();
    expect(captures).toHaveLength(1);
  });

  test("a :rejected use-world reply stops after two calls without throwing", async () => {
    const captures: Capture[] = [];
    scriptFetch(captures, [
      WELCOME,
      '{:event :rejected :reason :inconsistent}',
    ]);

    await expect(main()).resolves.toBeUndefined();
    expect(captures).toHaveLength(2);
  });

  test("an :error propose reply stops after three calls without throwing", async () => {
    const captures: Capture[] = [];
    scriptFetch(captures, [
      WELCOME,
      WORLD_SELECTED,
      '{:event :error :reason :malformed :message "bad"}',
    ]);

    await expect(main()).resolves.toBeUndefined();
    expect(captures).toHaveLength(3);
  });
});

// ===========================================================================
// (C) EDN sanity -- the verb form encodes to exact grammar-valid wire text.
// ===========================================================================

describe("EDN tagged-literal encoding", () => {
  test("(use-world #world \"default\") encodes to the exact wire text", () => {
    const form = new edn.List([new edn.Symbol("use-world"), world("default")]);

    expect(edn.encode(form)).toBe('(use-world #world "default")');
  });

  test("an #entity handle round-trips through encode/parse", () => {
    const e = entity("alice");

    expect(edn.encode(edn.parse(edn.encode(e)))).toBe(edn.encode(e));
    expect(edn.encode(e)).toBe('#entity "alice"');
  });

  test("a #fact form encodes its predicate/subject/object map", () => {
    const f = fact(
      new edn.Symbol("equivalent"),
      entity("morningstar"),
      entity("venus"),
    );

    // Reparse-equality guards against key/encoding-order differences.
    expect(edn.encode(edn.parse(edn.encode(f)))).toBe(edn.encode(f));
    const encoded = edn.encode(f);
    expect(encoded.startsWith("#fact ")).toBe(true);
    expect(encoded).toContain(":predicate equivalent");
    expect(encoded).toContain(':subject #entity "morningstar"');
    expect(encoded).toContain(':object #entity "venus"');
  });
});

// ===========================================================================
// (C2) query_all -- cursor pagination driven by a SCRIPTED in-memory `send`.
//
// query_all(send, queryForm) is transport-agnostic: `send` is a
// `form -> Promise<body>` closure. These tests exercise the pagination loop in
// isolation -- no fetch, no socket -- by handing it a closure over a queue of
// canned reply bodies built with `edn.parse` (so the #cursor it feeds back is
// the SAME edn.Tagged the server "sent"). The closure records every form it was
// called with so we can assert what query_all put back on the wire.
//
// The handshake-level main()/main_uds tests already assert the happy two-page
// drain at integration level; these add the dedicated unit coverage for the
// single-page, mid-stream-failure, and initial-failure branches plus the exact
// (continue #cursor) form, without re-driving the handshake fixtures.
// ===========================================================================

/**
 * Build a scripted `send`: a `form -> Promise<body>` closure over a queue of
 * canned EDN reply strings. Each string is `edn.parse`d on the way out so the
 * `#cursor` carried on a page is a genuine `edn.Tagged` that query_all can feed
 * straight back. Every form the closure receives is pushed onto `forms` so the
 * test can inspect what query_all sent. No network of any kind.
 */
function scriptedSend(
  bodies: string[],
  forms: unknown[],
): (form: unknown) => Promise<unknown> {
  let i = 0;
  return (form: unknown) => {
    forms.push(form);
    const raw = bodies[i];
    i += 1;
    if (raw === undefined) {
      throw new Error(
        `scripted send exhausted: query_all made more calls than scripted ` +
          `(${i} calls, ${bodies.length} bodies)`,
      );
    }
    return Promise.resolve(edn.parse(raw));
  };
}

describe("query_all pagination", () => {
  test("(A) concatenates rows across two pages in order", async () => {
    const forms: unknown[] = [];
    const send = scriptedSend(
      [
        '{:event :result :rows [[#entity "a"] [#entity "b"]] :done? false :cursor #cursor "c-1"}',
        '{:event :result :rows [[#entity "c"]] :done? true}',
      ],
      forms,
    );

    const result = await query_all(send, new edn.List([new edn.Symbol("query")]));

    expect(result.rows).toHaveLength(3);
    expect(result.rows.map((r) => edn.encode(r))).toEqual([
      '[#entity "a"]',
      '[#entity "b"]',
      '[#entity "c"]',
    ]);
  });

  test("(A) reports two pages for a two-page drain", async () => {
    const forms: unknown[] = [];
    const send = scriptedSend(
      [
        '{:event :result :rows [[#entity "a"] [#entity "b"]] :done? false :cursor #cursor "c-1"}',
        '{:event :result :rows [[#entity "c"]] :done? true}',
      ],
      forms,
    );

    const result = await query_all(send, new edn.List([new edn.Symbol("query")]));

    expect(result.pages).toBe(2);
  });

  test("(A) reports no failure on a successful drain", async () => {
    const forms: unknown[] = [];
    const send = scriptedSend(
      [
        '{:event :result :rows [[#entity "a"] [#entity "b"]] :done? false :cursor #cursor "c-1"}',
        '{:event :result :rows [[#entity "c"]] :done? true}',
      ],
      forms,
    );

    const result = await query_all(send, new edn.List([new edn.Symbol("query")]));

    expect(result.failure).toBeFalsy();
  });

  test("(A) the second send is (continue #cursor) carrying page one's cursor", async () => {
    const forms: unknown[] = [];
    const send = scriptedSend(
      [
        '{:event :result :rows [[#entity "a"] [#entity "b"]] :done? false :cursor #cursor "c-1"}',
        '{:event :result :rows [[#entity "c"]] :done? true}',
      ],
      forms,
    );

    await query_all(send, new edn.List([new edn.Symbol("query")]));

    expect(forms).toHaveLength(2);
    expect(edn.encode(forms[1])).toBe(
      edn.encode(new edn.List([
        new edn.Symbol("continue"), edn.parse('#cursor "c-1"'),
      ])),
    );
  });

  test("(B) a single done page returns one page and never sends a continue", async () => {
    const forms: unknown[] = [];
    const send = scriptedSend(
      ['{:event :result :rows [[#entity "a"]] :done? true}'],
      forms,
    );

    const result = await query_all(send, new edn.List([new edn.Symbol("query")]));

    expect(result.pages).toBe(1);
    expect(result.rows).toHaveLength(1);
    expect(result.failure).toBeFalsy();
    expect(forms).toHaveLength(1); // exactly one call: no (continue ...)
  });

  test("(B) does not throw on a done page that omits :cursor", async () => {
    const forms: unknown[] = [];
    const send = scriptedSend(
      ['{:event :result :rows [[#entity "a"]] :done? true}'],
      forms,
    );

    await expect(
      query_all(send, new edn.List([new edn.Symbol("query")])),
    ).resolves.toBeDefined();
  });

  test("(C) propagates a mid-stream continue failure with rows gathered so far", async () => {
    const forms: unknown[] = [];
    const send = scriptedSend(
      [
        '{:event :result :rows [[#entity "a"]] :done? false :cursor #cursor "c-1"}',
        '{:event :error :reason :unknown-handle}',
      ],
      forms,
    );

    const result = await query_all(send, new edn.List([new edn.Symbol("query")]));

    // rows-so-far survive; pages counts only the fully-fetched first page.
    expect(result.rows.map((r) => edn.encode(r))).toEqual(['[#entity "a"]']);
    expect(result.pages).toBe(1);
    expect(result.failure).not.toBeNull();
    expect(
      edn.encode((result.failure as edn.Map).at(new edn.Keyword(":reason"))),
    ).toBe(":unknown-handle");
  });

  test("(C) a mid-stream continue failure does not throw", async () => {
    const forms: unknown[] = [];
    const send = scriptedSend(
      [
        '{:event :result :rows [[#entity "a"]] :done? false :cursor #cursor "c-1"}',
        '{:event :error :reason :unknown-handle}',
      ],
      forms,
    );

    await expect(
      query_all(send, new edn.List([new edn.Symbol("query")])),
    ).resolves.toBeDefined();
  });

  test("(C) an initial-reply failure returns no rows, zero pages, and the failure body", async () => {
    const forms: unknown[] = [];
    const send = scriptedSend(
      ['{:event :error :reason :malformed :message "bad verb form"}'],
      forms,
    );

    const result = await query_all(send, new edn.List([new edn.Symbol("query")]));

    expect(result.rows).toEqual([]);
    expect(result.pages).toBe(0);
    expect(result.failure).not.toBeNull();
    // Only the initial query was sent -- no continue attempted past a failure.
    expect(forms).toHaveLength(1);
  });

  test("(C) a :rejected initial reply is also surfaced as a failure", async () => {
    const forms: unknown[] = [];
    const send = scriptedSend(
      ['{:event :rejected :reason :inconsistent}'],
      forms,
    );

    const result = await query_all(send, new edn.List([new edn.Symbol("query")]));

    expect(result.pages).toBe(0);
    expect(result.rows).toEqual([]);
    expect(
      edn.encode((result.failure as edn.Map).at(new edn.Keyword(":event"))),
    ).toBe(":rejected");
  });
});

// ===========================================================================
// (D) UDS framing helpers and shared fakes.
// ===========================================================================

/**
 * Build the on-the-wire frame for an EDN body exactly as `uds_send_frame`
 * should: a 4-byte big-endian length prefix followed by the UTF-8 body. Used to
 * synthesise canned server replies and to assert outbound framing.
 */
function frameOf(ednStr: string): Buffer {
  const body = Buffer.from(ednStr, "utf-8");
  const frame = Buffer.alloc(4 + body.length);
  frame.writeUInt32BE(body.length, 0);
  body.copy(frame, 4);
  return frame;
}

/** Decode one length-prefixed frame at `offset`, returning body + next offset. */
function readFrame(buf: Buffer, offset: number): { body: string; next: number } {
  const length = buf.readUInt32BE(offset);
  const body = buf.subarray(offset + 4, offset + 4 + length).toString("utf-8");
  return { body, next: offset + 4 + length };
}

/** Split a buffer into all frames it contains (assumes well-formed framing). */
function decodeFrames(buf: Buffer): string[] {
  const out: string[] = [];
  let off = 0;
  while (off < buf.length) {
    const { body, next } = readFrame(buf, off);
    out.push(body);
    off = next;
  }
  return out;
}

// The five canned UDS replies, mirroring the HTTP handshake. Note the UDS
// welcome carries the session as a #session-tagged field (connection-bound),
// NOT as a header.
const UDS_WELCOME =
  '{:event :welcome :version 1 :session #session "s-1" :world #world "default"' +
  ' :capabilities #{:lemma/cursor-pagination :lemma/watch :lemma/v1}' +
  ' :limits {:max-message-bytes 1048576}}';
const UDS_FULL = [
  UDS_WELCOME, WORLD_SELECTED, PROPOSED, ASSERTED, RESULT,
  ...PAGED_TAIL, ...UDS_WATCH_TAIL,
];
// UDS_FULL is the list of REPLIES the server emits (14: the watch tail's five
// includes the interleaved :watch-event). The client SENDS one fewer frame than
// it receives, because uds_await_watch_event reads the :watch-event WITHOUT
// sending a command for it. So the sent-frame count is 13:
//   1 hello + 4 handshake + 4 paged tail + 4 watch tail (watch-pattern, probe
//   propose, probe assert, unwatch).
const UDS_FULL_SENT = 13;

/** Silence console.log for a test body, restored by the shared afterEach. */
function muteLog(): void {
  console.log = (() => {}) as typeof console.log;
}

// ===========================================================================
// (A) Framing: uds_send_frame byte layout + FrameReader buffering/EOF.
//
// The FrameReader is internal, so its split / coalesce / EOF behaviour is
// exercised through main_uds, which feeds replies into it via 'data' and
// observes the result. uds_send_frame is exported and tested directly.
// ===========================================================================

describe("uds_send_frame byte layout", () => {
  test("writes a 4-byte big-endian length prefix equal to the UTF-8 body length", () => {
    const sock = new FakeSocket();
    const form = '{:event :welcome}';

    uds_send_frame(sock as unknown as import("node:net").Socket, form);

    const written = sock.written();
    const bodyLen = Buffer.from(form, "utf-8").length;
    expect(written.readUInt32BE(0)).toBe(bodyLen);
    expect(written.length).toBe(4 + bodyLen);
  });

  test("writes the UTF-8 body verbatim after the prefix", () => {
    const sock = new FakeSocket();
    const form = '(hello)';

    uds_send_frame(sock as unknown as import("node:net").Socket, form);

    const written = sock.written();
    expect(written.subarray(4).toString("utf-8")).toBe(form);
  });

  test("frames an edn.encode(form) so the prefix matches the encoded byte length", () => {
    const sock = new FakeSocket();
    const form = new edn.List([new edn.Symbol("hello")]);
    const encoded = edn.encode(form);

    uds_send_frame(sock as unknown as import("node:net").Socket, encoded);

    const written = sock.written();
    expect(written.readUInt32BE(0)).toBe(Buffer.from(encoded, "utf-8").length);
    expect(written.subarray(4).toString("utf-8")).toBe(encoded);
  });

  test("preserves multi-byte UTF-8: prefix counts bytes, not characters", () => {
    const sock = new FakeSocket();
    const form = '{:msg "héllo-✓"}'; // contains multi-byte code points

    uds_send_frame(sock as unknown as import("node:net").Socket, form);

    const written = sock.written();
    const byteLen = Buffer.from(form, "utf-8").length;
    expect(byteLen).toBeGreaterThan(form.length); // proves multi-byte present
    expect(written.readUInt32BE(0)).toBe(byteLen);
    expect(written.subarray(4).toString("utf-8")).toBe(form);
  });
});

describe("FrameReader buffering (via main_uds)", () => {
  test("reassembles a frame delivered SPLIT across multiple data chunks", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    const run = main_uds(DEFAULT_SOCKET);
    muteLog();

    // The welcome frame arrives byte-dribbled across several 'data' emissions
    // (prefix split from body, body split mid-way) -- the reader must buffer.
    const welcome = frameOf(UDS_WELCOME);
    sock.emit("connect");
    await Promise.resolve(); // let main_uds send (hello) and park on recv()
    sock.emit("data", welcome.subarray(0, 2));
    sock.emit("data", welcome.subarray(2, 4));
    sock.emit("data", welcome.subarray(4, 9));
    sock.emit("data", welcome.subarray(9));

    // Feed the rest of the round-trip one whole frame per chunk so it completes
    // (the handshake's four replies, the paginated tail's four, and the watch
    // tail's five -- the last including the interleaved :watch-event).
    for (const reply of [WORLD_SELECTED, PROPOSED, ASSERTED, RESULT, ...PAGED_TAIL, ...UDS_WATCH_TAIL]) {
      await Promise.resolve();
      sock.emit("data", frameOf(reply));
    }

    await expect(run).resolves.toBeUndefined();
    // All round-trip frames sent and decodable despite the dribbled welcome.
    expect(decodeFrames(sock.written())).toHaveLength(UDS_FULL_SENT);
  });

  test("yields ONE frame per recv when TWO frames coalesce in a single chunk", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    const run = main_uds(DEFAULT_SOCKET);
    muteLog();

    sock.emit("connect");
    await Promise.resolve();
    // welcome AND world-selected land in one chunk: the reader must hand back
    // exactly one frame to the first recv() and keep the second buffered.
    sock.emit("data", Buffer.concat([frameOf(UDS_WELCOME), frameOf(WORLD_SELECTED)]));
    // proposed + asserted coalesced too.
    await Promise.resolve();
    sock.emit("data", Buffer.concat([frameOf(PROPOSED), frameOf(ASSERTED)]));
    await Promise.resolve();
    sock.emit("data", frameOf(RESULT));
    // Drive the paginated tail and watch tail too so the round-trip completes.
    for (const reply of [...PAGED_TAIL, ...UDS_WATCH_TAIL]) {
      await Promise.resolve();
      sock.emit("data", frameOf(reply));
    }

    await expect(run).resolves.toBeUndefined();
    expect(decodeFrames(sock.written())).toHaveLength(UDS_FULL_SENT);
  });

  test("rejects (does not hang) on a premature EOF mid-frame via 'close'", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    const run = main_uds(DEFAULT_SOCKET);
    muteLog();

    sock.emit("connect");
    await Promise.resolve(); // hello sent, recv() parked
    // Only the length prefix arrives, promising a body that never comes, then
    // the peer drops the connection. The parked recv() must reject, not hang.
    sock.emit("data", frameOf(UDS_WELCOME).subarray(0, 4));
    sock.emit("close");

    await expect(run).rejects.toThrow("connection closed");
  });

  test("rejects on a premature EOF mid-frame via 'end'", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    const run = main_uds(DEFAULT_SOCKET);
    muteLog();

    sock.emit("connect");
    await Promise.resolve();
    sock.emit("data", frameOf(UDS_WELCOME).subarray(0, 5)); // partial frame
    sock.emit("end");

    await expect(run).rejects.toThrow("EOF");
  });
});

// ===========================================================================
// (B) main_uds handshake: drive the full sequence over the fake socket.
// ===========================================================================

/**
 * Drive a full (or truncated) UDS handshake: emit 'connect', then for each
 * canned reply wait a microtask (so the client has sent its frame and parked on
 * recv()) and emit that reply as one framed 'data' chunk. Returns when the
 * main_uds promise settles.
 */
async function driveUds(sock: FakeSocket, replies: string[]): Promise<void> {
  const run = main_uds(DEFAULT_SOCKET);
  muteLog();
  sock.emit("connect");
  for (const reply of replies) {
    await Promise.resolve();
    sock.emit("data", frameOf(reply));
  }
  await run;
}

describe("main_uds handshake", () => {
  test("completes the full round-trip (handshake + paged query + watch) without throwing", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    await expect(driveUds(sock, UDS_FULL)).resolves.toBeUndefined();
    expect(decodeFrames(sock.written())).toHaveLength(UDS_FULL_SENT);
  });

  test("drains the paginated query, threading the #cursor into a (continue ...) frame", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    await driveUds(sock, UDS_FULL);

    // Sent frame index 8 is the (continue #cursor "c-1") that drains page two,
    // carrying exactly the cursor handle page one returned.
    const continueFrame = decodeFrames(sock.written())[8];
    expect(continueFrame).toBe(
      edn.encode(new edn.List([
        new edn.Symbol("continue"), edn.parse('#cursor "c-1"'),
      ])),
    );
  });

  test("opens with an anonymous (hello) frame", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    await driveUds(sock, UDS_FULL);

    const sent = decodeFrames(sock.written());
    expect(sent[0]).toBe(edn.encode(new edn.List([new edn.Symbol("hello")])));
  });

  test("never echoes a session id / #session into any frame after hello (connection-bound)", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    await driveUds(sock, UDS_FULL);

    // The welcome carried #session "s-1"; over UDS the session is bound to the
    // connection, so NO sent frame may carry a session id or #session tag.
    for (const frame of decodeFrames(sock.written())) {
      expect(frame).not.toContain("#session");
      expect(frame).not.toContain("s-1");
      expect(frame).not.toContain(":session");
    }
  });

  test("threads the #proposal from the propose reply into the assert frame", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    await driveUds(sock, UDS_FULL);

    // Sent frame index 3 is the assert; it must carry exactly the #proposal
    // handle ("p-1") the proposed reply (UDS_FULL[2]) returned.
    const assertFrame = decodeFrames(sock.written())[3];
    const expected = edn.encode(
      new edn.List([new edn.Symbol("assert"), edn.parse('#proposal "p-1"')]),
    );
    expect(assertFrame).toBe(expected);
  });

  test("closes (destroys) the socket on the way out", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    await driveUds(sock, UDS_FULL);

    expect(sock.destroyed).toBe(true);
  });

  test("connects to the socket path it was given", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    const run = main_uds("/tmp/custom.sock");
    muteLog();
    sock.emit("connect");
    for (const reply of UDS_FULL) {
      await Promise.resolve();
      sock.emit("data", frameOf(reply));
    }
    await run;

    expect(lastPath).toBe("/tmp/custom.sock");
  });

  test("stops cleanly (no throw) on a non-:welcome first reply", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    await expect(
      driveUds(sock, ['{:event :error :reason :malformed :message "bad"}']),
    ).resolves.toBeUndefined();
    // Only the hello frame was sent before the sequence bailed.
    expect(decodeFrames(sock.written())).toHaveLength(1);
    expect(sock.destroyed).toBe(true);
  });
});

// ===========================================================================
// (C) connect failure: 'error' before 'connect' rejects, naming the path.
// ===========================================================================

describe("main_uds connect failure", () => {
  test("rejects with an Error naming the socket path when the socket errors before connect", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    const run = main_uds("/no/such.sock");
    muteLog();
    // An ENOENT-like failure arrives before 'connect' ever fires.
    sock.emit("error", new Error("connect ENOENT /no/such.sock"));

    await expect(run).rejects.toThrow("/no/such.sock");
  });

  test("the rejection mentions the server-not-running hint", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    const run = main_uds("/no/such.sock");
    muteLog();
    sock.emit("error", new Error("ENOENT"));

    await expect(run).rejects.toThrow("is the server running?");
  });
});

// ===========================================================================
// (D) _dispatch routing: "uds" -> UDS connect; URL / no-arg -> HTTP main.
// ===========================================================================

describe("_dispatch routing", () => {
  test('argv ["uds", "/x.sock"] attempts a UDS connection to that path', async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    const run = _dispatch(["uds", "/x.sock"]);
    muteLog();
    sock.emit("connect");
    for (const reply of UDS_FULL) {
      await Promise.resolve();
      sock.emit("data", frameOf(reply));
    }
    await run;

    expect(connectCalls).toBe(1);
    expect(lastPath).toBe("/x.sock");
  });

  test('argv ["uds"] alone defaults to DEFAULT_SOCKET', async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    const run = _dispatch(["uds"]);
    muteLog();
    sock.emit("connect");
    for (const reply of UDS_FULL) {
      await Promise.resolve();
      sock.emit("data", frameOf(reply));
    }
    await run;

    expect(lastPath).toBe(DEFAULT_SOCKET);
  });

  test("a URL arg routes to HTTP main and opens NO UDS connection", async () => {
    const captures: Capture[] = [];
    scriptFetch(captures, FULL_SEQUENCE);
    armSSE();

    await _dispatch(["http://example.test:9999"]);

    expect(connectCalls).toBe(0); // no UDS dial
    // First HTTP call hit the URL arg's base.
    expect(captures[0].url.startsWith("http://example.test:9999")).toBe(true);
  });

  test("no args route to HTTP main against DEFAULT_BASE and open NO UDS connection", async () => {
    const captures: Capture[] = [];
    scriptFetch(captures, FULL_SEQUENCE);
    armSSE();

    await _dispatch([]);

    expect(connectCalls).toBe(0);
    expect(captures[0].url.startsWith(DEFAULT_BASE)).toBe(true);
  });
});

// ===========================================================================
// (E) read_welcome / ServerInfo -- parse the :welcome surface (SPEC §10).
//
// read_welcome turns a parsed :welcome map into a queryable ServerInfo:
// a Set of advertised capability flags, a :limits map, the flattened
// :verbs / :predicates surfaces, supports(), and maxMessageBytes. We feed it
// genuine `edn.parse` output (the same shapes the wire delivers) so the
// jsedn-set / jsedn-map handling is exercised for real, not mocked.
// ===========================================================================

// A realistic welcome: a :capabilities Set, a :limits Map, and :verbs /
// :predicates split into {:core #{...} :extensions {pack #{...}}} surfaces.
const RICH_WELCOME =
  '{:event :welcome :version 1 :world #world "default"' +
  ' :capabilities #{:lemma/cursor-pagination :lemma/watch :lemma/v1}' +
  ' :limits {:max-message-bytes 1048576 :max-rows 500}' +
  ' :verbs {:core #{hello propose assert query}' +
  '         :extensions {ext #{continue watch}}}' +
  ' :predicates {:core #{equivalent subset-of}' +
  '              :extensions {ext #{member-of}}}}';

describe("read_welcome / ServerInfo", () => {
  test("supports() returns true for an advertised capability", () => {
    const info = read_welcome(edn.parse(RICH_WELCOME));

    expect(info.supports(":lemma/cursor-pagination")).toBe(true);
  });

  test("supports() returns false for an unadvertised capability", () => {
    const info = read_welcome(edn.parse(RICH_WELCOME));

    expect(info.supports(":lemma/time-travel")).toBe(false);
  });

  test("capabilities Set carries every advertised flag by canonical text", () => {
    const info = read_welcome(edn.parse(RICH_WELCOME));

    expect([...info.capabilities].sort()).toEqual([
      ":lemma/cursor-pagination",
      ":lemma/v1",
      ":lemma/watch",
    ]);
  });

  test("maxMessageBytes reads the :max-message-bytes limit", () => {
    const info = read_welcome(edn.parse(RICH_WELCOME));

    expect(info.maxMessageBytes).toBe(1048576);
  });

  test("limits map carries every advertised limit keyed by canonical text", () => {
    const info = read_welcome(edn.parse(RICH_WELCOME));

    expect(info.limits.get(":max-message-bytes")).toBe(1048576);
    expect(info.limits.get(":max-rows")).toBe(500);
  });

  test("verbs flatten :core members into the verb set", () => {
    const info = read_welcome(edn.parse(RICH_WELCOME));

    for (const verb of ["hello", "propose", "assert", "query"]) {
      expect(info.verbs.has(verb)).toBe(true);
    }
  });

  test("verbs flatten :extensions pack members into the verb set", () => {
    const info = read_welcome(edn.parse(RICH_WELCOME));

    // The ext pack's verbs are merged in alongside :core (SPEC §10).
    expect(info.verbs.has("continue")).toBe(true);
    expect(info.verbs.has("watch")).toBe(true);
  });

  test("predicates flatten core + extension members into the predicate set", () => {
    const info = read_welcome(edn.parse(RICH_WELCOME));

    for (const pred of ["equivalent", "subset-of", "member-of"]) {
      expect(info.predicates.has(pred)).toBe(true);
    }
  });

  test("a welcome with NO :limits yields undefined maxMessageBytes (no throw)", () => {
    const noLimits =
      '{:event :welcome :version 1' +
      ' :capabilities #{:lemma/cursor-pagination}}';

    const info = read_welcome(edn.parse(noLimits));

    expect(info.maxMessageBytes).toBeUndefined();
    expect(info.limits.size).toBe(0);
  });

  test("a minimal welcome (no caps/verbs/predicates) yields empty sets, not a throw", () => {
    const minimal = '{:event :welcome :version 1}';

    const info = read_welcome(edn.parse(minimal));

    expect(info.capabilities.size).toBe(0);
    expect(info.verbs.size).toBe(0);
    expect(info.predicates.size).toBe(0);
    expect(info.supports(":lemma/cursor-pagination")).toBe(false);
  });

  test("an :extensions-only verb surface is flattened into the verb set", () => {
    // No :core key at all -- only an extensions pack. flattenSurface must still
    // merge the pack's members rather than skipping the whole surface.
    const extOnly =
      '{:event :welcome :version 1' +
      ' :verbs {:extensions {pack #{foo bar}}}}';

    const info = read_welcome(edn.parse(extOnly));

    expect(info.verbs.has("foo")).toBe(true);
    expect(info.verbs.has("bar")).toBe(true);
  });
});

// ===========================================================================
// (F) within_message_limit -- UTF-8 byte cap enforcement (SPEC §10).
// ===========================================================================

describe("within_message_limit", () => {
  test("a small message under a 1 MB limit passes", () => {
    const info = read_welcome(edn.parse(RICH_WELCOME)); // 1 MB cap

    expect(within_message_limit(info, "(hello)")).toBe(true);
  });

  test("a message exactly at the byte cap passes (boundary, <=)", () => {
    // A welcome whose cap equals the message's exact UTF-8 byte length.
    const msg = "(hello)"; // 7 ASCII bytes
    const info = read_welcome(
      edn.parse('{:event :welcome :limits {:max-message-bytes 7}}'),
    );

    expect(Buffer.byteLength(msg, "utf8")).toBe(7);
    expect(within_message_limit(info, msg)).toBe(true);
  });

  test("an oversize message under a tiny limit fails", () => {
    const info = read_welcome(
      edn.parse('{:event :welcome :limits {:max-message-bytes 4}}'),
    );

    expect(within_message_limit(info, "(hello)")).toBe(false);
  });

  test("counts UTF-8 bytes, not characters, against the cap", () => {
    // "✓" is three UTF-8 bytes; one such char already exceeds a 2-byte cap.
    const info = read_welcome(
      edn.parse('{:event :welcome :limits {:max-message-bytes 2}}'),
    );

    expect(within_message_limit(info, "✓")).toBe(false);
  });

  test("an unadvertised limit (no :limits) treats any message as within limit", () => {
    const info = read_welcome(edn.parse('{:event :welcome :version 1}'));

    expect(info.maxMessageBytes).toBeUndefined();
    expect(within_message_limit(info, "x".repeat(10_000_000))).toBe(true);
  });
});

// ===========================================================================
// (G) Capability gating -- the paged tail runs IFF :lemma/cursor-pagination is
// advertised. We drive main()/main_uds with a welcome whose :capabilities OMITS
// cursor-pagination and assert the whole paginated block is skipped: the skip
// note is printed, no (continue ...) / batch propose is sent, and the call /
// frame count is the bare five of the handshake -- strictly fewer than the nine
// of the full run. The capability-present path is the (now-fixed) default
// WELCOME / UDS_WELCOME fixtures already driven by the handshake tests above.
// ===========================================================================

// A welcome advertising NEITHER :lemma/cursor-pagination NOR :lemma/watch. The
// single (query ...) still runs; both the paginated tail AND the watch demo must
// be gated off, so the whole round-trip is the bare five-call handshake.
const WELCOME_NO_PAGINATION =
  '{:event :welcome :version 1 :session #session "s-1" :world #world "default"' +
  ' :capabilities #{:lemma/v1}' +
  ' :limits {:max-message-bytes 1048576}}';
const UDS_WELCOME_NO_PAGINATION = WELCOME_NO_PAGINATION;

// The bare handshake (no paginated tail, no watch tail): hello, use-world,
// propose, assert, query. Five calls -- what a server advertising neither
// optional capability should elicit.
const HANDSHAKE_ONLY = [WORLD_SELECTED, PROPOSED, ASSERTED, RESULT];

describe("capability gating: cursor-pagination absent", () => {
  test("HTTP main() SKIPS the paged query when cursor-pagination is unadvertised", async () => {
    const lines: string[] = [];
    console.log = ((...args: unknown[]) => {
      lines.push(args.map(String).join(" "));
    }) as typeof console.log;
    const captures: Capture[] = [];
    scriptFetch(captures, [WELCOME_NO_PAGINATION, ...HANDSHAKE_ONLY]);

    await main();

    // Exactly the five handshake calls -- the four paginated-tail calls are gated.
    expect(captures).toHaveLength(5);
    expect(captures.length).toBeLessThan(FULL_SEQUENCE_LEN);
    const output = lines.join("\n");
    expect(output).toContain("server does not advertise cursor pagination; skipping paged query");
    // The skip is real: no batch propose, no (continue ...) ever went out.
    for (const cap of captures) {
      const sent = cap.init.body as string;
      expect(sent).not.toContain("continue");
      expect(sent).not.toContain("subset-of");
    }
  });

  test("HTTP main() resolves without throwing on the gated path", async () => {
    muteLog();
    scriptFetch([], [WELCOME_NO_PAGINATION, ...HANDSHAKE_ONLY]);

    await expect(main()).resolves.toBeUndefined();
  });

  test("UDS main_uds() SKIPS the paged query when cursor-pagination is unadvertised", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;
    const lines: string[] = [];
    const run = main_uds(DEFAULT_SOCKET);
    console.log = ((...args: unknown[]) => {
      lines.push(args.map(String).join(" "));
    }) as typeof console.log;
    sock.emit("connect");
    for (const reply of [UDS_WELCOME_NO_PAGINATION, ...HANDSHAKE_ONLY]) {
      await Promise.resolve();
      sock.emit("data", frameOf(reply));
    }
    await run;

    const sent = decodeFrames(sock.written());
    // Five frames sent (hello + four handshake), strictly fewer than the full
    // paginated + watch run.
    expect(sent).toHaveLength(5);
    expect(sent.length).toBeLessThan(UDS_FULL_SENT);
    const output = lines.join("\n");
    expect(output).toContain("server does not advertise cursor pagination; skipping paged query");
    for (const frame of sent) {
      expect(frame).not.toContain("continue");
      expect(frame).not.toContain("subset-of");
    }
  });

  test("UDS main_uds() still closes the socket on the gated path", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;
    const run = main_uds(DEFAULT_SOCKET);
    muteLog();
    sock.emit("connect");
    for (const reply of [UDS_WELCOME_NO_PAGINATION, ...HANDSHAKE_ONLY]) {
      await Promise.resolve();
      sock.emit("data", frameOf(reply));
    }
    await run;

    expect(sock.destroyed).toBe(true);
  });

  test("HTTP capability-present path (default WELCOME) runs the FULL paginated tail", async () => {
    // The complement to the gated path: with the fixed default fixtures (which
    // now advertise :lemma/cursor-pagination) the full nine-call run executes.
    const lines: string[] = [];
    console.log = ((...args: unknown[]) => {
      lines.push(args.map(String).join(" "));
    }) as typeof console.log;
    const captures: Capture[] = [];
    scriptFetch(captures, FULL_SEQUENCE);
    armSSE();

    await main();

    expect(captures).toHaveLength(FULL_SEQUENCE_LEN);
    const output = lines.join("\n");
    expect(output).toContain("paged query (subset-of ? group), limit 2 -> 3 rows over 2 page(s)");
    expect(output).not.toContain("skipping paged query");
  });
});

// ===========================================================================
// (H) SSE reader -- open_sse_stream + read_sse_events over the mocked socket.
//
// The HTTP watch path reads pushed :watch-event envelopes off a raw node:net
// socket carrying a chunked HTTP/1.1 SSE response (Bun's fetch can't stream the
// chunked body, hence the hand-rolled transport). We drive the real functions
// against the self-driving FakeSSESocket: open_sse_stream connects, writes the
// GET, and drains response headers; read_sse_events transfer-decodes the chunked
// body and parses SSE `data:` lines into envelopes. The canned response embeds
// a size-0 header-flush chunk and a `:`-comment keep-alive to prove neither is
// mistaken for EOF or an event. No real socket is ever opened.
// ===========================================================================

describe("SSE reader (open_sse_stream + read_sse_events)", () => {
  test("connects to the host/port parsed from the base, never a real socket", async () => {
    armSSE();

    const stream = await open_sse_stream("http://127.0.0.1:8080", "s-1", 1.0);
    stream.close();

    expect(sseConnectCalls).toBe(1);
    expect(lastHostPort).toEqual({ host: "127.0.0.1", port: 8080 });
    expect(connectCalls).toBe(0); // no UDS path dial
  });

  test("writes a GET /v1/sessions/{id}/events request with the session header", async () => {
    const sse = armSSE();

    const stream = await open_sse_stream("http://127.0.0.1:8080", "s-42", 1.0);
    stream.close();

    const request = sse.written().toString("utf-8");
    expect(request).toContain("GET /v1/sessions/s-42/events HTTP/1.1");
    expect(request).toContain("Accept: text/event-stream");
    expect(request).toContain("X-Lemma-Session: s-42");
  });

  test("yields the parsed :watch-event envelope (event/type/data)", async () => {
    armSSE([WATCH_EVENT]);

    const stream = await open_sse_stream("http://127.0.0.1:8080", "s-1", 1.0);
    const events = await read_sse_events(stream, 1);
    stream.close();

    expect(events).toHaveLength(1);
    const evt = events[0];
    expect(edn.encode((evt as edn.Map).at(new edn.Keyword(":event")))).toBe(
      ":watch-event",
    );
    expect(edn.encode((evt as edn.Map).at(new edn.Keyword(":type")))).toBe(
      ":asserted",
    );
  });

  test("treats the size-0 header-flush chunk as a keep-alive, NOT end-of-stream", async () => {
    // chunkedSSE() always emits a leading `0\r\n\r\n` chunk before the events. If
    // read_sse_events mistook it for EOF it would return zero events; getting the
    // event back proves the size-0 chunk is skipped and reading continues.
    armSSE([WATCH_EVENT]);

    const stream = await open_sse_stream("http://127.0.0.1:8080", "s-1", 1.0);
    const events = await read_sse_events(stream, 1);
    stream.close();

    expect(events).toHaveLength(1);
  });

  test("skips `:`-comment keep-alive lines rather than parsing them as events", async () => {
    // The canned response carries a trailing `: keep-alive` comment chunk. With
    // maxEvents=2 the reader will drain past the one real event and consume the
    // comment chunk; it must NOT surface the comment as a second event, and must
    // terminate (the stream ends) rather than hang.
    armSSE([WATCH_EVENT]);

    // Small read timeout: after the one event and the comment chunk are drained,
    // the next read finds nothing and times out fast, ending the loop.
    const stream = await open_sse_stream("http://127.0.0.1:8080", "s-1", 0.05);
    const events = await read_sse_events(stream, 2);
    stream.close();

    // Exactly one real event; the comment line produced none.
    expect(events).toHaveLength(1);
  });

  test("honors maxEvents: stops at the cap even when more events are available", async () => {
    armSSE([WATCH_EVENT, WATCH_EVENT, WATCH_EVENT]);

    const stream = await open_sse_stream("http://127.0.0.1:8080", "s-1", 1.0);
    const events = await read_sse_events(stream, 2);
    stream.close();

    expect(events).toHaveLength(2);
  });

  test("returns the events gathered so far and terminates on EOF (no hang)", async () => {
    // A response that ends after one event with a terminating size-0 chunk and a
    // socket close: read_sse_events must return that one event and not block.
    const body =
      "HTTP/1.1 200 OK\r\n" +
      "Content-Type: text/event-stream\r\n" +
      "Transfer-Encoding: chunked\r\n\r\n";
    const evtChunk = (() => {
      const s = `data: ${WATCH_EVENT}\n\n`;
      return `${Buffer.byteLength(s, "utf-8").toString(16)}\r\n${s}\r\n`;
    })();
    const sse = new FakeSSESocket(Buffer.from(body + evtChunk, "utf-8"));
    currentSSESocket = sse;

    const stream = await open_sse_stream("http://127.0.0.1:8080", "s-1", 1.0);
    // Ask for two but only one is on the wire; after delivering it, close the
    // socket so the next read sees EOF and the loop ends.
    const reading = read_sse_events(stream, 2);
    await Promise.resolve();
    sse.emit("close");
    const events = await reading;
    stream.close();

    expect(events).toHaveLength(1);
  });

  test("a quiet stream (no events before the per-read timeout) yields an empty list", async () => {
    // The socket connects and returns headers but no body chunks. The bounded
    // per-read timeout must fire so read_sse_events returns [] rather than hang.
    const headersOnly = Buffer.from(
      "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
      "utf-8",
    );
    const sse = new FakeSSESocket(headersOnly);
    currentSSESocket = sse;

    // Tiny timeout (10ms) so the test is fast and deterministic.
    const stream = await open_sse_stream("http://127.0.0.1:8080", "s-1", 0.01);
    const events = await read_sse_events(stream, 1);
    stream.close();

    expect(events).toEqual([]);
  });
});

// ===========================================================================
// (I) UDS watch demux -- uds_await_watch_event picks the :watch-event out of an
// interleaved command-reply frame stream.
//
// Over UDS the watch push has no separate channel: it interleaves with ordinary
// command replies on the one socket. uds_await_watch_event(reader, maxFrames)
// reads frames (via reader.recv()) until it sees a :watch-event, skipping
// command replies, bounded by maxFrames -- returning null on no-event / EOF /
// timeout. The function only ever calls reader.recv(), so we drive it with a
// minimal stub reader over a queue of canned frame bodies (a recv() that
// resolves the next body, or rejects to simulate a timeout/close). No socket.
// ===========================================================================

/**
 * A minimal stand-in for the internal FrameReader: `recv()` resolves the next
 * canned frame body, or rejects once the queue is exhausted (simulating an EOF /
 * socket-timeout, which is exactly how the real FrameReader fails a parked recv
 * after the socket is destroyed). uds_await_watch_event only calls recv(), so
 * this is a faithful seam without needing the unexported class.
 */
function stubReader(frames: string[], rejectMsg = "connection closed"): {
  recv: () => Promise<string>;
} {
  let i = 0;
  return {
    recv: () => {
      if (i < frames.length) {
        const body = frames[i];
        i += 1;
        return Promise.resolve(body);
      }
      return Promise.reject(new Error(rejectMsg));
    },
  };
}

describe("uds_await_watch_event demux", () => {
  test("returns the :watch-event found after skipping command-reply frames", async () => {
    const reader = stubReader([ASSERTED, PROPOSED, WATCH_EVENT, UNWATCH_OK]);

    const evt = await uds_await_watch_event(reader as never, 8);

    expect(evt).not.toBeNull();
    expect(edn.encode((evt as edn.Map).at(new edn.Keyword(":event")))).toBe(
      ":watch-event",
    );
  });

  test("returns the :watch-event when it is the very first frame read", async () => {
    const reader = stubReader([WATCH_EVENT]);

    const evt = await uds_await_watch_event(reader as never, 8);

    expect(edn.encode((evt as edn.Map).at(new edn.Keyword(":type")))).toBe(
      ":asserted",
    );
  });

  test("returns null (does not hang) when no :watch-event arrives within maxFrames", async () => {
    // Only command replies, more than maxFrames of them: the bounded loop must
    // give up and return null rather than draining forever.
    const reader = stubReader([ASSERTED, PROPOSED, ASSERTED, PROPOSED, ASSERTED]);

    const evt = await uds_await_watch_event(reader as never, 3);

    expect(evt).toBeNull();
  });

  test("stops at maxFrames even though a later frame would have been the event", async () => {
    // The :watch-event sits at index 3 but maxFrames is 3, so it is never read.
    const reader = stubReader([ASSERTED, PROPOSED, ASSERTED, WATCH_EVENT]);

    const evt = await uds_await_watch_event(reader as never, 3);

    expect(evt).toBeNull();
  });

  test("returns null on a premature close (recv rejects) before any event", async () => {
    // An empty queue makes the first recv() reject (EOF/timeout). The function
    // must catch that and report null, not propagate the rejection.
    const reader = stubReader([]);

    const evt = await uds_await_watch_event(reader as never, 8);

    expect(evt).toBeNull();
  });

  test("returns null when the stream closes after some command replies but no event", async () => {
    // A few command replies then EOF: recv resolves the replies, then rejects.
    const reader = stubReader([ASSERTED, PROPOSED]);

    const evt = await uds_await_watch_event(reader as never, 8);

    expect(evt).toBeNull();
  });
});

// ===========================================================================
// (J) Watch capability gating -- the watch demo runs IFF :lemma/watch is
// advertised. We drive main()/main_uds with a welcome that advertises
// cursor-pagination but OMITS :lemma/watch and assert the watch block is
// skipped: the skip note is printed, no (watch-pattern ...) is ever sent, and
// over HTTP the SSE socket is NEVER opened (sseConnectCalls stays 0).
// ===========================================================================

// A welcome advertising cursor-pagination but NOT :lemma/watch: the paged tail
// runs, the watch demo must be gated off.
const WELCOME_NO_WATCH =
  '{:event :welcome :version 1 :session #session "s-1" :world #world "default"' +
  ' :capabilities #{:lemma/cursor-pagination :lemma/v1}' +
  ' :limits {:max-message-bytes 1048576}}';
const UDS_WELCOME_NO_WATCH = WELCOME_NO_WATCH;

// The handshake + paged tail but NO watch tail: nine calls/frames.
const NO_WATCH_SEQUENCE = [
  WORLD_SELECTED, PROPOSED, ASSERTED, RESULT, ...PAGED_TAIL,
];

describe("capability gating: watch absent", () => {
  test("HTTP main() SKIPS the watch demo and NEVER opens the SSE socket", async () => {
    const lines: string[] = [];
    console.log = ((...args: unknown[]) => {
      lines.push(args.map(String).join(" "));
    }) as typeof console.log;
    const captures: Capture[] = [];
    scriptFetch(captures, [WELCOME_NO_WATCH, ...NO_WATCH_SEQUENCE]);
    // Deliberately do NOT arm the SSE seam: if the watch demo wrongly ran, the
    // SSE connect attempt would still be counted (and we assert it is zero).

    await main();

    // Nine fetch calls (handshake + paged tail); the four watch-tail calls gone.
    expect(captures).toHaveLength(9);
    expect(captures.length).toBeLessThan(FULL_SEQUENCE_LEN);
    // The SSE socket was never opened -- the load-bearing proof the watch path
    // was gated, not merely quiet.
    expect(sseConnectCalls).toBe(0);
    const output = lines.join("\n");
    expect(output).toContain("server does not advertise watch; skipping watch demo");
    // No watch-pattern / unwatch frame ever went out.
    for (const cap of captures) {
      const sent = cap.init.body as string;
      expect(sent).not.toContain("watch-pattern");
      expect(sent).not.toContain("unwatch");
    }
  });

  test("HTTP main() resolves without throwing on the watch-gated path", async () => {
    muteLog();
    scriptFetch([], [WELCOME_NO_WATCH, ...NO_WATCH_SEQUENCE]);

    await expect(main()).resolves.toBeUndefined();
    expect(sseConnectCalls).toBe(0);
  });

  test("UDS main_uds() SKIPS the watch demo when :lemma/watch is unadvertised", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;
    const lines: string[] = [];
    const run = main_uds(DEFAULT_SOCKET);
    console.log = ((...args: unknown[]) => {
      lines.push(args.map(String).join(" "));
    }) as typeof console.log;
    sock.emit("connect");
    for (const reply of [UDS_WELCOME_NO_WATCH, ...NO_WATCH_SEQUENCE]) {
      await Promise.resolve();
      sock.emit("data", frameOf(reply));
    }
    await run;

    const sent = decodeFrames(sock.written());
    // Nine frames (hello + handshake + paged tail), no watch tail.
    expect(sent).toHaveLength(9);
    expect(sent.length).toBeLessThan(UDS_FULL_SENT);
    const output = lines.join("\n");
    expect(output).toContain("server does not advertise watch; skipping watch demo");
    for (const frame of sent) {
      expect(frame).not.toContain("watch-pattern");
      expect(frame).not.toContain("unwatch");
    }
    // No host/port dial either -- UDS never touches the SSE transport.
    expect(sseConnectCalls).toBe(0);
  });

  test("UDS main_uds() still closes the socket on the watch-gated path", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;
    const run = main_uds(DEFAULT_SOCKET);
    muteLog();
    sock.emit("connect");
    for (const reply of [UDS_WELCOME_NO_WATCH, ...NO_WATCH_SEQUENCE]) {
      await Promise.resolve();
      sock.emit("data", frameOf(reply));
    }
    await run;

    expect(sock.destroyed).toBe(true);
  });
});
