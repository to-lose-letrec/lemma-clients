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
import * as edn from "jsedn";

import {
  post_edn,
  main,
  DEFAULT_BASE,
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

const FULL_SEQUENCE = [WELCOME, WORLD_SELECTED, PROPOSED, ASSERTED, RESULT];

describe("main() handshake", () => {
  test("completes the full five-step sequence without throwing", async () => {
    const captures: Capture[] = [];
    scriptFetch(captures, FULL_SEQUENCE);

    await expect(main()).resolves.toBeUndefined();
    expect(captures).toHaveLength(5);
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
