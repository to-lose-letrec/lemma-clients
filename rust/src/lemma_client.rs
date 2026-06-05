//! A single-file Rust client for the Lemma wire protocol.
//!
//! This file is a *recipe*, not a library: read it end to end. The first thing
//! any Lemma client needs is a way to turn Rust values into the EDN text the
//! server speaks, and to turn the server's EDN responses back into Rust values.
//! Rather than hand-roll that codec, this client leans on `edn-format` (the one
//! third-party dependency); everything else is `std`. On top of the codec sit a
//! hand-rolled HTTP transport and a runnable `main_run` that walks the
//! propose/assert/query round-trip.
//!
//! # EDN in a nutshell
//!
//! EDN (Extensible Data Notation) is Clojure's data syntax. Lemma uses a small,
//! well-defined subset of it. The pieces we care about and their `edn-format`
//! mappings (everything is one `edn_format::Value` enum):
//!
//! ```text
//! nil true false              -- Value::Nil / Value::Boolean
//! 42  -3  3.14                 -- Value::Integer / Value::Float
//! "a string\n"                -- Value::String
//! :event  :lemma/watch        -- Value::Keyword (Keyword::from_name)
//! equivalent  member-of  ?o   -- Value::Symbol  (?-vars are symbols)
//! ( a b c )                   -- Value::List   — the top-level verb form
//! [ a b c ]                   -- Value::Vector — the common inner case
//! { k v, k v }                -- Value::Map    (a BTreeMap<Value, Value>)
//! #{ a b c }                  -- Value::Set
//! #tag payload                -- Value::TaggedElement(Symbol, Box<Value>)
//! ```
//!
//! # Lists versus vectors — the one design decision
//!
//! EDN distinguishes lists `( … )` from vectors `[ … ]`, and Lemma relies on the
//! distinction (grammar §3): a *list* appears ONLY as the top-level verb form —
//! `(propose …)`, `(query …)`, `(hello)`. Everywhere inside the arguments,
//! collections are *vectors* (`:find [?x]`, `:where [[…]]`), maps, or sets —
//! never lists.
//!
//! Here is where Rust's `edn-format` is the simplest of the siblings: it has a
//! distinct `Value::List` variant that emits a BARE top-level list natively.
//! Python had to lean on the tuple/list split, and Go had to carry a custom
//! `verb` type because go-edn cannot emit a bare top-level list at all. In Rust
//! we just build `Value::List(vec![…])` and `emit_str` renders
//! `(use-world #world "default")` directly — so the [`verb`] helper below is a
//! one-line constructor, not a workaround. Argument collections are
//! `Value::Vector(…)`, exactly the grammar's split.
//!
//! # Tagged literals
//!
//! The ten core Lemma tags are `#entity #world #proposal #tx #ref #cursor #watch
//! #session #fact #violation` (grammar §5). Again Rust is the simplest sibling:
//! `Value::TaggedElement(Symbol, Box<Value>)` handles ALL ten in BOTH directions
//! with NO registration. `parse_str` reconstructs a `TaggedElement` from wire
//! text and `emit_str` re-emits the exact `#tag payload` text, so round-trip
//! equality holds for free — where Python and Go each had to declare custom
//! `_Handle` / `Handle` types and register readers. A tag we never name (e.g.
//! `#inst`) still parses, so an unexpected tag never breaks a response.
//!
//! # Why the HTTP transport is hand-rolled
//!
//! Rust's std has no HTTP client, and the demo budget for this sibling is
//! `std` + one EDN reader — pulling in `reqwest`/`hyper` (and its async runtime)
//! to POST a few hundred bytes would dwarf the recipe it serves. It would also
//! hide the very thing the repo exists to show: the wire. Every sibling already
//! hand-rolls the SSE stream by hand for the same reason; here we extend that
//! spirit to the request path too, speaking HTTP/1.1 over a raw
//! `std::net::TcpStream`. It is a few dozen readable lines and the whole
//! conversation stays visible.
//!
//! Loading this file performs no network I/O — only `main` / `main_run` touch
//! the network.

use std::io::{self, Read, Write};
use std::net::TcpStream;
use std::os::unix::net::UnixStream;

use edn_format::{Keyword, Symbol, Value, emit_str, parse_str};

// ---------------------------------------------------------------------------
// Constructor helpers
//
// Thin wrappers over Value so the round-trip code below reads as prose rather
// than as a wall of enum constructions. They mirror the grammar's payload
// shapes. Helpers, not abstractions: no traits, no builder types — each returns
// a plain Value.
// ---------------------------------------------------------------------------

/// Build an `#entity "<name>"` handle (grammar §5.3).
fn entity(name: &str) -> Value {
    Value::TaggedElement(Symbol::from_name("entity"), Box::new(Value::from(name)))
}

/// Build a `#world "<name>"` handle (grammar §5).
fn world(name: &str) -> Value {
    Value::TaggedElement(Symbol::from_name("world"), Box::new(Value::from(name)))
}

/// Build a `#fact {…}` binary fact: `(predicate subject object)`.
///
/// `predicate` is a bare symbol name; `subject` / `object` are typically
/// `#entity` handles. The keys are the grammar's reserved fact keys.
fn fact_value(predicate: &str, subject: Value, object: Value) -> Value {
    let mut map = std::collections::BTreeMap::new();
    map.insert(
        Value::Keyword(Keyword::from_name("predicate")),
        Value::Symbol(Symbol::from_name(predicate)),
    );
    map.insert(Value::Keyword(Keyword::from_name("subject")), subject);
    map.insert(Value::Keyword(Keyword::from_name("object")), object);
    Value::TaggedElement(Symbol::from_name("fact"), Box::new(Value::Map(map)))
}

/// Build a verb form: a bare top-level EDN list `( verb arg … )`.
///
/// This is the list-vs-vector vehicle (see the module doc). Unlike the sibling
/// clients, no custom type is needed — `Value::List` emits a bare top-level list
/// natively, so the verb head and its arguments just go into one `Vec<Value>`.
fn verb(elements: Vec<Value>) -> Value {
    Value::List(elements)
}

// ---------------------------------------------------------------------------
// Envelope inspection
//
// Every Lemma reply is a map keyed by :event. Two values mean refusal: :error
// (malformed / illegal) and :rejected (well-formed but disallowed, e.g. a
// consistency violation). Parsed maps are Value::Map(BTreeMap<Value, Value>)
// with Keyword keys, so the helpers below look keys up by Keyword.
// ---------------------------------------------------------------------------

/// Look a keyword field up in a parsed reply map.
///
/// Returns `None` if `body` is not a map or the key is absent. Centralising the
/// `Value::Map` + `Keyword` dance keeps the round-trip code readable.
fn get<'a>(body: &'a Value, key: &str) -> Option<&'a Value> {
    match body {
        Value::Map(map) => map.get(&Value::Keyword(Keyword::from_name(key))),
        _ => None,
    }
}

/// Report whether `body` is an `:error` or `:rejected` envelope.
fn is_failure(body: &Value) -> bool {
    match get(body, "event") {
        Some(Value::Keyword(kw)) => {
            *kw == Keyword::from_name("error") || *kw == Keyword::from_name("rejected")
        }
        _ => false,
    }
}

/// Format the salient parts of an error/rejection envelope for printing.
///
/// Pulls whichever of `:reason` / `:message` / `:violations` the server
/// included — the fields that explain *why* a call was refused — and renders
/// each through `emit_str` so keywords, strings, and `#violation` handles print
/// as they appear on the wire. The parts are `"; "`-joined; an envelope with
/// none of the three falls back to `"(no detail provided)"`.
fn describe_failure(body: &Value) -> String {
    let parts: Vec<String> = ["reason", "message", "violations"]
        .iter()
        .filter_map(|key| get(body, key).map(|val| format!(":{} {}", key, emit_str(val))))
        .collect();
    if parts.is_empty() {
        "(no detail provided)".to_string()
    } else {
        parts.join("; ")
    }
}

/// Render a parsed value as wire EDN text, for status lines.
///
/// A thin alias for `emit_str` that also maps an absent field (`None`) to the
/// literal `nil`, so a status line never prints an empty string where the wire
/// would show a value.
fn render(value: Option<&Value>) -> String {
    match value {
        Some(v) => emit_str(v),
        None => "nil".to_string(),
    }
}

// ---------------------------------------------------------------------------
// Cursor pagination:  drain a (query …) across (continue #cursor …) pages
//
// A (query …) with a :limit returns a FULL first page — `:rows` plus
// `:done? false` and a `#cursor` handle. Each `(continue #cursor)` carries the
// next page's `:rows` / `:cursor` / `:done?`, until `:done?` is true (SPEC §8).
// `query_all` walks that chain into one flat row set. It is transport-agnostic:
// the caller hands in a `send` closure (HTTP or UDS) that turns one verb form
// into one parsed reply body, and query_all threads the cursor through it.
// ---------------------------------------------------------------------------

/// The parsed `:rows` of a result envelope, as a borrowed slice.
///
/// `:rows` is a `Value::Vector` of row vectors; anything else (absent, or a
/// non-vector) reads as the empty slice so a malformed page never panics.
fn rows_of(body: &Value) -> &[Value] {
    match get(body, "rows") {
        Some(Value::Vector(rows)) => rows,
        _ => &[],
    }
}

/// Whether a result envelope's `:done?` flag is the literal `true`.
///
/// Treats anything other than `:done? true` (including an absent flag) as "not
/// done", so a page that omits the flag keeps the drain loop honest rather than
/// ending it early.
fn is_done(body: &Value) -> bool {
    get(body, "done?") == Some(&Value::Boolean(true))
}

/// Run `query_form` and drain every page via `(continue #cursor …)`.
///
/// `send` is the per-transport `form -> body` closure (HTTP `named` or the UDS
/// `call`, each adapted to take the form by value and yield the parsed reply).
/// Returns `(rows, pages, failure)`:
///
///   * `rows`    — every row across all pages, flattened in page order.
///   * `pages`   — how many result envelopes were drained (1 for a single page).
///   * `failure` — `None` on success, or the offending `:error` / `:rejected`
///     body when the server refuses a call mid-drain.
///
/// A failure envelope on the FIRST reply (the query itself) yields
/// `Ok((vec![], 0, Some(body)))`. A failure on a later `(continue …)` — e.g. an
/// expired cursor coming back `:error :unknown-handle` (server idle TTL ~300s,
/// SPEC §8) — yields `Ok((rows_so_far, pages, Some(body)))`; a real client would
/// re-issue the original query for a fresh page, but this demo surfaces it. A
/// transport-level error from `send` propagates as `Err`.
///
/// The `:cursor` is present EXACTLY when `:done?` is not true, so we read it only
/// inside the loop — and feed the parsed `#cursor` `TaggedElement` back verbatim,
/// never reconstructing it from text.
fn query_all<E>(
    send: &mut impl FnMut(Value) -> Result<Value, E>,
    query_form: Value,
) -> Result<(Vec<Value>, usize, Option<Value>), E> {
    let mut body = send(query_form)?;
    if is_failure(&body) {
        return Ok((Vec::new(), 0, Some(body)));
    }

    let mut rows: Vec<Value> = rows_of(&body).to_vec();
    let mut pages = 1usize;
    while !is_done(&body) {
        // :cursor is present only while :done? is falsey; the server omits it on
        // an already-done page, so reading it here (not before the loop) is safe.
        // A page that is not done yet still lacks a cursor would leave us nothing
        // to continue with, so we stop with what we have rather than spin.
        let cursor = match get(&body, "cursor") {
            Some(c) => c.clone(),
            None => break,
        };
        body = send(verb(vec![sym("continue"), cursor]))?;
        if is_failure(&body) {
            return Ok((rows, pages, Some(body)));
        }
        rows.extend_from_slice(rows_of(&body));
        pages += 1;
    }
    Ok((rows, pages, None))
}

// ---------------------------------------------------------------------------
// HTTP transport:  EDN form  ->  hand-rolled POST  ->  parsed EDN response
//
// With the codec in hand, talking to a Lemma server is just "encode, POST,
// decode". Rust's std has no HTTP client, so we speak HTTP/1.1 by hand over a
// std::net::TcpStream (see the module doc for why). The session protocol
// (SPEC §3) is:
//
//   * The first call is anonymous: POST /v1/messages with (hello). The :welcome
//     response carries the new session id in the X-Lemma-Session response header.
//   * Subsequent calls reuse that id, on the named endpoint
//     POST /v1/sessions/{id}/messages, echoing it in the x-lemma-session header.
//
// This helper handles one round-trip; the caller threads the returned session
// id into the next call.
// ---------------------------------------------------------------------------

/// Where a locally booted Dianoia HTTP listener lives by default (see the
/// protocol examples). Override per call via the `base` argument.
const DEFAULT_BASE: &str = "http://127.0.0.1:8080";

/// POST an EDN `form` to `base + path` and return `(parsed body, session id)`.
///
/// `form` is any `Value` — typically a verb form such as
/// `verb(vec![Value::Symbol(Symbol::from_name("hello"))])`. It is encoded to EDN
/// text via `emit_str` and sent as `application/edn` UTF-8 bytes; the response
/// body is parsed back into a `Value` with `parse_str`. When `session` is
/// `Some`, it is echoed in the `x-lemma-session` request header so the server
/// attaches the call to an existing session.
///
/// `base` is parsed as `http://host:port` by hand (no `url` crate); the scheme
/// and any path are ignored — only host and port are used to dial.
///
/// # Error handling
///
/// An HTTP error status (4xx/5xx) still carries a valid Lemma EDN *error
/// envelope* in its body, so we parse and return that as the body rather than
/// treating it as a transport failure — the caller inspects `:event` to tell a
/// welcome from an error. A connection-level failure (server down, refused, or a
/// response we cannot parse) is the one transport error: an `Err` that names the
/// `base` so the failure is actionable rather than a bare errno.
fn post_edn(
    path: &str,
    form: &Value,
    session: Option<&str>,
    base: &str,
) -> Result<(Value, Option<String>), String> {
    let (host, port) = parse_host_port(base);
    let body = emit_str(form);

    // Build the request by hand. Content-Length is the body's BYTE length (EDN
    // text is UTF-8); Connection: close lets us read the response to EOF safely.
    let mut request = String::new();
    request.push_str(&format!("POST {path} HTTP/1.1\r\n"));
    request.push_str(&format!("Host: {host}:{port}\r\n"));
    request.push_str("Content-Type: application/edn\r\n");
    request.push_str(&format!("Content-Length: {}\r\n", body.len()));
    if let Some(sid) = session {
        request.push_str(&format!("x-lemma-session: {sid}\r\n"));
    }
    request.push_str("Connection: close\r\n\r\n");
    request.push_str(&body);

    // Connect, write the request, read the whole response. A failure at any of
    // these steps means we never completed a round-trip with a Lemma server, so
    // it is reported as a transport error naming the base.
    let mut stream = TcpStream::connect((host.as_str(), port)).map_err(|err| {
        format!("could not reach the Lemma server at {base:?} ({err}); is the server running?")
    })?;
    stream.write_all(request.as_bytes()).map_err(|err| {
        format!("could not send to the Lemma server at {base:?} ({err}); is the server running?")
    })?;
    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).map_err(|err| {
        format!("could not read from the Lemma server at {base:?} ({err}); is the server running?")
    })?;

    // Split the HTTP response into headers and body at the blank line, capturing
    // the session header (case-insensitively) on the way. A 4xx/5xx body is
    // still a parseable Lemma error envelope, so we parse it the same way either
    // way — the caller reads :event to tell success from refusal.
    let (session_id, body_bytes) = split_http_response(&raw, base)?;
    let body_text = String::from_utf8_lossy(body_bytes);
    let parsed = parse_str(&body_text)
        .map_err(|err| format!("parsing EDN response from {base:?} ({err}): {body_text:?}"))?;
    Ok((parsed, session_id))
}

/// Parse `host` and `port` out of a `base` like `http://127.0.0.1:8080`.
///
/// String parsing is sufficient (no `url` crate): we drop any `scheme://`
/// prefix and any trailing path, then split the authority on `:`. A missing
/// port defaults to 80, matching the HTTP default the siblings use.
fn parse_host_port(base: &str) -> (String, u16) {
    let without_scheme = base.split("://").last().unwrap_or(base);
    // Authority ends at the first '/', if any (the path is irrelevant to dialling).
    let authority = without_scheme.split('/').next().unwrap_or(without_scheme);
    match authority.rsplit_once(':') {
        Some((host, port)) => (host.to_string(), port.parse().unwrap_or(80)),
        None => (authority.to_string(), 80),
    }
}

/// Split a raw HTTP response into `(session id, body bytes)`.
///
/// Reads the status line + headers up to the blank line (`\r\n\r\n`), capturing
/// the `X-Lemma-Session` header CASE-INSENSITIVELY (servers are free to vary the
/// casing), and returns everything after the blank line as the body. Because the
/// request set `Connection: close`, reading to EOF already gave us the whole
/// body, so we do not need to honour Content-Length on the read side.
fn split_http_response<'a>(
    raw: &'a [u8],
    base: &str,
) -> Result<(Option<String>, &'a [u8]), String> {
    let split = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or_else(|| format!("malformed HTTP response from {base:?}: no header terminator"))?;
    let header_text = String::from_utf8_lossy(&raw[..split]);
    let body = &raw[split + 4..];

    let mut session_id = None;
    for line in header_text.lines().skip(1) {
        if let Some((name, value)) = line.split_once(':')
            && name.trim().eq_ignore_ascii_case("x-lemma-session")
        {
            session_id = Some(value.trim().to_string());
        }
    }
    Ok((session_id, body))
}

// ---------------------------------------------------------------------------
// Runnable recipe:  the full Lemma round-trip
//
// A flat, linear retelling of the protocol's hello -> use-world -> propose ->
// assert -> query sequence. Each step prints one human-readable line so a reader
// can follow the wire conversation by running the file. After every reply we
// check :event; an :error / :rejected is printed (via describe_failure) and the
// sequence returns cleanly rather than panicking. NO unwrap()/expect() on
// server-controlled data — every Option/Result is handled explicitly.
// ---------------------------------------------------------------------------

/// Convenience: build a `Value::Symbol` from a bare name (verb heads, `?vars`).
fn sym(name: &str) -> Value {
    Value::Symbol(Symbol::from_name(name))
}

/// Walk the propose/assert/query round-trip against a Lemma server over HTTP.
/// `base` is the server's base URL (e.g. `http://127.0.0.1:8080`).
fn main_run(base: &str) {
    // 1. Anonymous hello. The welcome reply carries the new session id in the
    //    X-Lemma-Session response header, which post_edn surfaces for us.
    let (body, sid) = match post_edn("/v1/messages", &verb(vec![sym("hello")]), None, base) {
        Ok(pair) => pair,
        // Connection-level failure: post_edn already named the base URL. Print
        // the actionable line and stop — there is nothing more to attempt.
        Err(err) => {
            println!("{err}");
            return;
        }
    };
    if get(&body, "event") != Some(&Value::Keyword(Keyword::from_name("welcome"))) {
        println!(
            "hello: expected :welcome, got {} -- {}",
            render(get(&body, "event")),
            describe_failure(&body)
        );
        return;
    }
    let session = match sid {
        Some(s) => s,
        // A welcome with no session header leaves us nothing to attach later
        // calls to; report it and stop rather than guessing a session id.
        None => {
            println!("hello -> :welcome but no X-Lemma-Session header; cannot continue");
            return;
        }
    };
    println!(
        "hello -> :welcome  version={}  session={}  world={}",
        render(get(&body, "version")),
        session,
        render(get(&body, "world"))
    );

    // 2. Every later call rides the same session, on the named endpoint, with
    //    the session id echoed in the request header. The closure threads `base`
    //    and `session` so the steps below read as a plain sequence of verbs.
    let named = |form: &Value| -> Result<Value, String> {
        let path = format!("/v1/sessions/{session}/messages");
        post_edn(&path, form, Some(&session), base).map(|(b, _)| b)
    };

    // 3. Enter the world. (use-world #world "default")
    let body = match named(&verb(vec![sym("use-world"), world("default")])) {
        Ok(b) => b,
        Err(err) => {
            println!("{err}");
            return;
        }
    };
    if is_failure(&body) {
        println!("use-world refused: {}", describe_failure(&body));
        return;
    }
    println!(
        "use-world \"default\" -> {}  world={}",
        render(get(&body, "event")),
        render(get(&body, "world"))
    );

    // 4. Propose a fact: morningstar is equivalent to venus. The reply hands
    //    back a #proposal handle we feed straight into the assert.
    let f = fact_value("equivalent", entity("morningstar"), entity("venus"));
    let body = match named(&verb(vec![sym("propose"), f])) {
        Ok(b) => b,
        Err(err) => {
            println!("{err}");
            return;
        }
    };
    if is_failure(&body) {
        println!("propose refused: {}", describe_failure(&body));
        return;
    }
    // Clone the proposal handle out of the borrowed body so we can reuse `body`
    // (and feed the handle into the assert) without holding the borrow.
    let proposal = match get(&body, "proposal") {
        Some(p) => p.clone(),
        None => {
            println!(
                "propose -> {} but no :proposal handle",
                render(get(&body, "event"))
            );
            return;
        }
    };
    println!(
        "propose (equivalent morningstar venus) -> {}  proposal={}",
        render(get(&body, "event")),
        emit_str(&proposal)
    );

    // 5. Assert the proposed fact into the world.
    let body = match named(&verb(vec![sym("assert"), proposal])) {
        Ok(b) => b,
        Err(err) => {
            println!("{err}");
            return;
        }
    };
    if is_failure(&body) {
        println!("assert refused: {}", describe_failure(&body));
        return;
    }
    println!("assert proposal -> {}", render(get(&body, "event")));

    // 6. Query it back. Note :find / :where are VECTORS, and the where-clause is
    //    a vector of vectors; only the verb head is a list, and the query
    //    variable ?o stays a Symbol.
    let mut query_map = std::collections::BTreeMap::new();
    query_map.insert(
        Value::Keyword(Keyword::from_name("find")),
        Value::Vector(vec![sym("?o")]),
    );
    query_map.insert(
        Value::Keyword(Keyword::from_name("where")),
        Value::Vector(vec![Value::Vector(vec![
            sym("equivalent"),
            entity("morningstar"),
            sym("?o"),
        ])]),
    );
    let body = match named(&verb(vec![sym("query"), Value::Map(query_map)])) {
        Ok(b) => b,
        Err(err) => {
            println!("{err}");
            return;
        }
    };
    if is_failure(&body) {
        println!("query refused: {}", describe_failure(&body));
        return;
    }
    println!(
        "query (equivalent morningstar ?o) -> rows={}  done?={}",
        render(get(&body, "rows")),
        render(get(&body, "done?"))
    );

    // 7. Paginated query: seed three subset-of facts in one batched propose,
    //    assert, then drain a :limit 2 query via query_all / (continue #cursor …).
    // Seed three subset-of facts in ONE batched propose, then assert the
    // batch, so the paginated query below spans more than a single page.
    // subset-of is a pure-EDB (stored-fact) predicate: a query over it has
    // stable ordering and so can be paginated. A rule-headed predicate like
    // member-of cannot be the sole outer :where pattern — the server rejects
    // that :bad-args :unsupported-rule-call-ordering.
    let f1 = fact_value("subset-of", entity("sub-a"), entity("group"));
    let f2 = fact_value("subset-of", entity("sub-b"), entity("group"));
    let f3 = fact_value("subset-of", entity("sub-c"), entity("group"));
    let propose_form = verb(vec![sym("propose"), f1, f2, f3]);
    let body = match named(&propose_form) {
        Ok(b) => b,
        Err(err) => {
            println!("{err}");
            return;
        }
    };
    if is_failure(&body) {
        println!(
            "propose (3x subset-of) refused: {}",
            describe_failure(&body)
        );
        return;
    }
    let proposal = match get(&body, "proposal") {
        Some(p) => p.clone(),
        None => {
            println!(
                "propose (3x subset-of) -> {} but no :proposal handle",
                render(get(&body, "event"))
            );
            return;
        }
    };
    println!(
        "propose (3x subset-of ? group) -> {}  proposal={}",
        render(get(&body, "event")),
        emit_str(&proposal)
    );
    let body = match named(&verb(vec![sym("assert"), proposal])) {
        Ok(b) => b,
        Err(err) => {
            println!("{err}");
            return;
        }
    };
    if is_failure(&body) {
        println!("assert (3x subset-of) refused: {}", describe_failure(&body));
        return;
    }
    println!("assert proposal -> {}", render(get(&body, "event")));

    // 8. Paginated query: :limit 2 over 3 matching rows yields two pages
    //    (2 + 1). query_all drains them via (continue #cursor …). named takes
    //    a &Value and returns just the body (the session id rides the header,
    //    not the return), so we adapt it to the form -> body closure
    //    query_all wants.
    let mut paged_query = std::collections::BTreeMap::new();
    paged_query.insert(
        Value::Keyword(Keyword::from_name("find")),
        Value::Vector(vec![sym("?x")]),
    );
    paged_query.insert(
        Value::Keyword(Keyword::from_name("where")),
        Value::Vector(vec![Value::Vector(vec![
            sym("subset-of"),
            sym("?x"),
            entity("group"),
        ])]),
    );
    paged_query.insert(
        Value::Keyword(Keyword::from_name("limit")),
        Value::Integer(2),
    );
    let qform = verb(vec![sym("query"), Value::Map(paged_query)]);
    let mut send = |form: Value| named(&form);
    match query_all(&mut send, qform) {
        Ok((rows, pages, failure)) => {
            if let Some(failure) = failure {
                println!("paged query refused: {}", describe_failure(&failure));
                return;
            }
            println!(
                "paged query (subset-of ? group), limit 2 -> {} rows over {} page(s): {}",
                rows.len(),
                pages,
                emit_str(&Value::Vector(rows))
            );
        }
        Err(err) => {
            println!("{err}");
        }
    }
}

// ---------------------------------------------------------------------------
// UDS transport:  EDN form  ->  length-prefixed frame  ->  parsed EDN response
//
// A second transport that speaks the same EDN codec over a Unix domain socket
// instead of HTTP. It sits alongside post_edn rather than replacing it — same
// "encode, send, decode" shape, different plumbing. Two things differ from the
// HTTP path:
//
//   * Framing. There is no HTTP envelope, so each message is delimited
//     explicitly: a 4-byte big-endian UNSIGNED length prefix followed by that
//     many UTF-8 bytes of EDN. This matches Dianoia's transport/uds.clj
//     write-frame / read-frame exactly (DataOutputStream.writeInt is a 4-byte
//     big-endian int), and the prefix is built/read with u32::to_be_bytes /
//     u32::from_be_bytes — no struct/binary helper, just std.
//   * Session binding. Over HTTP the client threads the session id back into
//     each request header. Over UDS the server binds the session to the
//     *connection*: it captures the id from the welcome envelope and attaches
//     it to the socket (see uds.clj handle-frame / build-ctx). So the client
//     must NOT echo the session id into later frames — it just keeps sending on
//     the same socket, and the server already knows who it is. We read the id
//     for display straight out of the welcome BODY (:session), never a header.
//
// Pure std: std::os::unix::net::UnixStream for the connection, the byte-array
// prefix for framing; the EDN codec is still edn-format.
// ---------------------------------------------------------------------------

/// Where a locally booted Dianoia UDS listener binds by default (see uds.clj
/// start! :socket-path). Override per call via the `socket_path` argument.
const DEFAULT_SOCKET: &str = "/tmp/dianoia.sock";

/// Frame `edn_str` and write it: 4-byte big-endian length prefix, then the body.
///
/// The body is the UTF-8 bytes of `edn_str` (Rust `&str` is already UTF-8); the
/// prefix is its BYTE length as a big-endian `u32` (`u32::to_be_bytes`).
/// `write_all` keeps writing until every byte is on the wire, so one call per
/// chunk suffices. Mirrors uds.clj write-frame.
fn uds_send_frame(stream: &mut UnixStream, edn_str: &str) -> io::Result<()> {
    let body = edn_str.as_bytes();
    let prefix = (body.len() as u32).to_be_bytes();
    stream.write_all(&prefix)?;
    stream.write_all(body)?;
    Ok(())
}

/// Read one length-prefixed frame and return its body as a `String`.
///
/// The inverse of [`uds_send_frame`]: `read_exact` the 4-byte big-endian length
/// (`u32::from_be_bytes`), `read_exact` that many body bytes, then decode UTF-8.
/// `read_exact` is the loop-until-satisfied read — a single `read` may return
/// fewer bytes than asked, and a peer that closes mid-frame surfaces as an
/// `UnexpectedEof`. We rewrite that into an error naming how many bytes were
/// expected (prefix vs. body), so a truncated frame is actionable — mirroring
/// python's `_recv_exactly` message shape. Mirrors uds.clj read-frame.
fn uds_recv_frame(stream: &mut UnixStream) -> io::Result<String> {
    let mut prefix = [0u8; 4];
    stream.read_exact(&mut prefix).map_err(|err| {
        io::Error::new(
            err.kind(),
            format!("reading frame length prefix (4 bytes expected): {err}"),
        )
    })?;
    let length = u32::from_be_bytes(prefix) as usize;
    let mut body = vec![0u8; length];
    stream.read_exact(&mut body).map_err(|err| {
        io::Error::new(
            err.kind(),
            format!("reading frame body ({length} bytes expected): {err}"),
        )
    })?;
    String::from_utf8(body).map_err(|err| io::Error::new(io::ErrorKind::InvalidData, err))
}

fn main_uds(socket_path: &str) {
    // Dial the socket. A failure here — the socket file is missing, or nothing
    // is accepting on it — is the one transport error we name up front, pointing
    // at the path so the failure is actionable rather than a bare errno. No TCP
    // is touched: this is the only connect on the UDS path.
    let mut stream = match UnixStream::connect(socket_path) {
        Ok(s) => s,
        Err(err) => {
            println!(
                "could not connect to the Lemma UDS server at {socket_path:?} ({err}); \
                 is the server running?"
            );
            return;
        }
    };

    // One round-trip: frame out, frame in, parse. The session lives on the
    // connection — no id is echoed back, unlike the HTTP transport. A transport
    // error (write/read) or an unparseable body is returned as Err so the caller
    // can stop cleanly. Takes `&mut UnixStream` so it can reuse the one socket.
    let call = |stream: &mut UnixStream, form: &Value| -> Result<Value, String> {
        let payload = emit_str(form);
        uds_send_frame(stream, &payload)
            .map_err(|err| format!("could not send UDS frame to {socket_path:?} ({err})"))?;
        let raw = uds_recv_frame(stream)
            .map_err(|err| format!("could not read UDS frame from {socket_path:?} ({err})"))?;
        parse_str(&raw)
            .map_err(|err| format!("parsing EDN response from {socket_path:?} ({err}): {raw:?}"))
    };

    // 1. Anonymous hello. The welcome reply carries the session id, which the
    //    server has already pinned to this connection for us — so unlike HTTP we
    //    read it from the body (:session), not a header.
    let body = match call(&mut stream, &verb(vec![sym("hello")])) {
        Ok(b) => b,
        Err(err) => {
            println!("{err}");
            return;
        }
    };
    if get(&body, "event") != Some(&Value::Keyword(Keyword::from_name("welcome"))) {
        println!(
            "hello: expected :welcome, got {} -- {}",
            render(get(&body, "event")),
            describe_failure(&body)
        );
        return;
    }
    println!(
        "hello -> :welcome  version={}  session={}  world={}",
        render(get(&body, "version")),
        render(get(&body, "session")),
        render(get(&body, "world"))
    );

    // 2. Enter the world. (use-world #world "default")
    let body = match call(&mut stream, &verb(vec![sym("use-world"), world("default")])) {
        Ok(b) => b,
        Err(err) => {
            println!("{err}");
            return;
        }
    };
    if is_failure(&body) {
        println!("use-world refused: {}", describe_failure(&body));
        return;
    }
    println!(
        "use-world \"default\" -> {}  world={}",
        render(get(&body, "event")),
        render(get(&body, "world"))
    );

    // 3. Propose a fact: morningstar is equivalent to venus. The reply hands
    //    back a #proposal handle we feed straight into the assert.
    let f = fact_value("equivalent", entity("morningstar"), entity("venus"));
    let body = match call(&mut stream, &verb(vec![sym("propose"), f])) {
        Ok(b) => b,
        Err(err) => {
            println!("{err}");
            return;
        }
    };
    if is_failure(&body) {
        println!("propose refused: {}", describe_failure(&body));
        return;
    }
    // Clone the proposal handle out of the borrowed body so we can feed it into
    // the assert without holding the borrow (same shape as the HTTP path).
    let proposal = match get(&body, "proposal") {
        Some(p) => p.clone(),
        None => {
            println!(
                "propose -> {} but no :proposal handle",
                render(get(&body, "event"))
            );
            return;
        }
    };
    println!(
        "propose (equivalent morningstar venus) -> {}  proposal={}",
        render(get(&body, "event")),
        emit_str(&proposal)
    );

    // 4. Assert the proposed fact into the world.
    let body = match call(&mut stream, &verb(vec![sym("assert"), proposal])) {
        Ok(b) => b,
        Err(err) => {
            println!("{err}");
            return;
        }
    };
    if is_failure(&body) {
        println!("assert refused: {}", describe_failure(&body));
        return;
    }
    println!("assert proposal -> {}", render(get(&body, "event")));

    // 5. Query it back. As in the HTTP path, :find / :where are VECTORS and the
    //    where-clause is a vector of vectors; only the verb head is a list, and
    //    the query variable ?o stays a Symbol.
    let mut query_map = std::collections::BTreeMap::new();
    query_map.insert(
        Value::Keyword(Keyword::from_name("find")),
        Value::Vector(vec![sym("?o")]),
    );
    query_map.insert(
        Value::Keyword(Keyword::from_name("where")),
        Value::Vector(vec![Value::Vector(vec![
            sym("equivalent"),
            entity("morningstar"),
            sym("?o"),
        ])]),
    );
    let body = match call(
        &mut stream,
        &verb(vec![sym("query"), Value::Map(query_map)]),
    ) {
        Ok(b) => b,
        Err(err) => {
            println!("{err}");
            return;
        }
    };
    if is_failure(&body) {
        println!("query refused: {}", describe_failure(&body));
        return;
    }
    println!(
        "query (equivalent morningstar ?o) -> rows={}  done?={}",
        render(get(&body, "rows")),
        render(get(&body, "done?"))
    );

    // 6. Paginated query: seed three subset-of facts in one batched propose,
    //    assert, then drain a :limit 2 query via query_all / (continue #cursor …).
    // Seed three subset-of facts in ONE batched propose, then assert the
    // batch, so the paginated query below spans more than a single page.
    // subset-of is pure-EDB (stored facts) with stable ordering, so it
    // paginates; a rule-headed predicate like member-of cannot be the sole
    // outer :where pattern (server :bad-args :unsupported-rule-call-ordering).
    let f1 = fact_value("subset-of", entity("sub-a"), entity("group"));
    let f2 = fact_value("subset-of", entity("sub-b"), entity("group"));
    let f3 = fact_value("subset-of", entity("sub-c"), entity("group"));
    let propose_form = verb(vec![sym("propose"), f1, f2, f3]);
    let body = match call(&mut stream, &propose_form) {
        Ok(b) => b,
        Err(err) => {
            println!("{err}");
            return;
        }
    };
    if is_failure(&body) {
        println!(
            "propose (3x subset-of) refused: {}",
            describe_failure(&body)
        );
        return;
    }
    let proposal = match get(&body, "proposal") {
        Some(p) => p.clone(),
        None => {
            println!(
                "propose (3x subset-of) -> {} but no :proposal handle",
                render(get(&body, "event"))
            );
            return;
        }
    };
    println!(
        "propose (3x subset-of ? group) -> {}  proposal={}",
        render(get(&body, "event")),
        emit_str(&proposal)
    );
    let body = match call(&mut stream, &verb(vec![sym("assert"), proposal])) {
        Ok(b) => b,
        Err(err) => {
            println!("{err}");
            return;
        }
    };
    if is_failure(&body) {
        println!("assert (3x subset-of) refused: {}", describe_failure(&body));
        return;
    }
    println!("assert proposal -> {}", render(get(&body, "event")));

    // 7. Paginated query: :limit 2 over 3 matching rows yields two pages
    //    (2 + 1). query_all drains them via (continue #cursor …). The UDS
    //    `call` is (stream, form) -> body, so we wrap it in a form -> body
    //    closure that threads the one open socket — the session already rides
    //    the connection.
    let mut paged_query = std::collections::BTreeMap::new();
    paged_query.insert(
        Value::Keyword(Keyword::from_name("find")),
        Value::Vector(vec![sym("?x")]),
    );
    paged_query.insert(
        Value::Keyword(Keyword::from_name("where")),
        Value::Vector(vec![Value::Vector(vec![
            sym("subset-of"),
            sym("?x"),
            entity("group"),
        ])]),
    );
    paged_query.insert(
        Value::Keyword(Keyword::from_name("limit")),
        Value::Integer(2),
    );
    let qform = verb(vec![sym("query"), Value::Map(paged_query)]);
    let mut send = |form: Value| call(&mut stream, &form);
    match query_all(&mut send, qform) {
        Ok((rows, pages, failure)) => {
            if let Some(failure) = failure {
                println!("paged query refused: {}", describe_failure(&failure));
                return;
            }
            println!(
                "paged query (subset-of ? group), limit 2 -> {} rows over {} page(s): {}",
                rows.len(),
                pages,
                emit_str(&Value::Vector(rows))
            );
        }
        Err(err) => {
            println!("{err}");
        }
    }
}

// ---------------------------------------------------------------------------
// Dispatch & entry point
//
// dispatch routes CLI arguments to a transport, mirroring the sibling clients:
// no args runs the HTTP round-trip against the local default; a leading "uds"
// selects the Unix-domain-socket transport (with an optional socket-path
// argument); any other leading argument is an HTTP base URL.
// ---------------------------------------------------------------------------

/// Route CLI arguments (everything after the program name) to a transport.
fn dispatch(args: &[String]) {
    match args.first().map(String::as_str) {
        None => main_run(DEFAULT_BASE),
        // ["uds", path?] selects the Unix-domain-socket transport, taking an
        // optional second argument as the socket path (else DEFAULT_SOCKET).
        Some("uds") => main_uds(args.get(1).map(String::as_str).unwrap_or(DEFAULT_SOCKET)),
        Some(base) => main_run(base),
    }
}

fn main() {
    // No network happens before here — only dispatch (via main_run) touches it.
    let args: Vec<String> = std::env::args().skip(1).collect();
    dispatch(&args);
}

// ===========================================================================
// Tests
//
// WHY THESE TESTS LIVE HERE, AT THE BOTTOM OF THE BINARY SOURCE FILE
//
// This recipe is a *binary* crate (see Cargo.toml's `[[bin]]`), and a binary
// crate exposes no library surface: an out-of-tree `tests/` integration file
// cannot `use lemma_client::post_edn` because there is nothing to import — the
// items here are private to this crate's `main`. The idiomatic Rust answer is
// an in-file `#[cfg(test)] mod tests` block: it compiles only under `cargo
// test`, and `use super::*` reaches the file's private fns directly. So this is
// the sibling test-FILE equivalent for a one-file recipe — the whole client,
// codec, transport, and now its tests, stay readable end to end in one place.
// Mirror of python/test_lemma_client.py's TagRoundTripTests (codec round-trips),
// PostEdnTransportTests (the HTTP seam), and CliDispatchTests (argv routing).
//
// Determinism: every networked test drives a std::net::TcpListener bound to
// 127.0.0.1:0 (an OS-assigned free port) with a scripted std::thread peer.
// Reads are blocking and bounded by the request's own Content-Length; the peer
// signals end-of-response by CLOSING the socket (the client sends
// `Connection: close` and reads to EOF). NO sleeps anywhere — every wait is a
// blocking read or a thread join on a real I/O event.
// ===========================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use std::io::{Read, Write};
    use std::net::{TcpListener, TcpStream};
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::sync::mpsc;
    use std::thread::{self, JoinHandle};

    // -- helpers -----------------------------------------------------------

    /// The raw text of one HTTP request, split into its head and body.
    struct CapturedRequest {
        /// The status line + headers, up to (not including) the blank line.
        head: String,
        /// The decoded request body (exactly Content-Length bytes).
        body: String,
    }

    impl CapturedRequest {
        /// The request's start line, e.g. `POST /v1/messages HTTP/1.1`.
        fn request_line(&self) -> &str {
            self.head.lines().next().unwrap_or("")
        }

        /// Look a header up case-insensitively, returning its trimmed value.
        fn header(&self, name: &str) -> Option<String> {
            self.head.lines().skip(1).find_map(|line| {
                let (k, v) = line.split_once(':')?;
                if k.trim().eq_ignore_ascii_case(name) {
                    Some(v.trim().to_string())
                } else {
                    None
                }
            })
        }
    }

    /// Read one full HTTP request off `stream`: headers up to the blank line,
    /// then exactly Content-Length more bytes of body. Blocking; no timers.
    fn read_http_request(stream: &mut TcpStream) -> CapturedRequest {
        let mut buf = Vec::new();
        let mut byte = [0u8; 1];
        // Read byte-by-byte until the header terminator. Deterministic: the
        // client always writes the full request, so the terminator always
        // arrives without a timeout.
        loop {
            let n = stream.read(&mut byte).expect("read header byte");
            assert_ne!(n, 0, "peer closed before headers completed");
            buf.push(byte[0]);
            if buf.ends_with(b"\r\n\r\n") {
                break;
            }
        }
        let head = String::from_utf8_lossy(&buf[..buf.len() - 4]).to_string();
        let content_length = head
            .lines()
            .skip(1)
            .find_map(|line| {
                let (k, v) = line.split_once(':')?;
                if k.trim().eq_ignore_ascii_case("content-length") {
                    v.trim().parse::<usize>().ok()
                } else {
                    None
                }
            })
            .unwrap_or(0);
        let mut body = vec![0u8; content_length];
        stream.read_exact(&mut body).expect("read body");
        CapturedRequest {
            head,
            body: String::from_utf8_lossy(&body).to_string(),
        }
    }

    /// Spawn a one-shot scripted Lemma peer on a fresh 127.0.0.1 port.
    ///
    /// The thread accepts a single connection, reads the full request, sends
    /// `response_bytes` verbatim, then CLOSES (drops the stream) so the client's
    /// `read_to_end` returns. Returns the dialable base URL and a handle that
    /// yields the captured request once joined.
    fn spawn_scripted_peer(response_bytes: &'static [u8]) -> (String, JoinHandle<CapturedRequest>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind ephemeral port");
        let port = listener.local_addr().expect("local addr").port();
        let base = format!("http://127.0.0.1:{port}");
        let handle = thread::spawn(move || {
            let (mut stream, _peer) = listener.accept().expect("accept connection");
            let request = read_http_request(&mut stream);
            stream.write_all(response_bytes).expect("write response");
            // Dropping `stream` here closes the connection -> client sees EOF.
            request
        });
        (base, handle)
    }

    /// A minimal well-formed HTTP/1.1 response wrapping an EDN `body`, with an
    /// optional session header whose `name` lets a test vary the casing.
    fn http_response(status: &str, session: Option<(&str, &str)>, body: &str) -> Vec<u8> {
        let mut text = format!("HTTP/1.1 {status}\r\nContent-Type: application/edn\r\n");
        if let Some((name, value)) = session {
            text.push_str(&format!("{name}: {value}\r\n"));
        }
        text.push_str(&format!("Content-Length: {}\r\n", body.len()));
        text.push_str("Connection: close\r\n\r\n");
        text.push_str(body);
        text.into_bytes()
    }

    // ===================================================================
    // (A) Codec round-trips — the tagged-literal surface this client owns.
    //     Mirrors python TagRoundTripTests.
    // ===================================================================

    #[test]
    fn test_use_world_verb_form_emits_exact_wire_text() {
        let form = verb(vec![sym("use-world"), world("default")]);
        assert_eq!(emit_str(&form), r#"(use-world #world "default")"#);
    }

    #[test]
    fn test_world_handle_round_trips_through_codec() {
        let v = world("default");
        assert_eq!(parse_str(&emit_str(&v)).unwrap(), v);
    }

    #[test]
    fn test_entity_handle_round_trips_through_codec() {
        let v = entity("alice");
        assert_eq!(parse_str(&emit_str(&v)).unwrap(), v);
    }

    #[test]
    fn test_fact_map_round_trips_through_codec() {
        let v = fact_value("member-of", entity("alice"), entity("managers"));
        assert_eq!(parse_str(&emit_str(&v)).unwrap(), v);
    }

    #[test]
    fn test_result_envelope_parses_to_expected_shape_via_get() {
        let body = parse_str(r#"{:event :result :rows [[#entity "venus"]] :done? true}"#).unwrap();
        assert_eq!(
            get(&body, "event"),
            Some(&Value::Keyword(Keyword::from_name("result")))
        );
        assert_eq!(
            get(&body, "rows"),
            Some(&Value::Vector(vec![Value::Vector(vec![entity("venus")])]))
        );
        assert_eq!(get(&body, "done?"), Some(&Value::Boolean(true)));
    }

    // ===================================================================
    // (B) post_edn over a scripted TcpListener.
    //     Mirrors python PostEdnTransportTests.
    // ===================================================================

    #[test]
    fn test_post_edn_happy_path_returns_parsed_body_and_session_id() {
        let canned = r#"{:event :welcome :version 1 :world #world "default"}"#;
        let (base, peer) = spawn_scripted_peer(Box::leak(
            http_response("200 OK", Some(("X-Lemma-Session", "s-77")), canned).into_boxed_slice(),
        ));

        let (body, session) =
            post_edn("/v1/messages", &verb(vec![sym("hello")]), None, &base).unwrap();
        peer.join().unwrap();

        assert_eq!(body, parse_str(canned).unwrap());
        assert_eq!(session.as_deref(), Some("s-77"));
    }

    #[test]
    fn test_post_edn_captures_session_header_case_insensitively() {
        // The server replies with an all-lowercase x-lemma-session header; the
        // split must still capture it (eq_ignore_ascii_case).
        let canned = r#"{:event :welcome}"#;
        let (base, peer) = spawn_scripted_peer(Box::leak(
            http_response("200 OK", Some(("x-lemma-session", "s-low")), canned).into_boxed_slice(),
        ));

        let (_body, session) =
            post_edn("/v1/messages", &verb(vec![sym("hello")]), None, &base).unwrap();
        peer.join().unwrap();

        assert_eq!(session.as_deref(), Some("s-low"));
    }

    #[test]
    fn test_post_edn_request_line_method_path_and_version() {
        let (base, peer) = spawn_scripted_peer(Box::leak(
            http_response("200 OK", None, "{:event :result}").into_boxed_slice(),
        ));

        post_edn("/v1/messages", &verb(vec![sym("hello")]), None, &base).unwrap();
        let req = peer.join().unwrap();

        assert_eq!(req.request_line(), "POST /v1/messages HTTP/1.1");
    }

    #[test]
    fn test_post_edn_sends_application_edn_content_type() {
        let (base, peer) = spawn_scripted_peer(Box::leak(
            http_response("200 OK", None, "{:event :result}").into_boxed_slice(),
        ));

        post_edn("/v1/messages", &verb(vec![sym("hello")]), None, &base).unwrap();
        let req = peer.join().unwrap();

        assert_eq!(
            req.header("Content-Type").as_deref(),
            Some("application/edn")
        );
    }

    #[test]
    fn test_post_edn_content_length_matches_body_byte_length() {
        let (base, peer) = spawn_scripted_peer(Box::leak(
            http_response("200 OK", None, "{:event :result}").into_boxed_slice(),
        ));

        let form = verb(vec![sym("use-world"), world("default")]);
        post_edn("/v1/messages", &form, None, &base).unwrap();
        let req = peer.join().unwrap();

        let advertised: usize = req.header("Content-Length").unwrap().parse().unwrap();
        assert_eq!(advertised, emit_str(&form).len());
        assert_eq!(advertised, req.body.len());
    }

    #[test]
    fn test_post_edn_body_is_exact_emit_str_text() {
        let (base, peer) = spawn_scripted_peer(Box::leak(
            http_response("200 OK", None, "{:event :result}").into_boxed_slice(),
        ));

        let form = verb(vec![sym("use-world"), world("default")]);
        post_edn("/v1/messages", &form, None, &base).unwrap();
        let req = peer.join().unwrap();

        assert_eq!(req.body, emit_str(&form));
        assert_eq!(req.body, r#"(use-world #world "default")"#);
    }

    #[test]
    fn test_post_edn_without_session_omits_session_request_header() {
        let (base, peer) = spawn_scripted_peer(Box::leak(
            http_response("200 OK", None, "{:event :result}").into_boxed_slice(),
        ));

        post_edn("/v1/messages", &verb(vec![sym("hello")]), None, &base).unwrap();
        let req = peer.join().unwrap();

        assert_eq!(req.header("x-lemma-session"), None);
    }

    #[test]
    fn test_post_edn_with_session_sends_session_request_header() {
        let (base, peer) = spawn_scripted_peer(Box::leak(
            http_response("200 OK", None, "{:event :result}").into_boxed_slice(),
        ));

        post_edn(
            "/v1/sessions/s-77/messages",
            &verb(vec![sym("query")]),
            Some("s-77"),
            &base,
        )
        .unwrap();
        let req = peer.join().unwrap();

        assert_eq!(req.header("x-lemma-session").as_deref(), Some("s-77"));
    }

    #[test]
    fn test_post_edn_error_status_returns_ok_with_parsed_error_envelope() {
        // A 400 still carries a parseable Lemma error envelope; post_edn returns
        // Ok(body) and the caller reads :event — it is NOT a transport error.
        let envelope = r#"{:event :error :reason :malformed :message "bad verb form"}"#;
        let (base, peer) = spawn_scripted_peer(Box::leak(
            http_response("400 Bad Request", None, envelope).into_boxed_slice(),
        ));

        let (body, _session) =
            post_edn("/v1/messages", &verb(vec![sym("hello")]), None, &base).unwrap();
        peer.join().unwrap();

        assert!(is_failure(&body));
        assert_eq!(
            get(&body, "event"),
            Some(&Value::Keyword(Keyword::from_name("error")))
        );
    }

    #[test]
    fn test_post_edn_refused_connection_returns_err_naming_base() {
        // Bind to claim a port, note it, then drop the listener so nothing is
        // listening — the connect must fail with an Err that names the base.
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind ephemeral port");
        let port = listener.local_addr().expect("local addr").port();
        let base = format!("http://127.0.0.1:{port}");
        drop(listener);

        let result = post_edn("/v1/messages", &verb(vec![sym("hello")]), None, &base);

        let err = result.expect_err("connecting to a dropped listener must fail");
        assert!(
            err.contains(&base),
            "error should name the base {base:?}, got: {err}"
        );
    }

    // ===================================================================
    // (C) dispatch — argv shape selects the transport.
    //     Mirrors python CliDispatchTests.
    // ===================================================================

    #[test]
    fn test_dispatch_url_arg_drives_a_real_hello_post_to_messages_endpoint() {
        // A non-welcome reply makes main_run stop after the first call, so the
        // single scripted connection captures exactly the (hello) POST.
        let (base, peer) = spawn_scripted_peer(Box::leak(
            http_response(
                "200 OK",
                Some(("X-Lemma-Session", "s-1")),
                "{:event :error}",
            )
            .into_boxed_slice(),
        ));

        dispatch(&[base]);
        let req = peer.join().unwrap();

        assert_eq!(req.request_line(), "POST /v1/messages HTTP/1.1");
        assert_eq!(req.body, "(hello)");
    }

    #[test]
    fn test_dispatch_uds_opens_no_tcp_connection() {
        // ["uds"] must NOT dial: the scripted listener should see no accept.
        // We check this without sleeping by setting the listener nonblocking and
        // asserting accept() yields WouldBlock immediately after dispatch returns.
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind ephemeral port");
        listener
            .set_nonblocking(true)
            .expect("set listener nonblocking");

        dispatch(&["uds".to_string()]);

        match listener.accept() {
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {} // no connection: correct
            Ok(_) => panic!("uds dispatch unexpectedly opened a TCP connection"),
            Err(e) => panic!("unexpected accept error: {e}"),
        }
    }

    #[test]
    fn test_dispatch_no_args_returns_cleanly_when_default_port_refuses() {
        // Nothing listens on 8080 in the sandbox, so the default-base run hits
        // the refused-connection path: post_edn returns Err, main_run prints it
        // and returns. The contract under test is that dispatch returns without
        // panicking. A channel proves the call completed.
        let (tx, rx) = mpsc::channel();
        let worker = thread::spawn(move || {
            dispatch(&[]);
            tx.send(()).expect("signal completion");
        });
        rx.recv()
            .expect("dispatch with no args should return cleanly");
        worker.join().unwrap();
    }

    // ===================================================================
    // (D) UDS framing — uds_send_frame / uds_recv_frame over a socketpair.
    //     Mirrors python UdsSendFrameTests / UdsRecvFrameTests /
    //     RecvExactlyTests. We use std::os::unix::net::UnixStream::pair() for
    //     an in-memory bidirectional socketpair: writing on one end and reading
    //     on the other exercises the real read_exact/write_all paths with no
    //     listener, no path, no sleeps — every wait is a blocking I/O event.
    // ===================================================================

    #[test]
    fn test_uds_frame_round_trips_across_a_pair() {
        // Send a framed EDN string down one half of a socketpair; recv on the
        // other half must reconstruct the exact same string.
        let (mut a, mut b) = UnixStream::pair().expect("socketpair");
        let edn = emit_str(&verb(vec![sym("use-world"), world("default")]));

        // Write from a scripted peer thread so the blocking recv on `b` has a
        // producer; join proves the write completed.
        let sent = edn.clone();
        let writer = thread::spawn(move || {
            uds_send_frame(&mut a, &sent).expect("send frame");
            // `a` drops here, but the body was already fully written.
        });
        let got = uds_recv_frame(&mut b).expect("recv frame");
        writer.join().unwrap();

        assert_eq!(got, edn);
        // And the reconstructed text parses back to the original form.
        assert_eq!(
            parse_str(&got).unwrap(),
            verb(vec![sym("use-world"), world("default")])
        );
    }

    #[test]
    fn test_uds_send_frame_prefix_is_four_byte_big_endian_length() {
        // A 5-byte body must be prefixed by exactly the bytes [0, 0, 0, 5] —
        // a 4-byte big-endian u32 — before the body itself.
        let (mut a, mut b) = UnixStream::pair().expect("socketpair");
        let body = "hello"; // 5 ASCII bytes
        assert_eq!(body.len(), 5);

        let writer = thread::spawn(move || {
            uds_send_frame(&mut a, body).expect("send frame");
        });
        // Read the raw 4 prefix bytes + 5 body bytes off the wire directly,
        // bypassing uds_recv_frame, so we assert the on-wire prefix shape.
        let mut raw = [0u8; 9];
        b.read_exact(&mut raw).expect("read raw frame bytes");
        writer.join().unwrap();

        assert_eq!(&raw[..4], &[0, 0, 0, 5]);
        assert_eq!(&raw[4..], b"hello");
    }

    #[test]
    fn test_uds_recv_frame_reassembles_a_frame_split_across_writes() {
        // UnixStream is a byte stream, so read_exact reassembles a frame that
        // arrives in many small writes. The peer writes the 4-byte prefix one
        // byte at a time, then the body in two halves; recv must still return
        // the whole EDN string. (No sleeps — each write is a real I/O event the
        // blocking reader picks up.)
        let (mut a, mut b) = UnixStream::pair().expect("socketpair");
        let edn = r#"{:event :result :rows [["venus"]] :done? true}"#;
        let body = edn.as_bytes().to_vec();
        let prefix = (body.len() as u32).to_be_bytes();

        let writer = thread::spawn(move || {
            for byte in prefix {
                a.write_all(&[byte]).expect("write prefix byte");
                a.flush().expect("flush prefix byte");
            }
            let mid = body.len() / 2;
            a.write_all(&body[..mid]).expect("write body first half");
            a.flush().expect("flush body first half");
            a.write_all(&body[mid..]).expect("write body second half");
            a.flush().expect("flush body second half");
        });
        let got = uds_recv_frame(&mut b).expect("recv reassembled frame");
        writer.join().unwrap();

        assert_eq!(got, edn);
    }

    #[test]
    fn test_uds_recv_frame_premature_eof_in_prefix_errs_naming_expected_bytes() {
        // The peer writes 2 of the 4 prefix bytes then closes. recv must Err
        // (not hang, not short-read) with a message naming the prefix byte
        // count it expected. Mirrors python RecvExactly premature-EOF.
        let (mut a, mut b) = UnixStream::pair().expect("socketpair");

        let writer = thread::spawn(move || {
            a.write_all(&[0u8, 0u8]).expect("write partial prefix");
            // Drop `a` -> peer closed -> reader sees EOF mid-prefix.
        });
        let err = uds_recv_frame(&mut b).expect_err("truncated prefix must Err");
        writer.join().unwrap();

        let msg = err.to_string();
        assert!(
            msg.contains("4 bytes expected"),
            "prefix EOF error should name the 4-byte prefix, got: {msg}"
        );
    }

    #[test]
    fn test_uds_recv_frame_premature_eof_in_body_errs_naming_expected_bytes() {
        // A valid prefix declaring 10 body bytes, but only 5 arrive before the
        // peer closes. recv must Err with a message naming the declared body
        // byte count. Mirrors python recv_frame premature-EOF-in-body.
        let (mut a, mut b) = UnixStream::pair().expect("socketpair");

        let writer = thread::spawn(move || {
            a.write_all(&10u32.to_be_bytes()).expect("write prefix");
            a.write_all(b"short").expect("write partial body"); // 5 of 10 bytes
            // Drop `a` -> EOF mid-body.
        });
        let err = uds_recv_frame(&mut b).expect_err("truncated body must Err");
        writer.join().unwrap();

        let msg = err.to_string();
        assert!(
            msg.contains("10 bytes expected"),
            "body EOF error should name the declared 10-byte body, got: {msg}"
        );
    }

    // ===================================================================
    // (E) main_uds against a scripted UnixListener.
    //     Mirrors python UdsHandshake* tests, but with a real socket pair
    //     over a unique temp path instead of a monkeypatched socket factory.
    //     A drop guard removes the socket file even if a test panics; a
    //     std::thread peer reads each request frame and replies in lockstep.
    // ===================================================================

    /// Removes the bound socket path on drop, so a panicking test never leaves
    /// a stale file in temp_dir. Holds the listener for the same lifetime.
    struct UdsPeerGuard {
        path: std::path::PathBuf,
    }

    impl Drop for UdsPeerGuard {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.path);
        }
    }

    /// A unique socket path under the system temp dir. Uniqueness comes from
    /// the pid plus a process-global counter, so concurrent tests never collide.
    fn unique_socket_path() -> std::path::PathBuf {
        use std::sync::atomic::{AtomicU64, Ordering};
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!("lemma-uds-test-{}-{n}.sock", std::process::id()))
    }

    /// Frame each canned reply the way the server would (4-byte big-endian
    /// length prefix + UTF-8 body), for raw writing back to the client.
    fn frame_bytes(edn: &str) -> Vec<u8> {
        let body = edn.as_bytes();
        let mut out = (body.len() as u32).to_be_bytes().to_vec();
        out.extend_from_slice(body);
        out
    }

    /// Bind a scripted UDS peer that accepts one connection and, for each
    /// element of `replies`, reads exactly one request frame off the client
    /// then writes that reply frame back. Returns the socket path, a drop guard,
    /// and a join handle yielding the decoded EDN strings the client sent.
    fn spawn_scripted_uds_peer(
        replies: Vec<String>,
    ) -> (std::path::PathBuf, UdsPeerGuard, JoinHandle<Vec<String>>) {
        let path = unique_socket_path();
        let _ = std::fs::remove_file(&path); // clear any stale file first
        let listener = UnixListener::bind(&path).expect("bind unix listener");
        let guard = UdsPeerGuard { path: path.clone() };

        let handle = thread::spawn(move || {
            let (mut stream, _addr) = listener.accept().expect("accept uds connection");
            let mut received = Vec::new();
            for reply in &replies {
                // Read one request frame from the client (same wire format).
                let mut prefix = [0u8; 4];
                if stream.read_exact(&mut prefix).is_err() {
                    break; // client stopped early (e.g. failure path)
                }
                let len = u32::from_be_bytes(prefix) as usize;
                let mut body = vec![0u8; len];
                stream.read_exact(&mut body).expect("read request body");
                received.push(String::from_utf8(body).expect("utf8 request"));
                // Reply in lockstep.
                stream
                    .write_all(&frame_bytes(reply))
                    .expect("write reply frame");
            }
            // Drop `stream` -> client sees EOF on any further read.
            received
        });

        (path, guard, handle)
    }

    // Canned UDS reply bodies, mirroring the python fixtures' base five-step
    // sequence (the Rust recipe has no pagination/watch tail). The welcome
    // carries the session as a #session handle in its BODY (:session) — over
    // UDS the client reads it there, never echoes it back.
    const UDS_WELCOME: &str =
        r#"{:event :welcome :version 1 :session #session "s-uds-1" :world #world "default"}"#;
    const UDS_WORLD_SELECTED: &str = r#"{:event :world-selected :world #world "default"}"#;
    const UDS_PROPOSED: &str = r#"{:event :proposed :proposal #proposal "p-1"}"#;
    const UDS_ASSERTED: &str = r#"{:event :asserted}"#;
    const UDS_RESULT: &str = r#"{:event :result :rows [["venus"]] :done? true}"#;

    fn uds_full_sequence() -> Vec<String> {
        [
            UDS_WELCOME,
            UDS_WORLD_SELECTED,
            UDS_PROPOSED,
            UDS_ASSERTED,
            UDS_RESULT,
        ]
        .iter()
        .map(|s| s.to_string())
        .collect()
    }

    #[test]
    fn test_uds_first_sent_frame_is_anonymous_hello_with_no_session() {
        // The first frame main_uds sends must be exactly the anonymous (hello)
        // verb form, carrying no session anywhere — there is no session to know
        // before the welcome arrives.
        let (path, _guard, peer) = spawn_scripted_uds_peer(uds_full_sequence());

        main_uds(path.to_str().unwrap());
        let sent = peer.join().unwrap();

        assert_eq!(sent[0], "(hello)");
        assert!(!sent[0].contains("session"));
        assert!(!sent[0].contains("s-uds-1"));
    }

    #[test]
    fn test_uds_no_frame_after_hello_echoes_the_session_handle() {
        // CRITICAL: over UDS the server binds the session to the connection, so
        // the client must NOT re-send the session id in any later frame. No
        // sent frame may contain a #session handle, the :session key, or the
        // session value text.
        let (path, _guard, peer) = spawn_scripted_uds_peer(uds_full_sequence());

        main_uds(path.to_str().unwrap());
        let sent = peer.join().unwrap();

        // All five steps were sent (we reached the query), and none leaks the
        // session handle.
        assert_eq!(sent.len(), 5);
        for frame in &sent {
            assert!(
                !frame.contains("#session"),
                "frame leaked #session: {frame}"
            );
            assert!(
                !frame.contains("s-uds-1"),
                "frame leaked session id: {frame}"
            );
            assert!(
                !frame.contains(":session"),
                "frame leaked :session: {frame}"
            );
        }
    }

    #[test]
    fn test_uds_proposal_handle_is_threaded_into_assert_frame() {
        // The #proposal handle from the propose reply must be fed verbatim into
        // the assert frame: (assert #proposal "p-1"). Frame index 3 is the
        // assert (0 hello, 1 use-world, 2 propose, 3 assert, 4 query).
        let (path, _guard, peer) = spawn_scripted_uds_peer(uds_full_sequence());

        main_uds(path.to_str().unwrap());
        let sent = peer.join().unwrap();

        let assert_form = parse_str(&sent[3]).unwrap();
        assert_eq!(
            assert_form,
            verb(vec![
                sym("assert"),
                Value::TaggedElement(Symbol::from_name("proposal"), Box::new(Value::from("p-1")),),
            ])
        );
    }

    #[test]
    fn test_uds_nonexistent_socket_path_returns_fast_without_connecting() {
        // main_uds prints rather than returns its connect error, so we assert
        // the wire-level absence (no listener was ever bound, so nothing could
        // accept) plus a clean, prompt return. A channel proves it returned;
        // the path points at a temp file that does not exist.
        let path = unique_socket_path();
        let _ = std::fs::remove_file(&path); // ensure it does not exist
        let target = path.to_str().unwrap().to_string();

        let (tx, rx) = mpsc::channel();
        let worker = thread::spawn(move || {
            main_uds(&target);
            tx.send(()).expect("signal completion");
        });
        // Returns cleanly: recv succeeds because the connect fails fast and
        // main_uds returns rather than hanging.
        rx.recv()
            .expect("main_uds on a missing socket should return cleanly");
        worker.join().unwrap();

        // Wire-level absence: no socket file was created by the connect attempt.
        assert!(!path.exists(), "connect must not create the socket file");
    }

    // A tiny guard that the BTreeMap-keyed envelope helpers agree with `get` on
    // a hand-built map (keeps the fact-map ordering assumption honest).
    #[test]
    fn test_get_reads_keyword_field_from_hand_built_map() {
        let mut map = BTreeMap::new();
        map.insert(
            Value::Keyword(Keyword::from_name("event")),
            Value::Keyword(Keyword::from_name("welcome")),
        );
        let body = Value::Map(map);
        assert_eq!(
            get(&body, "event"),
            Some(&Value::Keyword(Keyword::from_name("welcome")))
        );
        assert_eq!(get(&body, "missing"), None);
    }

    // ===================================================================
    // (F) Cursor pagination: query_all(send, query_form)
    //     -> (rows, pages, failure). Mirrors python QueryAllTests /
    //     _ScriptedSend.
    //
    //     query_all is transport-agnostic: it takes a `form -> body` closure
    //     and drains a paginated query by threading the #cursor through
    //     (continue ...) until :done? is true. We drive it with a purely
    //     in-memory scripted fake -- no socket, no TcpStream -- that parses a
    //     canned EDN reply STRING per call (via parse_str AT CALL TIME, so
    //     `#cursor "c-1"` decodes to the same TaggedElement the loop feeds back
    //     into (continue #cursor ...)) and records the form it was handed each
    //     call into a Vec the test inspects afterwards.
    // ===================================================================

    /// A scripted `form -> body` stand-in for `query_all`'s `send` closure.
    ///
    /// Holds a list of scripted replies; each call pops the next and either
    /// parses a canned EDN body STRING via `parse_str` at call time (so a
    /// `#cursor "c-1"` decodes into the real `TaggedElement` the loop feeds
    /// back) or yields a transport `Err`. Every `form` it is handed is recorded
    /// into `forms` for after-the-fact inspection. Running past the script
    /// panics -- over-driving `send` is a bug worth surfacing, not a silent
    /// empty read. Mirrors python's `_ScriptedSend`.
    enum ScriptedReply {
        /// A canned EDN reply body, parsed with `parse_str` when popped.
        Body(&'static str),
        /// A transport-level failure: `send` returns `Err(message)`.
        TransportErr(&'static str),
    }

    struct ScriptedSend {
        replies: Vec<ScriptedReply>,
        next: usize,
        /// Every form handed to `send`, in call order, for the test to inspect.
        forms: Vec<Value>,
    }

    impl ScriptedSend {
        fn new(replies: Vec<ScriptedReply>) -> Self {
            ScriptedSend {
                replies,
                next: 0,
                forms: Vec::new(),
            }
        }

        /// One `send` invocation: record `form`, then yield the next scripted
        /// reply (parsing the EDN string now) or a transport `Err`.
        fn call(&mut self, form: Value) -> Result<Value, String> {
            self.forms.push(form);
            let reply = self
                .replies
                .get(self.next)
                .expect("ScriptedSend over-driven: query_all called send past its script");
            self.next += 1;
            match reply {
                ScriptedReply::Body(edn) => {
                    Ok(parse_str(edn).expect("scripted reply must be valid EDN"))
                }
                ScriptedReply::TransportErr(msg) => Err((*msg).to_string()),
            }
        }

        fn call_count(&self) -> usize {
            self.forms.len()
        }
    }

    /// A representative initial `(query …)` form. `query_all` never inspects it
    /// -- it just hands it to `send` unchanged on the first call -- so the shape
    /// only needs to be plausible, not server-validated. Mirrors python's
    /// `_QFORM`.
    fn qform() -> Value {
        let mut map = BTreeMap::new();
        map.insert(
            Value::Keyword(Keyword::from_name("find")),
            Value::Vector(vec![sym("?x")]),
        );
        map.insert(
            Value::Keyword(Keyword::from_name("where")),
            Value::Vector(vec![Value::Vector(vec![
                sym("subset-of"),
                sym("?x"),
                entity("group"),
            ])]),
        );
        map.insert(
            Value::Keyword(Keyword::from_name("limit")),
            Value::Integer(2),
        );
        verb(vec![sym("query"), Value::Map(map)])
    }

    // --- (A) Multi-page drain ---------------------------------------------

    #[test]
    fn test_query_all_multi_page_concatenates_rows_in_order() {
        // Page 1: rows [a b], not done, cursor #cursor "c-1".
        // Page 2: rows [c], done. Rows flatten in page order; pages == 2;
        // failure is None.
        let mut send = ScriptedSend::new(vec![
            ScriptedReply::Body(
                r#"{:event :result :rows [[#entity "a"] [#entity "b"]] :done? false :cursor #cursor "c-1"}"#,
            ),
            ScriptedReply::Body(r#"{:event :result :rows [[#entity "c"]] :done? true}"#),
        ]);

        let (rows, pages, failure) = query_all(&mut |f| send.call(f), qform()).unwrap();

        assert_eq!(failure, None);
        assert_eq!(pages, 2);
        assert_eq!(
            rows,
            vec![
                Value::Vector(vec![entity("a")]),
                Value::Vector(vec![entity("b")]),
                Value::Vector(vec![entity("c")]),
            ]
        );
    }

    #[test]
    fn test_query_all_second_form_is_continue_carrying_the_page_one_cursor() {
        // The first recorded form is the original query; the second is exactly
        // (continue <the page-1 cursor>), verified BOTH by Value equality and by
        // emit_str wire text -- the cursor is the parsed #cursor TaggedElement
        // from page 1, fed back verbatim (never reconstructed from text).
        let mut send = ScriptedSend::new(vec![
            ScriptedReply::Body(
                r#"{:event :result :rows [[#entity "a"] [#entity "b"]] :done? false :cursor #cursor "c-1"}"#,
            ),
            ScriptedReply::Body(r#"{:event :result :rows [[#entity "c"]] :done? true}"#),
        ]);

        query_all(&mut |f| send.call(f), qform()).unwrap();

        assert_eq!(send.forms[0], qform());
        // The page-1 cursor, exactly as it parsed off the wire.
        let page_one_cursor =
            Value::TaggedElement(Symbol::from_name("cursor"), Box::new(Value::from("c-1")));
        let expected_continue = verb(vec![sym("continue"), page_one_cursor]);
        // By Value equality.
        assert_eq!(send.forms[1], expected_continue);
        // And by exact wire text.
        assert_eq!(emit_str(&send.forms[1]), emit_str(&expected_continue));
        assert_eq!(emit_str(&send.forms[1]), r#"(continue #cursor "c-1")"#);
    }

    // --- (B) Single page ---------------------------------------------------

    #[test]
    fn test_query_all_single_page_done_true_sends_once_without_continue() {
        // :done? true on the first page with NO :cursor key: exactly one send,
        // rows returned, no continue issued, and the missing :cursor never
        // panics (the continue branch is unreached).
        let mut send = ScriptedSend::new(vec![ScriptedReply::Body(
            r#"{:event :result :rows [[#entity "a"]] :done? true}"#,
        )]);

        let (rows, pages, failure) = query_all(&mut |f| send.call(f), qform()).unwrap();

        assert_eq!(failure, None);
        assert_eq!(pages, 1);
        assert_eq!(rows, vec![Value::Vector(vec![entity("a")])]);
        assert_eq!(send.call_count(), 1);
        assert_eq!(send.forms, vec![qform()]);
    }

    // --- (C) Continue failure (expired cursor) -----------------------------

    #[test]
    fn test_query_all_continue_failure_returns_rows_so_far_and_the_envelope() {
        // Page 1 not done + cursor; the (continue …) comes back an :error
        // :unknown-handle envelope. query_all yields Ok with the rows gathered
        // from page 1, pages == 1 (only the drained page counts), and the error
        // body returned verbatim as `failure`.
        let error_envelope =
            r#"{:event :error :reason :unknown-handle :message "cursor c-1 has expired"}"#;
        let mut send = ScriptedSend::new(vec![
            ScriptedReply::Body(
                r#"{:event :result :rows [[#entity "a"] [#entity "b"]] :done? false :cursor #cursor "c-1"}"#,
            ),
            ScriptedReply::Body(error_envelope),
        ]);

        let (rows, pages, failure) = query_all(&mut |f| send.call(f), qform()).unwrap();

        assert_eq!(
            rows,
            vec![
                Value::Vector(vec![entity("a")]),
                Value::Vector(vec![entity("b")]),
            ]
        );
        assert_eq!(pages, 1);
        assert_eq!(failure, Some(parse_str(error_envelope).unwrap()));
        // Two sends: the query and the failing continue.
        assert_eq!(send.call_count(), 2);
    }

    // --- (D) First-reply failure -------------------------------------------

    #[test]
    fn test_query_all_first_reply_failure_returns_empty_zero_pages_no_continue() {
        // A query that is refused outright (a :rejected envelope on the FIRST
        // reply) yields Ok((empty rows, 0 pages, Some(envelope))) and never
        // issues a continue.
        let rejected = r#"{:event :rejected :reason :forbidden :message "no read access"}"#;
        let mut send = ScriptedSend::new(vec![ScriptedReply::Body(rejected)]);

        let (rows, pages, failure) = query_all(&mut |f| send.call(f), qform()).unwrap();

        assert_eq!(rows, Vec::<Value>::new());
        assert_eq!(pages, 0);
        assert_eq!(failure, Some(parse_str(rejected).unwrap()));
        // Exactly one send: the failing query. No continue was issued.
        assert_eq!(send.call_count(), 1);
        assert_eq!(send.forms, vec![qform()]);
    }

    // --- (E) Transport-error propagation (exceeds python parity) -----------

    #[test]
    fn test_query_all_transport_error_mid_drain_propagates_as_err() {
        // Beyond the python suite: the Rust signature returns Result<_, E>, so a
        // `send` that returns Err mid-drain (page 1 OK and not done, then the
        // continue's transport fails) propagates straight out as Err rather than
        // being folded into the failure envelope.
        let mut send = ScriptedSend::new(vec![
            ScriptedReply::Body(
                r#"{:event :result :rows [[#entity "a"] [#entity "b"]] :done? false :cursor #cursor "c-1"}"#,
            ),
            ScriptedReply::TransportErr("connection reset mid-drain"),
        ]);

        let result = query_all(&mut |f| send.call(f), qform());

        let err = result.expect_err("a transport Err mid-drain must propagate as Err");
        assert_eq!(err, "connection reset mid-drain");
        // The query and the failing continue were both attempted.
        assert_eq!(send.call_count(), 2);
    }
}
