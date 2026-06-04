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

                                    ;; 7. Cursor pagination. Seed three
                                    ;;    subset-of facts in one batched propose,
                                    ;;    assert the batch, then drain a
                                    ;;    :limit-2 query across pages. subset-of
                                    ;;    is a pure-EDB (stored-fact) predicate
                                    ;;    with stable (tx-id, ref-id) ordering, so
                                    ;;    it paginates; a rule-headed predicate
                                    ;;    like member-of cannot be the sole outer
                                    ;;    :where pattern (the server refuses it
                                    ;;    :bad-args :unsupported-rule-call-ordering).
                                    (let [f1 (fct {:predicate 'subset-of :subject (ent "sub-a") :object (ent "group")})
                                          f2 (fct {:predicate 'subset-of :subject (ent "sub-b") :object (ent "group")})
                                          f3 (fct {:predicate 'subset-of :subject (ent "sub-c") :object (ent "group")})
                                          {p3 :body} (named (list 'propose f1 f2 f3))]
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
                                                                " page(s): " (pr-str rows))))))))))))))))))))))))))
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
                                      ;;    HTTP path. Seed three subset-of facts
                                      ;;    in one batched propose, assert the
                                      ;;    batch, then drain a :limit-2 query
                                      ;;    across pages. subset-of is a pure-EDB
                                      ;;    predicate (stable tx-id/ref-id
                                      ;;    ordering) so it paginates; member-of
                                      ;;    is rule-headed and cannot be the sole
                                      ;;    outer :where pattern (server refuses
                                      ;;    :bad-args :unsupported-rule-call-ordering).
                                      ;;    The UDS `call` closure is already
                                      ;;    form -> body, so query-all takes it directly.
                                      (let [f1 (fct {:predicate 'subset-of :subject (ent "sub-a") :object (ent "group")})
                                            f2 (fct {:predicate 'subset-of :subject (ent "sub-b") :object (ent "group")})
                                            f3 (fct {:predicate 'subset-of :subject (ent "sub-c") :object (ent "group")})
                                            p3 (call (list 'propose f1 f2 f3))]
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
                                                                  " page(s): " (pr-str rows))))))))))))))))))))))))))
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
