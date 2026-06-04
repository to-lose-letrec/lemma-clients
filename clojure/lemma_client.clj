;; lemma_client.clj — a single-file Clojure client for the Lemma wire protocol.
;;
;; This file is a *recipe*, not a library: read it end to end. Clojure is the
;; protocol's native language, so there is nothing to install. Lemma speaks EDN
;; (Clojure's own data syntax) over HTTP, and both halves of that — the codec
;; and the transport — already live in the JDK:
;;
;;   * `clojure.edn`  parses inbound EDN into ordinary Clojure values.
;;   * `pr-str`       renders outbound Clojure values back to EDN text.
;;   * `java.net.http` (JDK 11+) is the HTTP client.
;;
;; ZERO client dependencies. `deps.edn` carries none.
;;
;; EDN in a nutshell
;; -----------------
;; The pieces Lemma uses and their Clojure values:
;;
;;     nil true false              -- nil / true / false
;;     42  3.14                     -- long / double
;;     "a string"                  -- string
;;     :event  :verbs/core         -- keyword
;;     equivalent  ?o              -- symbol (?-vars are symbols)
;;     ( a b c )                   -- a LIST   — verb forms only: (hello), (query …)
;;     [ a b c ]                   -- a VECTOR — everywhere inside the args
;;     { k v }                     -- a map
;;     #{ a b }                    -- a set
;;     #tag payload                -- a tagged literal (see below)
;;
;; Lists vs. vectors is the one load-bearing distinction (grammar §3): a LIST
;; appears ONLY as the top-level verb form — `(propose …)`, `(query …)`,
;; `(hello)`. Inside the arguments every collection is a VECTOR, map, or set —
;; never a list. We build verb forms with `list` (or quoting) and everything
;; else with `[]` / `{}`, so the distinction falls straight out of Clojure's
;; reader.
;;
;; Tagged literals
;; ---------------
;; The core Lemma tags are `#fact #violation #entity #proposal #tx #ref #cursor
;; #watch #session #world` (grammar §5). Rather than register a `data_readers.clj`
;; we build outbound tags with `clojure.core/tagged-literal`, which renders as
;; `#tag payload` under `pr-str`, and parse inbound EDN with an unknown-tag
;; `:default` of `tagged-literal` — so any tag the server sends (a `#proposal`,
;; `#entity`, even one we have never seen) round-trips back onto the wire
;; cleanly without a reader registration. Loading this file performs NO network
;; I/O; only `-main` touches the network.

(ns lemma-client
  (:require [clojure.edn :as edn]
            [clojure.string :as str])
  (:import (java.net URI
                     InetSocketAddress
                     Socket
                     SocketTimeoutException
                     UnixDomainSocketAddress
                     StandardProtocolFamily)
           (java.net.http HttpClient
                          HttpRequest
                          HttpResponse$BodyHandlers
                          HttpRequest$BodyPublishers)
           (java.nio ByteBuffer)
           (java.nio.channels SocketChannel)
           (java.nio.charset StandardCharsets)))

;; Where a locally booted Dianoia HTTP listener lives by default (SPEC examples).
;; Override by passing a base URL as the first CLI argument.
(def default-base "http://127.0.0.1:8080")

;; Where a locally booted Dianoia UDS listener binds by default (see Dianoia's
;; transport/uds.clj `start!` :socket-path). Override by passing a path after
;; the `uds` CLI argument.
(def default-socket "/tmp/dianoia.sock")

;; One shared client for the whole session. Building it is pure (no connection
;; is opened until the first send), so it is safe at load time.
(def ^HttpClient client (.build (HttpClient/newBuilder)))

;; ---------------------------------------------------------------------------
;; Tag constructors
;;
;; Thin wrappers so the round-trip below reads as prose. Each produces a
;; tagged-literal that `pr-str` renders as the matching `#tag payload`.
;; ---------------------------------------------------------------------------

(defn ent  "Build an #entity \"<name>\" handle (grammar §5.3)." [n] (tagged-literal 'entity n))
(defn wrld "Build a #world \"<name>\" handle (grammar §5)."      [n] (tagged-literal 'world  n))
(defn fct  "Build a #fact {…} (grammar §5.1) from its key/value map." [m] (tagged-literal 'fact m))

;; ---------------------------------------------------------------------------
;; Capabilities & limits:  the :welcome surface  ->  ServerInfo
;;
;; Every session opens with a (hello) whose :welcome reply advertises what the
;; server can do (SPEC §10): a :capabilities set of namespaced flag keywords, a
;; :limits map of resource caps, and the :verbs / :predicates the world exposes.
;; A well-behaved client reads this once and tailors itself to it — skipping
;; features the server doesn't advertise and staying under the byte caps it
;; enforces.
;;
;; Because Lemma speaks EDN — Clojure's own data syntax — the whole surface
;; parses straight into native values: :capabilities is a set of keywords,
;; :limits a map, :verbs / :predicates maps of {:core #{…} :extensions {pack
;; #{…}}}. There is no codec layer to cross, so ServerInfo is just a plain map
;; (not a new abstraction): the parsed, queryable form of the welcome. The
;; round-trip below can then ask "does this server paginate?" or "is my message
;; small enough?" in one readable call.
;; ---------------------------------------------------------------------------

(defn- flatten-surface
  "Merge a `{:core #{…} :extensions {pack #{…}}}` surface into one flat set.

  The :verbs and :predicates entries of a welcome split names into a :core set
  plus per-pack :extensions sets (SPEC §10). A client mostly just wants \"every
  name this server understands\", so we union :core with all the extension sets.
  Missing keys default to empty — a minimal welcome need not carry every section."
  [surface]
  (let [core       (set (:core surface))
        extensions (vals (:extensions surface))]
    (reduce into core extensions)))

(defn read-welcome
  "Parse a :welcome `body` map into a ServerInfo map (SPEC §10).

  Pulls :version; :capabilities (a set of keywords); :limits (a map of resource
  caps); and the flattened :verbs / :predicates surfaces (each a flat set of
  symbols with :core unioned across all :extensions packs). Every key is
  optional: a server that omits a section yields an empty default (empty set /
  empty map) rather than an error, so this stays robust against minimal
  welcomes."
  [body]
  {:capabilities (set (:capabilities body))
   :limits       (or (:limits body) {})
   :verbs        (flatten-surface (:verbs body))
   :predicates   (flatten-surface (:predicates body))
   :version      (:version body)})

(defn supports?
  "True iff `info` advertises `cap` (a keyword, e.g. :lemma/cursor-pagination)."
  [info cap]
  (contains? (:capabilities info) cap))

(defn max-message-bytes
  "The server's :max-message-bytes limit, or nil if it advertised none.

  A nil value means \"unadvertised\" — treated as unlimited by
  `within-message-limit?`."
  [info]
  (get-in info [:limits :max-message-bytes]))

(defn within-message-limit?
  "True iff `edn-str` fits under the server's :max-message-bytes cap.

  The limit is measured in UTF-8 bytes (SPEC §10). An unadvertised limit (nil)
  means unlimited, so any message passes."
  [info edn-str]
  (let [m (max-message-bytes info)]
    (or (nil? m)
        (<= (count (.getBytes edn-str "UTF-8")) m))))

;; ---------------------------------------------------------------------------
;; HTTP transport:  EDN form  ->  POST  ->  parsed EDN response
;;
;; `http-send` is the single point where bytes actually leave the process: it
;; takes a built HttpRequest and returns the HttpResponse. It is pulled out as
;; its own `defn` precisely so tests can `with-redefs` it to a canned response
;; and exercise `post-edn` (header threading, EDN encode/decode, the failure
;; path) without a live server.
;; ---------------------------------------------------------------------------

(defn http-send
  "Send `req` and return the HttpResponse (body as string). The one I/O seam;
  tests rebind this with `with-redefs`."
  [^HttpRequest req]
  (.send client req (HttpResponse$BodyHandlers/ofString)))

(defn post-edn
  "POST an EDN `form` to `base` + `path`; return `{:body <edn> :session <sid>}`.

  `form` is any Clojure value `pr-str` can render — typically a verb LIST such
  as `(list 'hello)`. It is encoded to EDN text and sent as `application/edn`;
  when `:session` is given it is echoed back in the `x-lemma-session` request
  header so the server attaches the call to an existing session. The response
  body is parsed with an unknown-tag `:default` of `tagged-literal`, so the
  SPEC §5 handles round-trip clean. `:session` in the result is the
  `X-Lemma-Session` response header value, or nil if absent.

  An HTTP error status (4xx/5xx) still carries a valid Lemma EDN error envelope
  in its body, so we parse and return it — the caller inspects `:event` to tell
  a welcome from an error. A connection-level failure (server down/refused) is
  re-thrown as an `ex-info` naming the `base` URL, so the failure is actionable
  rather than a bare Java exception."
  [path form & {:keys [session base] :or {base default-base}}]
  (let [req (cond-> (HttpRequest/newBuilder)
              true    (.uri (URI/create (str base path)))
              true    (.header "content-type" "application/edn")
              session (.header "x-lemma-session" session)
              true    (.POST (HttpRequest$BodyPublishers/ofString (pr-str form)))
              true    (.build))
        rsp (try
              (http-send req)
              (catch java.io.IOException e
                ;; No HTTP status at all — we never reached a Lemma server.
                ;; Name the base so the failure points at what to fix. Some JDK
                ;; connect failures carry a null message, so fall back to the
                ;; exception's simple class name for a non-empty cause.
                (let [cause (or (not-empty (.getMessage e))
                                (.. e getClass getSimpleName))]
                  (throw (ex-info (str "could not reach the Lemma server at " base
                                       " (" cause "); is the server running?")
                                  {:base base} e)))))]
    {:body    (edn/read-string {:default tagged-literal} (.body rsp))
     :session (-> rsp .headers (.firstValue "x-lemma-session") (.orElse nil))}))

;; ---------------------------------------------------------------------------
;; Envelope inspection
;;
;; Every Lemma reply is a map keyed by `:event`. Two values mean refusal:
;; `:error` (malformed/illegal) and `:rejected` (well-formed but disallowed,
;; e.g. a consistency violation). One small predicate keeps the round-trip flat.
;; ---------------------------------------------------------------------------

(defn- failure?
  "True if `body` is an error/rejection envelope."
  [body]
  (contains? #{:error :rejected} (:event body)))

(defn- describe-failure
  "Format the salient `:reason` / `:message` of a refusal for printing."
  [body]
  (->> [(when-let [r (:reason body)]  (str ":reason " (pr-str r)))
        (when-let [m (:message body)] (str ":message " (pr-str m)))]
       (remove nil?)
       (str/join "; ")
       (#(if (seq %) % "(no detail provided)"))))

;; ---------------------------------------------------------------------------
;; Cursor pagination:  drain a (query …) across (continue #cursor …) pages
;;
;; A query with :limit returns a full first page with :done? false plus a
;; #cursor; (continue #cursor) carries the next :rows/:cursor/:done? until
;; :done? is true (SPEC §8). `query-all` walks that chain to a flat row set.
;; `send` is a `form -> body` closure (the per-transport adapter), so the same
;; pagination loop serves both HTTP and UDS.
;; ---------------------------------------------------------------------------

(defn query-all
  "Run `query-form` and drain every page via (continue #cursor …).

  `send` is a `(fn [form] body)` closure — it sends one verb LIST and returns
  the parsed reply body. Returns `{:rows <all-rows> :pages <n> :failure nil}`
  on success, or `{:rows … :pages … :failure <body>}` if any page is refused
  (the rows gathered so far are still returned).

  A query with :limit yields a full first page with :done? false and a
  `#cursor` tagged literal; the cursor is present ONLY while :done? is falsey
  (the server omits it on an already-done result), so we read it only inside
  the loop. An expired cursor (server idle TTL ~300s, SPEC §8) comes back as
  :error :unknown-handle; this demo PROPAGATES that failure, whereas a real
  client would re-issue the original query to start a fresh page."
  [send query-form]
  (let [body (send query-form)]
    (if (failure? body)
      {:rows [] :pages 0 :failure body}
      (loop [rows  (vec (:rows body))
             pages 1
             body  body]
        (if (:done? body)
          {:rows rows :pages pages :failure nil}
          (let [next-body (send (list 'continue (:cursor body)))]
            (if (failure? next-body)
              {:rows rows :pages pages :failure next-body}
              (recur (into rows (:rows next-body))
                     (inc pages)
                     next-body))))))))

;; ---------------------------------------------------------------------------
;; Watch over HTTP:  the SSE event stream  ->  parsed :watch-event envelopes
;;
;; A (watch-pattern …) call registers a standing query; matching changes are
;; then *pushed* to the session rather than polled. Over HTTP those pushes
;; arrive on a separate Server-Sent-Events stream, GET /v1/sessions/{id}/events
;; (SPEC §9). SSE is a one-way text stream: each event is one or more `data:`
;; lines terminated by a blank line; `:`-prefixed lines are keep-alive comments
;; to be ignored.
;;
;; Why a raw socket instead of `java.net.http`? Dianoia (http-kit) serves the
;; stream with `Transfer-Encoding: chunked` and writes an immediate size-0 chunk
;; to flush the response headers before any event exists. `java.net.http`'s
;; streaming body handler treats that size-0 chunk as end-of-body and reports
;; EOF, closing the stream before the first event ever arrives. So we speak HTTP
;; by hand over a raw `java.net.Socket` — the same "encode, send, decode" shape
;; the UDS transport already uses — and treat a size-0 chunk as a keep-alive
;; flush (skip it, keep reading) rather than as the end of the stream. The
;; socket carries an SO_TIMEOUT so a quiet stream can never hang the demo: a
;; read that blocks past the timeout throws `SocketTimeoutException`, which we
;; treat as "no event observed" rather than an error.
;;
;; ORDERING IS LOAD-BEARING. Dianoia registers the per-session SSE sink LAZILY,
;; at the moment the GET /events connection's headers are written — and the
;; watch dispatcher delivers a :watch-event only to sinks present at emit time,
;; with NO backlog replay. So the stream must be OPENED (sink registered) BEFORE
;; the change that triggers the event, or the push races ahead of the sink and
;; is lost. We therefore split the work in two:
;;
;;   * open-sse-stream — connect, send the GET, read PAST the status line and
;;     headers (writing the request + draining headers is what makes Dianoia
;;     register the sink), and hand back an open handle. Call this BEFORE the
;;     trigger.
;;   * read-sse-events — drain parsed events from an already-open handle, AFTER
;;     the trigger. Bounded by the handle's socket timeout.
;;
;; The handle is a plain map {:sock <Socket> :buf <StringBuilder>}, not a new
;; abstraction: just the live socket plus the byte buffer carried across the
;; header read into the body decode (any body bytes already read past the header
;; terminator live in :buf so the chunked decoder does not lose them). Loading
;; this file performs NO network I/O; only the run-* recipes touch the network.
;; ---------------------------------------------------------------------------

(defn open-sse-stream
  "Open the SSE event stream for `session-id` and return a handle.

  Connects a raw `java.net.Socket` to the host/port parsed from `base` (e.g.
  `http://127.0.0.1:8080`), issues `GET /v1/sessions/{id}/events` with an
  `Accept: text/event-stream` header, and reads PAST the status line and
  response headers — stopping at the blank line that begins the body. It does
  NOT read any event bodies; that is `read-sse-events`'s job.

  The split matters because writing the GET and draining its headers is what
  makes Dianoia register this session's SSE sink, and the watch dispatcher only
  delivers to sinks that exist when an event is emitted (no replay). So a caller
  must open the stream BEFORE triggering the change it wants to observe, then
  read AFTER — otherwise the push races ahead of the sink and is lost.

  `timeout` is the per-read socket timeout in milliseconds, set as SO_TIMEOUT on
  the socket so subsequent `read-sse-events` calls inherit it: a quiet stream
  raises `SocketTimeoutException` rather than blocking forever.

  Returns `{:sock <Socket> :buf <StringBuilder>}`. The caller must `.close` the
  `:sock` when done. If the server closes the connection before the header
  terminator arrives, the handle is still valid but its buffer is empty; the
  subsequent read sees EOF and yields no events."
  [base session-id timeout]
  (let [uri  (URI/create base)
        host (.getHost uri)
        port (let [p (.getPort uri)] (if (neg? p) 80 p))
        sock (Socket.)]
    (.connect sock (InetSocketAddress. host port) timeout)
    (.setSoTimeout sock timeout)
    (let [out (.getOutputStream sock)
          in  (.getInputStream sock)
          req (str "GET /v1/sessions/" session-id "/events HTTP/1.1\r\n"
                   "Host: " host ":" port "\r\n"
                   "Accept: text/event-stream\r\n"
                   "X-Lemma-Session: " session-id "\r\n"
                   "Connection: keep-alive\r\n\r\n")]
      (.write out (.getBytes req StandardCharsets/UTF_8))
      (.flush out)
      ;; Consume the status line and headers; the body starts after the blank
      ;; line. Anything read past it is retained in :buf so the chunked decoder
      ;; in read-sse-events does not lose those bytes. Draining the headers here
      ;; is the act that registers the server-side sink. We read in 4 KiB blocks
      ;; (UTF-8 in headers is plain ASCII, so a block split never cuts a glyph).
      (let [acc (StringBuilder.)
            buf (byte-array 4096)]
        (try
          (loop []
            (let [n (.read in buf)]
              (cond
                (neg? n)
                ;; Server closed before headers completed: hand back an empty
                ;; buffer; the read will see EOF and report no events.
                {:sock sock :buf (StringBuilder.)}

                :else
                (do
                  (.append acc (String. buf 0 n StandardCharsets/UTF_8))
                  (let [s (.toString acc)
                        i (.indexOf s "\r\n\r\n")]
                    (if (neg? i)
                      (recur)
                      {:sock sock :buf (StringBuilder. (subs s (+ i 4)))}))))))
          (catch SocketTimeoutException _
            ;; Quiet connection during the header read: still hand back a handle
            ;; so the caller's try/finally can close it uniformly.
            {:sock sock :buf (StringBuilder.)}))))))

(defn read-sse-events
  "Drain up to `max-events` parsed envelopes from an open `stream` handle.

  `stream` is the handle returned by `open-sse-stream` (its socket is live and
  its headers already consumed). This transfer-decodes the chunked body and
  parses Server-Sent Events out of it: each event's `data:` lines are
  concatenated and run through `edn/read-string` (with an unknown-tag `:default`
  of `tagged-literal`, so handles like `#watch` round-trip), and the parsed
  envelopes (typically :watch-event maps) are returned as a vector.

  A `SocketTimeoutException` (the SO_TIMEOUT set by open-sse-stream) or end of
  stream ends the read and returns whatever arrived so far, so a quiet stream
  degrades to an empty vector rather than hanging. A size-0 chunk is http-kit's
  header-flush keep-alive, NOT end-of-stream, so we skip it and keep reading; a
  genuine connection close (`.read` returns -1) ends the read. The caller owns
  the socket and closes it; this only reads."
  [stream max-events]
  (let [^Socket sock (:sock stream)
        in           (.getInputStream sock)
        ^StringBuilder buf (:buf stream)
        ;; Pull more bytes onto the decode buffer; -1 (EOF) throws to end the
        ;; loop. UTF-8 may straddle a block boundary, but the chunk framing and
        ;; SSE data here are ASCII delimiters around UTF-8 payloads that arrive
        ;; whole within a chunk body, so block-wise decode is safe in practice.
        read-more (fn []
                    (let [b (byte-array 4096)
                          n (.read in b)]
                      (when (neg? n) (throw (java.io.EOFException.)))
                      (.append buf (String. b 0 n StandardCharsets/UTF_8))))
        ;; One CRLF-delimited line (chunk-size lines).
        read-line* (fn []
                     (loop []
                       (let [s (.toString buf)
                             i (.indexOf s "\r\n")]
                         (if (neg? i)
                           (do (read-more) (recur))
                           (let [line (subs s 0 i)]
                             (.delete buf 0 (+ i 2))
                             line)))))
        ;; Exactly n bytes worth of buffered characters (chunk bodies).
        read-n* (fn [n]
                  (loop []
                    (let [s (.toString buf)]
                      (if (< (count s) n)
                        (do (read-more) (recur))
                        (let [out (subs s 0 n)]
                          (.delete buf 0 n)
                          out)))))]
    (let [events (transient [])
          text   (StringBuilder.)]  ; decoded body bytes awaiting SSE framing
      (try
        (loop []
          (when (< (count events) max-events)
            (let [size-line (str/trim (read-line*))]
              (if (= "" size-line)
                (recur)                       ; stray blank line between chunks
                (let [size (Integer/parseInt size-line 16)]
                  (if (zero? size)
                    (recur)                   ; header-flush keep-alive, not EOF
                    (do
                      (.append text (read-n* size))
                      (read-n* 2)             ; the CRLF that trails a chunk body
                      ;; An SSE event is the run of lines up to the next blank
                      ;; line. Concatenate its data: payloads (dropping
                      ;; :-comments) and parse the result as one EDN envelope.
                      (loop []
                        (let [s   (.toString text)
                              idx (.indexOf s "\n\n")]
                          (when (and (>= idx 0) (< (count events) max-events))
                            (let [block (subs s 0 idx)
                                  datas (->> (str/split-lines block)
                                             (filter #(str/starts-with? % "data:"))
                                             (map #(str/triml (subs % (count "data:")))))]
                              (.delete text 0 (+ idx 2))
                              (when (seq datas)
                                (conj! events
                                       (edn/read-string {:default tagged-literal}
                                                        (str/join "\n" datas))))
                              (recur)))))
                      (recur))))))))
        (catch SocketTimeoutException _ nil)  ; quiet period: return what we have
        (catch java.io.EOFException _ nil))   ; end of stream: same
      (persistent! events))))

;; ---------------------------------------------------------------------------
;; Runnable recipe:  the full Lemma round-trip
;;
;; A flat, linear retelling of the protocol's hello → use-world → propose →
;; assert → query sequence. Each step prints one human-readable line so a
;; reader can follow the wire conversation by running the file. After every
;; reply we check `:event`; an `:error` / `:rejected` is printed and the
;; sequence stops cleanly rather than crashing.
;; ---------------------------------------------------------------------------

(defn run-http
  "Walk the propose/assert/query round-trip against a Lemma server over HTTP.
  `base` is the server's base URL (e.g. `http://127.0.0.1:8080`).

  A connection-level failure (server down/refused) is the one thing the
  round-trip cannot recover from: post-edn re-throws it as an ex-info naming
  the base. We catch it here so the demo prints that actionable line and exits
  nonzero, rather than dumping a raw Java stack trace."
  [base]
  (try
    ;; 1. Anonymous hello. The welcome reply carries the new session id in the
    ;;    X-Lemma-Session response header, which post-edn surfaces for us.
    (let [{welcome :body sid :session} (post-edn "/v1/messages" (list 'hello) :base base)]
      (if (not= :welcome (:event welcome))
        (println (str "hello: expected :welcome, got " (pr-str (:event welcome))
                      " -- " (describe-failure welcome)))
        (do
          (println (str "hello -> :welcome  version=" (pr-str (:version welcome))
                        "  session=" sid
                        "  world=" (pr-str (:world welcome))))

          ;; 1a. Read the advertised capabilities and limits once, up front, so
          ;;     the rest of the round-trip can tailor itself to this server
          ;;     (SPEC §10). EDN parses the welcome straight into Clojure values,
          ;;     so info is just the parsed surface as a plain map.
          (let [info (read-welcome welcome)
                caps (->> info :capabilities (map #(subs (str %) 1)) sort (str/join ", "))]
            (println (str "server: caps={" caps "}"
                          " max-message-bytes=" (max-message-bytes info)))

          ;; 2. Every later call rides the same session, on the named endpoint.
          (let [named (fn [form]
                        (post-edn (str "/v1/sessions/" sid "/messages") form
                                  :session sid :base base))]

            ;; 3. Enter the world. (use-world #world "default")
            (let [{world :body} (named (list 'use-world (wrld "default")))]
              (if (failure? world)
                (println (str "use-world refused: " (describe-failure world)))

                (do
                  (println (str "use-world \"default\" -> " (pr-str (:event world))
                                "  world=" (pr-str (:world world))))

                  ;; 4. Propose a fact: morningstar is equivalent to venus. The
                  ;;    reply hands back a #proposal handle we feed to the assert.
                  (let [f (fct {:predicate 'equivalent
                                :subject   (ent "morningstar")
                                :object    (ent "venus")})
                        {p :body} (named (list 'propose f))]
                    (if (failure? p)
                      (println (str "propose refused: " (describe-failure p)))

                      (do
                        (println (str "propose (equivalent morningstar venus) -> "
                                      (pr-str (:event p))
                                      "  proposal=" (pr-str (:proposal p))))

                        ;; 5. Assert the proposed fact into the world.
                        (let [{a :body} (named (list 'assert (:proposal p)))]
                          (if (failure? a)
                            (println (str "assert refused: " (describe-failure a)))

                            (do
                              (println (str "assert proposal -> " (pr-str (:event a))))

                              ;; 6. Query it back. Note :find / :where are
                              ;;    VECTORS and the where-clause is a vector of
                              ;;    vectors; only the verb head is a list, and
                              ;;    the query variable ?o stays a quoted symbol.
                              (let [{q :body} (named (list 'query
                                                           {:find  '[?o]
                                                            :where [['equivalent (ent "morningstar") '?o]]}))]
                                (if (failure? q)
                                  (println (str "query refused: " (describe-failure q)))
                                  (do
                                    (println (str "query (equivalent morningstar ?o) -> rows="
                                                  (pr-str (:rows q))
                                                  "  done?=" (pr-str (:done? q))))

                                    ;; 7. Cursor pagination. Gated on the server
                                    ;;    advertising :lemma/cursor-pagination —
                                    ;;    without it, draining pages via
                                    ;;    (continue #cursor …) is unsupported, so
                                    ;;    we skip the whole block rather than
                                    ;;    guess. Seed three subset-of facts in one
                                    ;;    batched propose, assert the batch, then
                                    ;;    drain a :limit-2 query across pages.
                                    ;;    subset-of is a pure-EDB (stored-fact)
                                    ;;    predicate with stable (tx-id, ref-id)
                                    ;;    ordering, so it paginates; a rule-headed
                                    ;;    predicate like member-of cannot be the
                                    ;;    sole outer :where pattern (the server
                                    ;;    refuses it :bad-args
                                    ;;    :unsupported-rule-call-ordering).
                                    (if (supports? info :lemma/cursor-pagination)
                                      (let [f1 (fct {:predicate 'subset-of :subject (ent "sub-a") :object (ent "group")})
                                            f2 (fct {:predicate 'subset-of :subject (ent "sub-b") :object (ent "group")})
                                            f3 (fct {:predicate 'subset-of :subject (ent "sub-c") :object (ent "group")})
                                            propose-form (list 'propose f1 f2 f3)]
                                        ;; The batch propose is the largest
                                        ;; representative message we send, so it is
                                        ;; the one worth checking against
                                        ;; :max-message-bytes. A real client checks
                                        ;; every outbound message; this demo checks
                                        ;; this one.
                                        (if-not (within-message-limit? info (pr-str propose-form))
                                          (println "limit-exceeded: message exceeds max-message-bytes; skipping")
                                          (let [{p3 :body} (named propose-form)]
                                            (if (failure? p3)
                                              (println (str "propose (3x subset-of) refused: " (describe-failure p3)))
                                              (let [{a3 :body} (named (list 'assert (:proposal p3)))]
                                                (if (failure? a3)
                                                  (println (str "assert (3x subset-of) refused: " (describe-failure a3)))
                                                  (do
                                                    (println (str "propose (3x subset-of ? group) -> "
                                                                  (pr-str (:event p3))
                                                                  "  proposal=" (pr-str (:proposal p3))))
                                                    (println (str "assert proposal -> " (pr-str (:event a3))))

                                                    ;; query-all wants a form -> body
                                                    ;; closure; named returns a map, so
                                                    ;; we adapt it by taking :body.
                                                    (let [send (fn [form] (:body (named form)))
                                                          {:keys [rows pages failure]}
                                                          (query-all send
                                                                     (list 'query
                                                                           {:find  '[?x]
                                                                            :where [['subset-of '?x (ent "group")]]
                                                                            :limit 2}))]
                                                      (if failure
                                                        (println (str "paged query refused: " (describe-failure failure)))
                                                        (println (str "paged query (subset-of ? group), limit 2 -> "
                                                                      (count rows) " rows over " pages
                                                                      " page(s): " (pr-str rows))))))))))))
                                      (println "server does not advertise cursor pagination; skipping paged query"))

                                    ;; 8. Watch: register a standing pattern and
                                    ;;    observe a matching change pushed back on
                                    ;;    the SSE event stream. Gated on the server
                                    ;;    advertising :lemma/watch — without it the
                                    ;;    (watch-pattern …) verb is unsupported.
                                    (if (supports? info :lemma/watch)
                                      ;; (watch-pattern :pattern [[subset-of ?x #entity "group"]])
                                      ;; — the args are FLAT keyword args (the
                                      ;; :pattern keyword then the where-vector),
                                      ;; not a wrapping map. The reply hands back a
                                      ;; #watch handle to unwatch with.
                                      (let [pattern [['subset-of '?x (ent "group")]]
                                            {w :body} (named (list 'watch-pattern :pattern pattern))]
                                        (if (failure? w)
                                          (println (str "watch-pattern refused: " (describe-failure w)))
                                          (let [watch (:watch w)]
                                            (println (str "watch (subset-of ? group) -> " (pr-str (:event w))
                                                          "  watch=" (pr-str watch)))

                                            ;; Ordering is load-bearing: Dianoia
                                            ;; registers this session's SSE sink
                                            ;; lazily, when the GET /events headers
                                            ;; are written, and delivers a
                                            ;; :watch-event only to sinks present at
                                            ;; emit time (no backlog replay). So OPEN
                                            ;; the stream first (registering the
                                            ;; sink), THEN trigger the change, THEN
                                            ;; drain — otherwise the push can fire
                                            ;; within milliseconds of the assert,
                                            ;; before our sink exists, and be lost.
                                            (let [stream (open-sse-stream base sid 10000)]
                                              (try
                                                ;; The server pushes only DELTAS, so
                                                ;; the change must be new: a fact
                                                ;; re-asserted verbatim is a no-op and
                                                ;; fires nothing. We key the probe
                                                ;; entity to this process so each run
                                                ;; asserts a genuinely fresh fact.
                                                (let [probe (ent (str "watch-probe-" (.pid (java.lang.ProcessHandle/current))))
                                                      {wp :body} (named (list 'propose
                                                                              (fct {:predicate 'subset-of
                                                                                    :subject probe
                                                                                    :object  (ent "group")})))]
                                                  (cond
                                                    (failure? wp)
                                                    (println (str "watch-probe propose refused: " (describe-failure wp)))

                                                    :else
                                                    (let [{wa :body} (named (list 'assert (:proposal wp)))]
                                                      (if (failure? wa)
                                                        (println (str "watch-probe assert refused: " (describe-failure wa)))
                                                        (let [events (read-sse-events stream 1)]
                                                          (if (seq events)
                                                            (let [evt (first events)]
                                                              (println (str "watch (subset-of ? group) -> " (pr-str (:event evt))
                                                                            " type=" (pr-str (:type evt))
                                                                            " data=" (pr-str (:data evt)))))
                                                            (println "watch: no event observed before timeout")))))))
                                                (finally
                                                  ;; Release the SSE socket so the
                                                  ;; server drops the stream, whether
                                                  ;; or not an event arrived.
                                                  (.close ^Socket (:sock stream)))))

                                            ;; Tear the watch down. (unwatch #watch "w-N") -> :ok.
                                            (let [{u :body} (named (list 'unwatch watch))]
                                              (if (failure? u)
                                                (println (str "unwatch refused: " (describe-failure u)))
                                                (println (str "unwatch " (pr-str watch) " -> " (pr-str (:event u)))))))))
                                      (println "server does not advertise watch; skipping watch demo")))))))))))))))))))
    (catch clojure.lang.ExceptionInfo e
      (println (.getMessage e))
      (System/exit 1))))

;; ---------------------------------------------------------------------------
;; UDS transport:  EDN form  ->  length-prefixed frame  ->  parsed EDN response
;;
;; A second transport that speaks the same EDN codec over a Unix domain socket
;; instead of HTTP. Same "encode, send, decode" shape as `post-edn`, different
;; plumbing. Two things differ from the HTTP path:
;;
;;   * Framing. There is no HTTP envelope, so each message is delimited
;;     explicitly: a 4-byte BIG-ENDIAN length prefix followed by exactly that
;;     many UTF-8 bytes of EDN. This matches Dianoia's transport/uds.clj
;;     `write-frame` / `read-frame` exactly — a `DataOutputStream.writeInt` is a
;;     4-byte big-endian int, and `ByteBuffer.putInt` / `.getInt` default to
;;     big-endian, so the two agree on the wire without a byte-order dance.
;;   * Session binding. Over HTTP the client threads the session id back into
;;     each request header. Over UDS the server binds the session to the
;;     *connection* — it captures the id from the welcome envelope and pins it
;;     to the socket (see uds.clj handle-frame / build-ctx). So the client must
;;     NOT echo the session id into later frames; it just keeps sending on the
;;     same open channel and the server already knows who it is.
;;
;; `uds-send-frame` / `uds-recv-frame` are the channel I/O seam — pulled out as
;; their own `defn`s so tests can `with-redefs` them with a canned exchange and
;; exercise `run-uds` without a live socket. The channel is blocking, which
;; makes "read exactly N bytes" a straightforward loop on `.read`.
;; ---------------------------------------------------------------------------

(defn uds-send-frame
  "Frame `s` and write it to channel `ch`: a 4-byte big-endian length prefix,
  then the UTF-8 body bytes. A `SocketChannel` write may be partial, so we loop
  on each buffer until it is fully drained (`.hasRemaining`). Mirrors uds.clj
  `write-frame`."
  [^SocketChannel ch ^String s]
  (let [body (.getBytes s StandardCharsets/UTF_8)
        len  (doto (ByteBuffer/allocate 4)
               (.putInt (alength body))
               (.flip))
        buf  (ByteBuffer/wrap body)]
    (while (.hasRemaining len)
      (.write ch len))
    (while (.hasRemaining buf)
      (.write ch buf))))

(defn uds-recv-frame
  "Read one length-prefixed frame from channel `ch` and return its body as a
  String. Reads exactly 4 bytes for the big-endian length N, then exactly N
  body bytes, decoding UTF-8. `.read` may return fewer bytes than requested, so
  each phase loops until its buffer is full; a `-1` (EOF) before a buffer fills
  is a truncated frame, which we surface as an ex-info rather than a short read.
  Mirrors uds.clj `read-frame`."
  ^String [^SocketChannel ch]
  (let [read-fully (fn [^ByteBuffer buf]
                     (while (.hasRemaining buf)
                       (when (neg? (.read ch buf))
                         (throw (ex-info (str "connection closed with "
                                              (.remaining buf)
                                              " of " (.capacity buf)
                                              " bytes still expected")
                                         {:reason :eof}))))
                     buf)
        len-buf (.flip ^ByteBuffer (read-fully (ByteBuffer/allocate 4)))
        n       (.getInt len-buf)
        body    (byte-array n)]
    (read-fully (ByteBuffer/wrap body))
    (String. body StandardCharsets/UTF_8)))

;; ---------------------------------------------------------------------------
;; Watch over UDS:  demultiplex the :watch-event push out of the frame stream
;;
;; Over UDS there is no separate event channel: watch pushes interleave with
;; ordinary command responses on the SAME frame stream (uds.clj fans both onto
;; the one connection). So after triggering a change we read frames in a loop,
;; skipping command replies (the :asserted echo, etc.) until we see the
;; :watch-event envelope. `recv-fn` is a `(fn [] body)` that reads and parses
;; one frame — the same per-transport seam the round-trip already uses — so the
;; demux stays testable without a live socket. The loop is bounded by
;; `max-frames` and by the channel's blocking read (the caller sets no socket
;; timeout, but max-frames guarantees termination): a missing push returns nil,
;; which the caller reports as "no event observed".
;; ---------------------------------------------------------------------------

(defn uds-await-watch-event
  "Read framed replies via `recv-fn` until a :watch-event arrives; return it (or
  nil after `max-frames` frames). `recv-fn` is a `(fn [] body)` reading and
  parsing one UDS frame. Command replies (the :asserted echo, etc.) are skipped;
  the first map whose `:event` is `:watch-event` is returned. A read failure
  (EOF / closed connection) ends the loop early and yields nil."
  [recv-fn max-frames]
  (loop [n max-frames]
    (if (pos? n)
      (let [body (try (recv-fn) (catch clojure.lang.ExceptionInfo _ ::closed))]
        (cond
          (= ::closed body)                          nil
          (and (map? body)
               (= :watch-event (:event body)))       body
          :else                                      (recur (dec n))))
      nil)))

(defn run-uds
  "Run the same propose/assert/query round-trip over a Unix domain socket.

  Step for step this is `run-http` — hello, enter a world, propose a fact,
  assert it, query it back — but spoken over a UDS frame stream. The one
  protocol difference is session handling: the server binds the session to the
  connection from the welcome envelope (uds.clj handle-frame), so we do NOT
  thread the session id into later frames. Every call after hello simply rides
  the same open channel.

  Connecting to a missing socket (or one with no listener) throws an ex-info
  naming the path, so the failure is actionable rather than a bare Java
  exception. After each response we check `:event`; an `:error` / `:rejected`
  envelope is printed and the sequence stops cleanly. The channel is always
  closed in a `finally`."
  [socket-path]
  (let [ch (SocketChannel/open StandardProtocolFamily/UNIX)]
    (try
      (try
        (.connect ch (UnixDomainSocketAddress/of ^String socket-path))
        (catch java.io.IOException e
          ;; No listener at the path: the socket file is missing, or nothing is
          ;; accepting on it. Name the path so the failure points at what to fix.
          (let [cause (or (not-empty (.getMessage e))
                          (.. e getClass getSimpleName))]
            (throw (ex-info (str "could not connect to the Lemma UDS server at "
                                 socket-path " (" cause "); is the server running?")
                            {:socket-path socket-path} e)))))

      ;; One round-trip: frame out, frame in, decode. The session lives on the
      ;; connection — no id is echoed back, unlike the HTTP transport.
      (let [call (fn [form]
                   (uds-send-frame ch (pr-str form))
                   (edn/read-string {:default tagged-literal} (uds-recv-frame ch)))]

        ;; 1. Anonymous hello. The welcome reply carries the session id, which
        ;;    the server has already pinned to this connection for us.
        (let [welcome (call (list 'hello))]
          (if (not= :welcome (:event welcome))
            (println (str "hello: expected :welcome, got " (pr-str (:event welcome))
                          " -- " (describe-failure welcome)))
            (do
              (println (str "hello -> :welcome  version=" (pr-str (:version welcome))
                            "  session=" (pr-str (:session welcome))
                            "  world=" (pr-str (:world welcome))))

              ;; 1a. Read the advertised capabilities and limits once, up front,
              ;;     so the rest of the round-trip can tailor itself to this
              ;;     server (SPEC §10). Same parsed surface as the HTTP path.
              (let [info (read-welcome welcome)
                    caps (->> info :capabilities (map #(subs (str %) 1)) sort (str/join ", "))]
                (println (str "server: caps={" caps "}"
                              " max-message-bytes=" (max-message-bytes info)))

              ;; 2. Enter the world. (use-world #world "default")
              (let [world (call (list 'use-world (wrld "default")))]
                (if (failure? world)
                  (println (str "use-world refused: " (describe-failure world)))

                  (do
                    (println (str "use-world \"default\" -> " (pr-str (:event world))
                                  "  world=" (pr-str (:world world))))

                    ;; 3. Propose a fact: morningstar is equivalent to venus. The
                    ;;    reply hands back a #proposal handle we feed to the assert.
                    (let [f (fct {:predicate 'equivalent
                                  :subject   (ent "morningstar")
                                  :object    (ent "venus")})
                          p (call (list 'propose f))]
                      (if (failure? p)
                        (println (str "propose refused: " (describe-failure p)))

                        (do
                          (println (str "propose (equivalent morningstar venus) -> "
                                        (pr-str (:event p))
                                        "  proposal=" (pr-str (:proposal p))))

                          ;; 4. Assert the proposed fact into the world.
                          (let [a (call (list 'assert (:proposal p)))]
                            (if (failure? a)
                              (println (str "assert refused: " (describe-failure a)))

                              (do
                                (println (str "assert proposal -> " (pr-str (:event a))))

                                ;; 5. Query it back. As in the HTTP path, :find /
                                ;;    :where are VECTORS and the where-clause is a
                                ;;    vector of vectors; only the verb head is a
                                ;;    list, and ?o stays a quoted symbol.
                                (let [q (call (list 'query
                                                    {:find  '[?o]
                                                     :where [['equivalent (ent "morningstar") '?o]]}))]
                                  (if (failure? q)
                                    (println (str "query refused: " (describe-failure q)))
                                    (do
                                      (println (str "query (equivalent morningstar ?o) -> rows="
                                                    (pr-str (:rows q))
                                                    "  done?=" (pr-str (:done? q))))

                                      ;; 6. Cursor pagination, exactly as the
                                      ;;    HTTP path. Gated on the server
                                      ;;    advertising :lemma/cursor-pagination —
                                      ;;    without it, draining pages via
                                      ;;    (continue #cursor …) is unsupported, so
                                      ;;    we skip the whole block. Seed three
                                      ;;    subset-of facts in one batched propose,
                                      ;;    assert the batch, then drain a :limit-2
                                      ;;    query across pages. subset-of is a
                                      ;;    pure-EDB predicate (stable tx-id/ref-id
                                      ;;    ordering) so it paginates; member-of is
                                      ;;    rule-headed and cannot be the sole
                                      ;;    outer :where pattern (server refuses
                                      ;;    :bad-args :unsupported-rule-call-ordering).
                                      ;;    The UDS `call` closure is already
                                      ;;    form -> body, so query-all takes it directly.
                                      (if (supports? info :lemma/cursor-pagination)
                                        (let [f1 (fct {:predicate 'subset-of :subject (ent "sub-a") :object (ent "group")})
                                              f2 (fct {:predicate 'subset-of :subject (ent "sub-b") :object (ent "group")})
                                              f3 (fct {:predicate 'subset-of :subject (ent "sub-c") :object (ent "group")})
                                              propose-form (list 'propose f1 f2 f3)]
                                          ;; Check the batch propose — the largest
                                          ;; representative message — against the
                                          ;; server's :max-message-bytes before sending.
                                          (if-not (within-message-limit? info (pr-str propose-form))
                                            (println "limit-exceeded: message exceeds max-message-bytes; skipping")
                                            (let [p3 (call propose-form)]
                                              (if (failure? p3)
                                                (println (str "propose (3x subset-of) refused: " (describe-failure p3)))
                                                (let [a3 (call (list 'assert (:proposal p3)))]
                                                  (if (failure? a3)
                                                    (println (str "assert (3x subset-of) refused: " (describe-failure a3)))
                                                    (do
                                                      (println (str "propose (3x subset-of ? group) -> "
                                                                    (pr-str (:event p3))
                                                                    "  proposal=" (pr-str (:proposal p3))))
                                                      (println (str "assert proposal -> " (pr-str (:event a3))))

                                                      (let [{:keys [rows pages failure]}
                                                            (query-all call
                                                                       (list 'query
                                                                             {:find  '[?x]
                                                                              :where [['subset-of '?x (ent "group")]]
                                                                              :limit 2}))]
                                                        (if failure
                                                          (println (str "paged query refused: " (describe-failure failure)))
                                                          (println (str "paged query (subset-of ? group), limit 2 -> "
                                                                        (count rows) " rows over " pages
                                                                        " page(s): " (pr-str rows))))))))))))
                                        (println "server does not advertise cursor pagination; skipping paged query"))

                                      ;; 7. Watch over UDS. Same standing-pattern
                                      ;;    idea as the HTTP path, but the push has
                                      ;;    nowhere separate to go: it interleaves
                                      ;;    with command replies on this one socket.
                                      ;;    Gated on the server advertising
                                      ;;    :lemma/watch.
                                      (if (supports? info :lemma/watch)
                                        ;; (watch-pattern :pattern [[subset-of ?x #entity "group"]])
                                        ;; — flat keyword args, as on HTTP. The reply
                                        ;; carries the #watch handle.
                                        (let [pattern [['subset-of '?x (ent "group")]]
                                              w (call (list 'watch-pattern :pattern pattern))]
                                          (if (failure? w)
                                            (println (str "watch-pattern refused: " (describe-failure w)))
                                            (let [watch (:watch w)]
                                              (println (str "watch (subset-of ? group) -> " (pr-str (:event w))
                                                            "  watch=" (pr-str watch)))

                                              ;; Trigger a fresh delta (a verbatim
                                              ;; re-assert is a no-op and fires
                                              ;; nothing), keyed to this process so
                                              ;; each run is genuinely new. The
                                              ;; :asserted reply and the :watch-event
                                              ;; push both land on this socket; we
                                              ;; read the assert reply here, then
                                              ;; demux the push below.
                                              (let [probe (ent (str "watch-probe-" (.pid (java.lang.ProcessHandle/current))))
                                                    wp (call (list 'propose
                                                                   (fct {:predicate 'subset-of
                                                                         :subject probe
                                                                         :object  (ent "group")})))]
                                                (cond
                                                  (failure? wp)
                                                  (println (str "watch-probe propose refused: " (describe-failure wp)))

                                                  :else
                                                  (let [wa (call (list 'assert (:proposal wp)))]
                                                    (if (failure? wa)
                                                      (println (str "watch-probe assert refused: " (describe-failure wa)))
                                                      ;; Demux the push out of the
                                                      ;; frame stream: read frames,
                                                      ;; skipping command echoes, until
                                                      ;; the :watch-event arrives.
                                                      (let [recv-fn #(edn/read-string {:default tagged-literal}
                                                                                      (uds-recv-frame ch))
                                                            evt (uds-await-watch-event recv-fn 8)]
                                                        (if (some? evt)
                                                          (println (str "watch (subset-of ? group) -> " (pr-str (:event evt))
                                                                        " type=" (pr-str (:type evt))
                                                                        " data=" (pr-str (:data evt))))
                                                          (println "watch: no event observed before timeout")))))))

                                              ;; Tear the watch down. (unwatch #watch "w-N") -> :ok.
                                              (let [u (call (list 'unwatch watch))]
                                                (if (failure? u)
                                                  (println (str "unwatch refused: " (describe-failure u)))
                                                  (println (str "unwatch " (pr-str watch) " -> " (pr-str (:event u)))))))))
                                        (println "server does not advertise watch; skipping watch demo")))))))))))))))))))
      (finally
        (.close ch)))))

;; ---------------------------------------------------------------------------
;; Dispatcher
;;
;; `-main` routes the CLI argv to a transport. `uds [path]` runs the UDS
;; round-trip (defaulting to `default-socket`); a bare URL runs the HTTP
;; round-trip against it; no args runs HTTP against `default-base`. A
;; connection-level failure on the UDS path is re-thrown by `run-uds` as an
;; ex-info naming the socket — we catch it here, print the actionable line, and
;; exit nonzero (the HTTP path catches its own equivalent inside `run-http`).
;; ---------------------------------------------------------------------------

(defn -main
  "Route the CLI argv to a transport: `uds [path]` -> UDS round-trip;
  a base URL -> HTTP round-trip; no args -> HTTP against `default-base`."
  [& args]
  (if (= "uds" (first args))
    (try
      (run-uds (or (second args) default-socket))
      (catch clojure.lang.ExceptionInfo e
        (println (.getMessage e))
        (System/exit 1)))
    (run-http (or (first args) default-base))))
