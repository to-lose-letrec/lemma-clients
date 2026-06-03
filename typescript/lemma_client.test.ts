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

  write(chunk: Buffer | string): boolean {
    this.writes.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    return true;
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

// The fake `createConnection` returns. Set per test before invoking the client.
let currentSocket: FakeSocket | null = null;
// The path the client most recently asked `createConnection` to dial.
let lastPath: string | null = null;
// How many times `createConnection` was called -- proves a UDS connect attempt.
let connectCalls = 0;

mock.module("node:net", () => ({
  createConnection: (opts: { path: string }) => {
    connectCalls += 1;
    lastPath = opts.path;
    const sock = currentSocket ?? new FakeSocket();
    currentSocket = sock;
    return sock;
  },
}));

import {
  post_edn,
  main,
  main_uds,
  _dispatch,
  query_all,
  uds_send_frame,
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
  lastPath = null;
  connectCalls = 0;
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
  '{:event :welcome :version 1 :session #session "s-1" :world #world "default"}';
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
  WELCOME, WORLD_SELECTED, PROPOSED, ASSERTED, RESULT, ...PAGED_TAIL,
];
// The full round-trip now makes nine calls: the original five plus the four
// paginated-tail calls (batch propose, assert, page one, continue page two).
const FULL_SEQUENCE_LEN = FULL_SEQUENCE.length;

describe("main() handshake", () => {
  test("completes the full round-trip (handshake + paged query) without throwing", async () => {
    const captures: Capture[] = [];
    scriptFetch(captures, FULL_SEQUENCE);

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
  '{:event :welcome :version 1 :session #session "s-1" :world #world "default"}';
const UDS_FULL = [
  UDS_WELCOME, WORLD_SELECTED, PROPOSED, ASSERTED, RESULT, ...PAGED_TAIL,
];
const UDS_FULL_LEN = UDS_FULL.length;

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
    // (the handshake's four replies plus the paginated tail's four).
    for (const reply of [WORLD_SELECTED, PROPOSED, ASSERTED, RESULT, ...PAGED_TAIL]) {
      await Promise.resolve();
      sock.emit("data", frameOf(reply));
    }

    await expect(run).resolves.toBeUndefined();
    // All round-trip frames sent and decodable despite the dribbled welcome.
    expect(decodeFrames(sock.written())).toHaveLength(UDS_FULL_LEN);
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
    // Drive the paginated tail too so the round-trip runs to completion.
    for (const reply of PAGED_TAIL) {
      await Promise.resolve();
      sock.emit("data", frameOf(reply));
    }

    await expect(run).resolves.toBeUndefined();
    expect(decodeFrames(sock.written())).toHaveLength(UDS_FULL_LEN);
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
  test("completes the full round-trip (handshake + paged query) without throwing", async () => {
    const sock = new FakeSocket();
    currentSocket = sock;

    await expect(driveUds(sock, UDS_FULL)).resolves.toBeUndefined();
    expect(decodeFrames(sock.written())).toHaveLength(UDS_FULL_LEN);
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

    await _dispatch(["http://example.test:9999"]);

    expect(connectCalls).toBe(0); // no UDS dial
    // First HTTP call hit the URL arg's base.
    expect(captures[0].url.startsWith("http://example.test:9999")).toBe(true);
  });

  test("no args route to HTTP main against DEFAULT_BASE and open NO UDS connection", async () => {
    const captures: Capture[] = [];
    scriptFetch(captures, FULL_SEQUENCE);

    await _dispatch([]);

    expect(connectCalls).toBe(0);
    expect(captures[0].url.startsWith(DEFAULT_BASE)).toBe(true);
  });
});
