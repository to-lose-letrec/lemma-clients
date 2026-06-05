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

use std::io::{Read, Write};
use std::net::TcpStream;

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
}

// ---------------------------------------------------------------------------
// Dispatch & entry point
//
// dispatch routes CLI arguments to a transport, mirroring the sibling clients:
// no args runs the HTTP round-trip against the local default; a leading "uds"
// is reserved for the Unix-domain-socket transport (not yet implemented); any
// other leading argument is an HTTP base URL.
// ---------------------------------------------------------------------------

/// Route CLI arguments (everything after the program name) to a transport.
fn dispatch(args: &[String]) {
    match args.first().map(String::as_str) {
        None => main_run(DEFAULT_BASE),
        // "uds" is reserved for the UDS transport, which lands next.
        Some("uds") => println!("uds transport: not yet implemented"),
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
}
