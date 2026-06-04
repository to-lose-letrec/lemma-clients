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
  (:import (java.net URI)
           (java.net.http HttpClient
                          HttpRequest
                          HttpResponse$BodyHandlers
                          HttpRequest$BodyPublishers)))

;; Where a locally booted Dianoia HTTP listener lives by default (SPEC examples).
;; Override by passing a base URL as the first CLI argument.
(def default-base "http://127.0.0.1:8080")

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
;; Runnable recipe:  the full Lemma round-trip
;;
;; A flat, linear retelling of the protocol's hello → use-world → propose →
;; assert → query sequence. Each step prints one human-readable line so a
;; reader can follow the wire conversation by running the file. After every
;; reply we check `:event`; an `:error` / `:rejected` is printed and the
;; sequence stops cleanly rather than crashing.
;; ---------------------------------------------------------------------------

(defn -main
  "Walk the propose/assert/query round-trip against a Lemma server. The HTTP
  base is the first CLI arg, defaulting to `default-base`."
  [& args]
  (let [base (or (first args) default-base)]
   ;; A connection-level failure (server down/refused) is the one thing the
   ;; round-trip cannot recover from: post-edn re-throws it as an ex-info
   ;; naming the base. Catch it here so the demo prints that actionable line
   ;; and exits nonzero, rather than dumping a raw Java stack trace.
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
                                  (println (str "query (equivalent morningstar ?o) -> rows="
                                                (pr-str (:rows q))
                                                "  done?=" (pr-str (:done? q)))))))))))))))))))
    (catch clojure.lang.ExceptionInfo e
      (println (.getMessage e))
      (System/exit 1)))))
