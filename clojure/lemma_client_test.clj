;; lemma_client_test.clj — clojure.test coverage for the Clojure Lemma client.
;;
;; Run dependency-free:  cd clojure && clojure -M:test
;;
;; Every test rebinds the single I/O seam `lc/http-send` with `with-redefs` and
;; returns a *fake* java.net.http.HttpResponse — NO real network is touched.
;; The fake only needs to satisfy how `post-edn` reads a response: `.body`
;; (an EDN string) and `.headers` (a real HttpHeaders that answers
;; `.firstValue "x-lemma-session"`).

(ns lemma-client-test
  (:require [clojure.test :refer :all]
            [clojure.edn :as edn]
            [clojure.string :as str]
            [lemma-client :as lc])
  (:import (java.net.http HttpResponse HttpHeaders)
           (java.net UnixDomainSocketAddress StandardProtocolFamily
                     Socket ServerSocket InetAddress InetSocketAddress)
           (java.util.concurrent Flow$Subscriber Flow$Subscription)
           (java.util.concurrent.atomic AtomicReference)
           (java.nio ByteBuffer)
           (java.nio.channels ServerSocketChannel SocketChannel)
           (java.nio.charset StandardCharsets)
           (java.nio.file Files)
           (java.nio.file.attribute FileAttribute)
           (java.util List Map)
           (java.util.function BiPredicate)))

;; ---------------------------------------------------------------------------
;; Helpers: build a fake HttpResponse, and drain an outbound BodyPublisher.
;; ---------------------------------------------------------------------------

(defn ^HttpHeaders headers-with
  "Build a real HttpHeaders carrying the given (already-lowercased) header map
  of string -> string. HttpHeaders/of takes a Map<String,List<String>>."
  [m]
  (HttpHeaders/of
   (into {} (map (fn [[k v]] [k (List/of v)]) m))
   (reify BiPredicate (test [_ _ _] true))))

(defn fake-response
  "A reify of java.net.http.HttpResponse whose `.body` is `edn-str` and whose
  `.headers` answers `x-lemma-session` with `session` (nil => header absent).
  Only the methods post-edn actually calls are implemented; the rest throw."
  ([edn-str] (fake-response edn-str nil))
  ([edn-str session]
   (let [hdrs (headers-with (if session {"x-lemma-session" session} {}))]
     (reify HttpResponse
       (body [_] edn-str)
       (headers [_] hdrs)
       (statusCode [_] 200)
       (uri [_] (java.net.URI/create "http://test/"))
       (version [_] (java.net.http.HttpClient$Version/HTTP_1_1))))))

(defn publisher->string
  "Synchronously drain an HttpRequest.BodyPublisher into a String. The publisher
  built by post-edn (ofString) emits the whole payload as one ByteBuffer, so we
  subscribe, request everything, and concatenate what arrives."
  [publisher]
  (let [acc (StringBuilder.)
        done (java.util.concurrent.CountDownLatch. 1)]
    (.subscribe publisher
                (reify Flow$Subscriber
                  (onSubscribe [_ s] (.request ^Flow$Subscription s Long/MAX_VALUE))
                  (onNext [_ bb]
                    (.append acc (.decode StandardCharsets/UTF_8 ^java.nio.ByteBuffer bb)))
                  (onError [_ _] (.countDown done))
                  (onComplete [_] (.countDown done))))
    (.await done 5 java.util.concurrent.TimeUnit/SECONDS)
    (.toString acc)))

(defn capturing-send
  "Returns [atom-of-requests send-fn]. The send-fn records each HttpRequest it
  is handed and returns successive responses from `responses` (last repeats)."
  [responses]
  (let [reqs (atom [])
        remaining (atom responses)
        send-fn (fn [req]
                  (swap! reqs conj req)
                  (let [[r & more] @remaining]
                    (when (seq more) (reset! remaining more))
                    r))]
    [reqs send-fn]))

;; ===========================================================================
;; (A) post-edn — request construction + response parsing
;; ===========================================================================

(deftest post-edn-builds-post-request-to-base-plus-path
  (let [[reqs send] (capturing-send [(fake-response "{:event :welcome}")])]
    (with-redefs [lc/http-send send]
      (lc/post-edn "/v1/messages" (list 'hello) :base "http://example:9999"))
    (let [req (first @reqs)]
      (is (= "http://example:9999/v1/messages" (str (.uri req)))
          "URI is base + path")
      (is (= "POST" (.method req)) "method is POST"))))

(deftest post-edn-sets-content-type-application-edn
  (let [[reqs send] (capturing-send [(fake-response "{:event :welcome}")])]
    (with-redefs [lc/http-send send]
      (lc/post-edn "/p" (list 'hello) :base "http://h"))
    (let [hdrs (.headers (first @reqs))]
      (is (= "application/edn"
             (-> hdrs (.firstValue "content-type") (.orElse nil)))))))

(deftest post-edn-body-carries-pr-str-of-the-form
  (let [[reqs send] (capturing-send [(fake-response "{:event :welcome}")])
        form (list 'use-world (lc/wrld "default"))]
    (with-redefs [lc/http-send send]
      (lc/post-edn "/p" form :base "http://h"))
    (let [body (publisher->string (.get (.bodyPublisher (first @reqs))))]
      (is (= (pr-str form) body)
          "request body is exactly pr-str of the form")
      (is (= "(use-world #world \"default\")" body)
          "and renders the verb LIST with the #world tag literal"))))

(deftest post-edn-omits-session-header-when-no-session
  (let [[reqs send] (capturing-send [(fake-response "{:event :welcome}")])]
    (with-redefs [lc/http-send send]
      (lc/post-edn "/p" (list 'hello) :base "http://h"))
    (let [hdrs (.headers (first @reqs))]
      (is (false? (-> hdrs (.firstValue "x-lemma-session") (.isPresent)))
          "x-lemma-session is absent when no :session is passed"))))

(deftest post-edn-sets-session-header-when-session-passed
  (let [[reqs send] (capturing-send [(fake-response "{:event :ok}")])]
    (with-redefs [lc/http-send send]
      (lc/post-edn "/p" (list 'hello) :base "http://h" :session "sess-42"))
    (let [hdrs (.headers (first @reqs))]
      (is (= "sess-42"
             (-> hdrs (.firstValue "x-lemma-session") (.orElse nil)))
          "x-lemma-session echoes the passed session"))))

(deftest post-edn-returns-parsed-body-and-response-session
  (let [[_ send] (capturing-send
                  [(fake-response "{:event :welcome :version \"1.0\"}" "sid-7")])]
    (with-redefs [lc/http-send send]
      (let [{:keys [body session]} (lc/post-edn "/p" (list 'hello) :base "http://h")]
        (is (= {:event :welcome :version "1.0"} body)
            "body is the parsed EDN map")
        (is (= "sid-7" session)
            "session is the x-lemma-session RESPONSE header")))))

(deftest post-edn-session-nil-when-response-header-absent
  (let [[_ send] (capturing-send [(fake-response "{:event :welcome}")])]
    (with-redefs [lc/http-send send]
      (is (nil? (:session (lc/post-edn "/p" (list 'hello) :base "http://h")))
          "session is nil when the response omits the header"))))

(deftest post-edn-round-trips-unknown-tagged-literal-in-body
  ;; The server hands back a #proposal handle the client has never registered.
  ;; post-edn parses with {:default tagged-literal}, so it survives as a
  ;; tagged-literal value and re-renders identically under pr-str.
  (let [[_ send] (capturing-send
                  [(fake-response "{:event :proposed :proposal #proposal \"p-99\"}")])]
    (with-redefs [lc/http-send send]
      (let [{:keys [body]} (lc/post-edn "/p" (list 'propose) :base "http://h")
            prop (:proposal body)]
        (is (= clojure.lang.TaggedLiteral (class prop))
            "#proposal parses to a tagged-literal, not an error")
        (is (= 'proposal (:tag prop)) "tag is preserved")
        (is (= "p-99" (:form prop)) "payload is preserved")
        (is (= "#proposal \"p-99\"" (pr-str prop))
            "and renders back onto the wire unchanged")))))

;; ===========================================================================
;; (B) handshake — the full -main round-trip over canned responses
;; ===========================================================================

(def welcome-edn "{:event :welcome :version \"1.0\" :world #world \"default\" :capabilities #{:lemma/v1 :lemma/cursor-pagination :lemma/watch} :limits {:max-message-bytes 1048576}}")
(def world-edn   "{:event :world-selected :world #world \"default\"}")
(def proposed-edn "{:event :proposed :proposal #proposal \"p-1\"}")
(def asserted-edn "{:event :asserted}")
(def result-edn  "{:event :result :rows [[#entity \"venus\"]] :done? true}")
;; The cursor-pagination tail (SPEC §8): after the single-result query, the
;; round-trip seeds three subset-of facts in one batched propose, asserts the
;; batch, then drains a :limit-2 query across TWO pages. Page 1 comes back
;; :done? false with a #cursor; (continue #cursor) yields page 2 :done? true.
(def proposed-batch-edn "{:event :proposed :proposal #proposal \"p-2\"}")
(def page1-edn "{:event :result :rows [[#entity \"sub-a\"] [#entity \"sub-b\"]] :done? false :cursor #cursor \"c-1\"}")
(def page2-edn "{:event :result :rows [[#entity \"sub-c\"]] :done? true}")
;; The watch tail (SPEC §9): after the paged query, the round-trip — gated on
;; the welcome advertising :lemma/watch — registers a standing (watch-pattern …),
;; opens the SSE stream, proposes+asserts a probe delta to trigger a push, drains
;; the :watch-event off the (mocked) SSE seam, then unwatches. Over HTTP the
;; watch-pattern reply carries a #watch handle; the probe propose/assert reply
;; like any other; unwatch replies :ok. The SSE open/read are mocked (see
;; mock-sse-* below) so no socket is touched.
(def watch-established-edn "{:event :watch-established :watch #watch \"w-1\"}")
(def watch-probe-proposed-edn "{:event :proposed :proposal #proposal \"p-watch\"}")
(def watch-probe-asserted-edn "{:event :asserted}")
(def unwatch-ok-edn "{:event :ok}")

;; The full 8-send HTTP round-trip + the trailing (continue #cursor) + the 4
;; watch-tail sends (watch-pattern, probe propose, probe assert, unwatch) = 13
;; canned replies: hello, use-world, propose, assert, query (single page),
;; propose(batch), assert, query (page 1 :done? false), continue (page 2 :done?
;; true), watch-pattern, watch-probe propose, watch-probe assert, unwatch.
(def main-handshake-edns
  [welcome-edn world-edn proposed-edn asserted-edn result-edn
   proposed-batch-edn asserted-edn page1-edn page2-edn
   watch-established-edn watch-probe-proposed-edn watch-probe-asserted-edn unwatch-ok-edn])

(defn main-handshake-responses
  "The 13 canned HttpResponses for the full paged+watch round-trip; the welcome
  carries `session` so the named-endpoint URIs can be asserted."
  [session]
  (into [(fake-response welcome-edn session)]
        (map fake-response (rest main-handshake-edns))))

;; A canned :watch-event envelope, shaped like the push Dianoia delivers on the
;; SSE stream when the watch's pattern matches the probe delta. The mocked
;; read-sse-events hands this back so run-http's watch step never touches a
;; socket — the welcome advertises :lemma/watch but no real SSE flows.
(def canned-watch-event
  {:event :watch-event
   :type  :asserted
   :data  (tagged-literal 'fact {:predicate 'subset-of
                                  :subject   (tagged-literal 'entity "watch-probe-1")
                                  :object    (tagged-literal 'entity "group")})})

(defn mock-open-sse-stream
  "A stand-in for open-sse-stream that returns a canned handle WITHOUT opening a
  socket. The handle carries a fresh UNCONNECTED java.net.Socket in :sock so
  run-http's type-hinted `(.close ^Socket (:sock stream))` in the finally is a
  harmless no-op (closing an unconnected socket neither connects nor throws)."
  [_base _session-id _timeout]
  {:sock (java.net.Socket.)
   :buf  (StringBuilder.)})

(defn mock-read-sse-events
  "A stand-in for read-sse-events that returns the canned :watch-event without
  reading any bytes — bounded by `max-events` exactly as the real reader is."
  [_stream max-events]
  (vec (take max-events [canned-watch-event])))

(deftest main-walks-full-roundtrip-to-result-without-throwing
  (let [[reqs send] (capturing-send (main-handshake-responses "sid-MAIN"))
        out (with-redefs [lc/http-send send
                          lc/open-sse-stream mock-open-sse-stream
                          lc/read-sse-events mock-read-sse-events]
              (with-out-str (lc/-main)))]
    (is (re-find #"hello -> :welcome" out) "prints the welcome line")
    (is (re-find #"query .* -> rows=" out)
        "reaches the final query/result line")
    (is (re-find #"paged query .* 3 rows over 2 page\(s\)" out)
        "drains the limit-2 query across two pages")
    (is (re-find #"watch .* -> :watch-event type=:asserted" out)
        "reaches the watch step and reports the pushed :watch-event")
    (is (re-find #"unwatch .* -> :ok" out)
        "and tears the watch down after observing the event")
    (is (not (re-find #"refused" out)) "no step is reported as refused")
    ;; The welcome-header session must flow into every later endpoint URI.
    (let [uris (map #(str (.uri %)) @reqs)]
      (is (= 13 (count uris))
          "all round-trip steps fire: 5 pre-pagination, the paged propose/assert/query/continue, and the 4 watch-tail sends")
      (is (= (str lc/default-base "/v1/messages") (first uris))
          "first call is the anonymous hello on /v1/messages")
      (is (every? #(re-find #"/v1/sessions/sid-MAIN/messages" %) (rest uris))
          "the welcome session id threads into the named-endpoint URIs"))))

(deftest main-threads-proposal-handle-from-propose-into-assert
  ;; Step 4 returns #proposal "p-1"; step 5 (assert) must carry that exact
  ;; tagged literal back as its verb argument.
  (let [[reqs send] (capturing-send (main-handshake-responses "sid-X"))]
    (with-redefs [lc/http-send send
                  lc/open-sse-stream mock-open-sse-stream
                  lc/read-sse-events mock-read-sse-events]
      (with-out-str (lc/-main)))
    ;; The 4th request (index 3) is the assert. Its body should be
    ;; (assert #proposal "p-1") — the proposal handle from step 3.
    (let [assert-body (publisher->string (.get (.bodyPublisher (nth @reqs 3))))]
      (is (= "(assert #proposal \"p-1\")" assert-body)
          "the #proposal from propose threads verbatim into the assert verb"))))

(deftest main-sets-session-header-on-named-calls
  (let [[reqs send] (capturing-send (main-handshake-responses "sid-H"))]
    (with-redefs [lc/http-send send
                  lc/open-sse-stream mock-open-sse-stream
                  lc/read-sse-events mock-read-sse-events]
      (with-out-str (lc/-main)))
    ;; The hello (req 0) carries no session header; the named calls all do.
    (let [hdr (fn [i] (-> (.headers (nth @reqs i))
                          (.firstValue "x-lemma-session") (.orElse nil)))]
      (is (nil? (hdr 0)) "hello is anonymous")
      (is (= "sid-H" (hdr 1)) "use-world rides the session")
      (is (= "sid-H" (hdr 4)) "query rides the same session")
      (is (= "sid-H" (hdr 8)) "the (continue #cursor) page-2 call rides it too"))))

(deftest main-stops-cleanly-on-error-envelope
  ;; A non-:welcome first reply must NOT throw; -main prints and stops.
  (let [[reqs send] (capturing-send
                     [(fake-response "{:event :error :reason :nope}")])
        out (with-redefs [lc/http-send send]
              (with-out-str (lc/-main)))]
    (is (re-find #"expected :welcome" out)
        "an error at hello is reported, not thrown")
    (is (= 1 (count @reqs)) "and the sequence stops after the failed hello")))

;; ===========================================================================
;; (B2) query-all — cursor pagination drained with an in-memory scripted `send`.
;;
;; query-all takes a `(fn [form] body)` closure and walks (continue #cursor …)
;; until :done?. We drive it with a pure in-memory script — an atom of canned
;; reply BODIES (parsed the same way post-edn parses the wire: {:default
;; tagged-literal}, so #entity / #cursor survive as tagged literals) — recording
;; every form sent. NO network, NO channel, fully deterministic.
;; ===========================================================================

(defn read-body
  "Parse an EDN reply body string exactly as post-edn / run-uds do on the wire:
  unknown tags (#entity, #cursor, …) fall through to tagged-literal."
  [s]
  (edn/read-string {:default tagged-literal} s))

(defn scripted-send
  "Return `[sent-atom send-fn]`. `send-fn` is a `(fn [form] body)` over an atom
  of canned reply bodies: it records each form it is handed into `sent-atom`
  (in order) and returns the next body from `bodies` (the last repeats if the
  loop over-calls — though a correct drain never does). This is the exact shape
  query-all expects, with zero I/O."
  [bodies]
  (let [sent      (atom [])
        remaining (atom bodies)
        send-fn   (fn [form]
                    (swap! sent conj form)
                    (let [[b & more] @remaining]
                      (when (seq more) (reset! remaining more))
                      b))]
    [sent send-fn]))

;; --- (A) multi-page: two pages drained to a flat, ordered row set ----------

(deftest query-all-drains-two-pages-into-flat-ordered-rows
  (let [bodies [(read-body "{:event :result :rows [[#entity \"a\"] [#entity \"b\"]] :done? false :cursor #cursor \"c-1\"}")
                (read-body "{:event :result :rows [[#entity \"c\"]] :done? true}")]
        [_ send] (scripted-send bodies)
        {:keys [rows pages failure]} (lc/query-all send (list 'query {:limit 2}))]
    (is (= [[(lc/ent "a")] [(lc/ent "b")] [(lc/ent "c")]] rows)
        "all three rows are returned, page 1 then page 2, in order")
    (is (= 2 pages) "two pages were drained")
    (is (nil? failure) "a fully-drained query reports no failure")))

(deftest query-all-second-form-is-continue-of-the-page1-cursor
  (let [cursor (read-body "#cursor \"c-1\"")
        bodies [(read-body "{:event :result :rows [[#entity \"a\"] [#entity \"b\"]] :done? false :cursor #cursor \"c-1\"}")
                (read-body "{:event :result :rows [[#entity \"c\"]] :done? true}")]
        [sent send] (scripted-send bodies)
        query (list 'query {:limit 2})]
    (lc/query-all send query)
    (is (= query (first @sent)) "the first form sent is the original query")
    (is (= (list 'continue cursor) (second @sent))
        "the second form is (continue #cursor \"c-1\") — the page-1 cursor verbatim")
    (is (= 2 (count @sent)) "exactly two forms are sent for a two-page drain")))

;; --- (B) single page: :done? true on page 1, no #cursor present ------------

(deftest query-all-single-page-done-true-needs-no-continue
  (let [bodies [(read-body "{:event :result :rows [[#entity \"only\"]] :done? true}")]
        [sent send] (scripted-send bodies)
        {:keys [rows pages failure]} (lc/query-all send (list 'query {}))]
    (is (= [[(lc/ent "only")]] rows) "the single page's one row is returned")
    (is (= 1 pages) "exactly one page")
    (is (nil? failure) "no failure")
    (is (= 1 (count @sent))
        "send is called once — no (continue …) when page 1 is already :done?")))

(deftest query-all-single-page-tolerates-missing-cursor
  ;; The server omits :cursor on an already-done result; query-all must not read
  ;; it (it only touches the cursor inside the not-:done? branch). A :done? page
  ;; with NO :cursor key must drain cleanly without throwing.
  (let [bodies [(read-body "{:event :result :rows [[#entity \"x\"]] :done? true}")]
        [_ send] (scripted-send bodies)
        result (lc/query-all send (list 'query {}))]
    (is (= 1 (:pages result))
        "a done page without a #cursor key drains in one page, no NPE")))

;; --- (C) failure propagation: mid-drain error and initial error ------------

(deftest query-all-propagates-continue-failure-with-rows-so-far
  ;; Page 1 is :done? false with a cursor; the (continue …) comes back an error
  ;; (e.g. an expired cursor :unknown-handle). query-all must NOT throw: it
  ;; returns the rows gathered so far plus the error body as :failure.
  (let [err (read-body "{:event :error :reason :unknown-handle}")
        bodies [(read-body "{:event :result :rows [[#entity \"a\"] [#entity \"b\"]] :done? false :cursor #cursor \"c-1\"}")
                err]
        [_ send] (scripted-send bodies)
        {:keys [rows pages failure]} (lc/query-all send (list 'query {:limit 2}))]
    (is (= [[(lc/ent "a")] [(lc/ent "b")]] rows)
        "the rows gathered before the failure are still returned")
    (is (= 1 pages) "only the first page was fully drained")
    (is (= err failure)
        "the refusal body is surfaced verbatim as :failure")))

(deftest query-all-initial-failure-returns-empty-rows-zero-pages
  ;; When the very first query reply is a refusal, there are no rows and no
  ;; pages — :failure is that body and the loop never runs.
  (let [err (read-body "{:event :error :reason :bad-args}")
        [sent send] (scripted-send [err])
        {:keys [rows pages failure]} (lc/query-all send (list 'query {:bad :form}))]
    (is (= [] rows) "no rows when the opening query is refused")
    (is (= 0 pages) "zero pages — the drain loop never entered")
    (is (= err failure) "the opening refusal body is the :failure")
    (is (= 1 (count @sent)) "and no (continue …) is attempted after it")))

;; ===========================================================================
;; (C) EDN sanity — tag rendering + round-trip
;; ===========================================================================

(deftest wrld-renders-as-world-tagged-literal-inside-a-verb-list
  (is (= "(use-world #world \"default\")"
         (pr-str (list 'use-world (lc/wrld "default"))))))

(deftest ent-renders-as-entity-tagged-literal
  (is (= "#entity \"morningstar\"" (pr-str (lc/ent "morningstar")))))

(deftest fct-renders-as-fact-tagged-literal
  (is (= "#fact {:predicate equivalent}"
         (pr-str (lc/fct {:predicate 'equivalent})))))

(deftest edn-round-trips-a-tagged-literal-form
  (let [form (list 'propose (lc/fct {:predicate 'equivalent
                                     :subject (lc/ent "morningstar")
                                     :object  (lc/ent "venus")}))
        text (pr-str form)
        back (edn/read-string {:default tagged-literal} text)]
    (is (= text (pr-str back))
        "pr-str -> edn/read-string -> pr-str is identity for tagged forms")))

;; ===========================================================================
;; (D) UDS framing — uds-send-frame / uds-recv-frame over a real in-process
;; UNIX socketpair. No HTTP, no external server: a ServerSocketChannel bound to
;; a temp path + a connected client SocketChannel give us both ends of one
;; blocking connection, so the real framing code (length prefix, partial-read
;; loops, UTF-8) runs end to end. The temp socket file and all channels are
;; torn down in a fixture-style `finally`.
;; ===========================================================================

(defn no-attrs
  "An empty FileAttribute[] for the varargs of Files/createTempDirectory."
  []
  (make-array FileAttribute 0))

(defn with-socketpair
  "Open a connected UNIX SocketChannel pair in-process and call
  `(f client-channel accepted-channel)`. Tears down both channels, the server
  channel, the temp socket file, and the temp dir in a `finally` — so a test
  failure never leaks a socket file. Returns whatever `f` returns."
  [f]
  (let [dir  (Files/createTempDirectory "lemma-uds-test" (no-attrs))
        path (.resolve dir "t.sock")
        addr (UnixDomainSocketAddress/of path)
        srv  (doto (ServerSocketChannel/open StandardProtocolFamily/UNIX)
               (.bind addr))
        cli  (SocketChannel/open StandardProtocolFamily/UNIX)]
    (try
      (.connect cli addr)
      (let [acc (.accept srv)]
        (try
          (f cli acc)
          (finally (.close acc))))
      (finally
        (.close cli)
        (.close srv)
        (Files/deleteIfExists path)
        (Files/deleteIfExists dir)))))

(defn read-raw-frame
  "Read one length-prefixed frame straight off `ch` WITHOUT using the code under
  test: pull exactly 4 bytes, interpret them as a big-endian int N, then pull
  exactly N bytes. Returns `{:len N :body <string> :len-bytes [b0 b1 b2 b3]}` so
  a test can assert the wire layout independently of uds-recv-frame."
  [^SocketChannel ch]
  (let [read-fully (fn [^ByteBuffer buf]
                     (while (.hasRemaining buf)
                       (when (neg? (.read ch buf))
                         (throw (ex-info "eof" {}))))
                     buf)
        len-buf (.flip ^ByteBuffer (read-fully (ByteBuffer/allocate 4)))
        len-bytes (let [a (byte-array 4)] (.get (.duplicate len-buf) a) (vec a))
        n       (.getInt len-buf)
        body    (byte-array n)]
    (read-fully (ByteBuffer/wrap body))
    {:len n
     :len-bytes len-bytes
     :body (String. body StandardCharsets/UTF_8)}))

(deftest uds-send-frame-writes-4-byte-big-endian-length-then-body
  (with-socketpair
    (fn [cli acc]
      (let [s "(hello)"]
        (lc/uds-send-frame cli s)
        (let [{:keys [len len-bytes body]} (read-raw-frame acc)]
          (is (= (alength (.getBytes s StandardCharsets/UTF_8)) len)
              "the 4-byte prefix is the UTF-8 body byte count")
          (is (= [0 0 0 7] len-bytes)
              "the prefix is BIG-ENDIAN (most-significant byte first)")
          (is (= s body)
              "the body bytes follow the prefix verbatim"))))))

(deftest uds-send-frame-length-counts-utf8-bytes-not-chars
  ;; "héllo" is 5 characters but 6 UTF-8 bytes (é encodes as 2 bytes). The frame
  ;; length must be the byte count, or a multibyte body desyncs the stream.
  (with-socketpair
    (fn [cli acc]
      (let [s "(say \"héllo\")"]
        (lc/uds-send-frame cli s)
        (let [{:keys [len body]} (read-raw-frame acc)]
          (is (= (alength (.getBytes s StandardCharsets/UTF_8)) len)
              "length is the UTF-8 byte count")
          (is (not= (count s) len)
              "and that byte count differs from the character count here")
          (is (= s body)
              "the multibyte body round-trips byte-for-byte"))))))

(deftest uds-recv-frame-reconstructs-the-original-edn-string
  (with-socketpair
    (fn [cli acc]
      (let [s "(propose #fact {:predicate equivalent})"]
        ;; Write from the accepted end; read with the code under test on cli.
        (lc/uds-send-frame acc s)
        (is (= s (lc/uds-recv-frame cli))
            "uds-recv-frame returns exactly the string uds-send-frame framed")))))

(deftest uds-recv-frame-reconstructs-multibyte-utf8-body
  (with-socketpair
    (fn [cli acc]
      (let [s "(query {:find [?o] :note \"café→venus ☿\"})"]
        (lc/uds-send-frame acc s)
        (is (= s (lc/uds-recv-frame cli))
            "a body with multibyte UTF-8 (counted in bytes) decodes back intact")))))

(deftest uds-send-recv-round-trip-is-identity-on-the-same-pair
  ;; Full round-trip across the real socket: frame out on one end, recv on the
  ;; other, for several distinct forms in sequence. Exercises the length-then-body
  ;; loops repeatedly on one open connection (frames must not bleed into each
  ;; other), which also naturally drives partial reads.
  (with-socketpair
    (fn [cli acc]
      (let [forms ["(hello)"
                   "(use-world #world \"default\")"
                   "#world \"a-fairly-long-world-name-to-push-past-tiny-buffers\""
                   "(say \"unicode: ☿ ♀ → 日本語\")"]]
        (doseq [s forms]
          (lc/uds-send-frame cli s)
          (is (= s (lc/uds-recv-frame acc))
              (str "round-trip identity for " (pr-str s))))))))

(deftest uds-recv-frame-throws-actionable-error-on-truncated-frame
  ;; A length prefix promising N bytes followed by a premature EOF must surface
  ;; as an ex-info (:reason :eof), not a silent short read.
  (with-socketpair
    (fn [cli acc]
      ;; Hand-write a prefix claiming 10 bytes, send only 3, then close the
      ;; writing end so the reader hits EOF mid-body.
      (let [len (doto (ByteBuffer/allocate 4) (.putInt 10) (.flip))]
        (while (.hasRemaining len) (.write acc len)))
      (let [body (ByteBuffer/wrap (.getBytes "abc" StandardCharsets/UTF_8))]
        (while (.hasRemaining body) (.write acc body)))
      (.close acc)
      (let [ex (try (lc/uds-recv-frame cli) nil
                    (catch clojure.lang.ExceptionInfo e e))]
        (is (some? ex) "a truncated frame throws rather than returning a short read")
        (is (= :eof (:reason (ex-data ex)))
            "the failure is the :eof ex-info, not a generic exception")))))

;; ===========================================================================
;; (E) run-uds handshake — the full propose/assert/query sequence over UDS.
;;
;; run-uds opens a real SocketChannel and `.connect`s BEFORE it touches the
;; uds-send-frame/uds-recv-frame seams. We do NOT stub the channel open (it is
;; static interop); instead we stand up a real in-process UNIX listener at a
;; temp path so the connect succeeds harmlessly, then with-redefs both frame
;; seams to capture outbound EDN and feed canned inbound EDN. No real bytes flow
;; through the channel — the seams short-circuit it — but the connect/close
;; lifecycle (including the `finally` .close) runs for real.
;; ===========================================================================

(def uds-welcome-edn  "{:event :welcome :version \"1.0\" :session #session \"sess-uds\" :world #world \"default\" :capabilities #{:lemma/v1 :lemma/cursor-pagination :lemma/watch} :limits {:max-message-bytes 1048576}}")
(def uds-world-edn    "{:event :world-selected :world #world \"default\"}")
(def uds-proposed-edn "{:event :proposed :proposal #proposal \"p-uds\"}")
(def uds-asserted-edn "{:event :asserted}")
(def uds-result-edn   "{:event :result :rows [[#entity \"venus\"]] :done? true}")
;; Same cursor-pagination tail as the HTTP path: batched propose/assert, then a
;; :limit-2 query drained over two pages via (continue #cursor "c-uds").
(def uds-proposed-batch-edn "{:event :proposed :proposal #proposal \"p-uds-2\"}")
(def uds-page1-edn "{:event :result :rows [[#entity \"sub-a\"] [#entity \"sub-b\"]] :done? false :cursor #cursor \"c-uds\"}")
(def uds-page2-edn "{:event :result :rows [[#entity \"sub-c\"]] :done? true}")
;; The watch tail over UDS (SPEC §9): watch-pattern (reply carries a #watch),
;; the probe propose/assert, then — interleaved on the SAME frame stream — the
;; :watch-event push, then the unwatch :ok. uds-await-watch-event reads frames
;; until it sees the :watch-event, skipping the command echoes; here the push
;; lands immediately after the probe assert, so no echo precedes it (the
;; standalone (B) test below exercises the skip-an-echo path explicitly).
(def uds-watch-established-edn "{:event :watch-established :watch #watch \"w-uds\"}")
(def uds-watch-probe-proposed-edn "{:event :proposed :proposal #proposal \"p-watch-uds\"}")
(def uds-watch-probe-asserted-edn "{:event :asserted}")
(def uds-watch-event-edn "{:event :watch-event :type :asserted :data #fact {:predicate subset-of :subject #entity \"watch-probe-uds\" :object #entity \"group\"}}")
(def uds-unwatch-ok-edn "{:event :ok}")

(defn with-uds-listener
  "Stand up a real UNIX ServerSocketChannel at a temp path and call `(f path)`.
  The listener exists only so run-uds's real `.connect` succeeds; we never read
  or write it (the frame seams are redefined by the caller). A background thread
  accepts the one inbound connection so `.connect` does not block. Everything is
  torn down in a `finally`."
  [f]
  (let [dir  (Files/createTempDirectory "lemma-uds-run" (no-attrs))
        path (.resolve dir "t.sock")
        addr (UnixDomainSocketAddress/of path)
        srv  (doto (ServerSocketChannel/open StandardProtocolFamily/UNIX)
               (.bind addr))
        accepted (atom nil)
        t   (doto (Thread. #(try (reset! accepted (.accept srv))
                                 (catch Throwable _ nil)))
              (.setDaemon true)
              (.start))]
    (try
      (f (str path))
      (finally
        (when-let [a @accepted] (.close a))
        (.close srv)
        (.interrupt t)
        (Files/deleteIfExists path)
        (Files/deleteIfExists dir)))))

(defn canned-recv
  "Returns a uds-recv-frame stand-in that yields successive strings from
  `responses` on each call (the last value repeats if over-called)."
  [responses]
  (let [remaining (atom responses)]
    (fn [_ch]
      (let [[r & more] @remaining]
        (when (seq more) (reset! remaining more))
        r))))

(defn run-uds-capturing
  "Run run-uds against a real (but unused) UNIX listener with the frame seams
  redefined: outbound EDN strings are captured into an atom, inbound frames are
  the canned `responses`. Returns `{:out <stdout-string> :sent [<edn-string>...]}`."
  [responses]
  (with-uds-listener
    (fn [path]
      (let [sent (atom [])
            out (with-redefs [lc/uds-send-frame (fn [_ch s] (swap! sent conj s))
                              lc/uds-recv-frame (canned-recv responses)]
                  (with-out-str (lc/run-uds path)))]
        {:out out :sent @sent}))))

;; The full 8-frame UDS round-trip + the trailing (continue #cursor) + the watch
;; tail = 14 canned reply frames (one more than the 13 SENT frames: the
;; :watch-event push is an extra inbound frame that uds-await-watch-event reads
;; via uds-recv-frame, with no matching outbound frame). The 13 outbound frames
;; mirror main-handshake-edns plus the 4 watch-tail sends.
(def uds-handshake-responses
  [uds-welcome-edn uds-world-edn uds-proposed-edn uds-asserted-edn uds-result-edn
   uds-proposed-batch-edn uds-asserted-edn uds-page1-edn uds-page2-edn
   uds-watch-established-edn uds-watch-probe-proposed-edn uds-watch-probe-asserted-edn
   uds-watch-event-edn uds-unwatch-ok-edn])

(deftest run-uds-walks-full-roundtrip-to-result-without-throwing
  (let [{:keys [out sent]} (run-uds-capturing uds-handshake-responses)]
    (is (re-find #"hello -> :welcome" out) "prints the welcome line")
    (is (re-find #"query .* -> rows=" out) "reaches the final query/result line")
    (is (re-find #"paged query .* 3 rows over 2 page\(s\)" out)
        "drains the limit-2 query across two pages")
    (is (re-find #"watch .* -> :watch-event type=:asserted" out)
        "demuxes the pushed :watch-event off the frame stream")
    (is (re-find #"unwatch .* -> :ok" out)
        "and tears the watch down after observing the event")
    (is (not (re-find #"refused" out)) "no step is reported as refused")
    (is (= 13 (count sent))
        "all 13 outbound frames fire: the 9 paged-roundtrip frames plus the 4 watch-tail sends")))

(deftest run-uds-first-frame-is-the-anonymous-hello
  (let [{:keys [sent]} (run-uds-capturing uds-handshake-responses)]
    (is (= "(hello)" (first sent))
        "the opening frame is the bare hello verb")))

(deftest run-uds-never-echoes-the-session-id-into-later-frames
  ;; Over UDS the session is bound to the CONNECTION by the server, so the
  ;; client must NOT thread the welcome's session id back into any later frame.
  (let [{:keys [sent]} (run-uds-capturing uds-handshake-responses)]
    (is (every? #(not (re-find #"#session" %)) (rest sent))
        "no post-hello frame carries a #session tag")
    (is (every? #(not (re-find #"sess-uds" %)) (rest sent))
        "and none carries the welcome's session id literal")))

(deftest run-uds-threads-proposal-handle-from-propose-into-assert
  ;; The propose reply hands back #proposal "p-uds"; the assert frame must carry
  ;; that exact tagged literal as its verb argument.
  (let [{:keys [sent]} (run-uds-capturing uds-handshake-responses)
        ;; Frames in order: hello, use-world, propose, assert, query.
        assert-frame (nth sent 3)]
    (is (= "(assert #proposal \"p-uds\")" assert-frame)
        "the #proposal from propose threads verbatim into the assert verb")))

(deftest run-uds-sends-use-world-and-query-verbs-in-order
  (let [{:keys [sent]} (run-uds-capturing uds-handshake-responses)]
    (is (= "(use-world #world \"default\")" (nth sent 1))
        "second frame enters the default world")
    (is (re-find #"^\(query " (nth sent 4))
        "fifth frame is the query verb")))

(deftest run-uds-stops-cleanly-on-non-welcome-first-reply
  ;; A non-:welcome hello reply must NOT throw; run-uds prints and stops after
  ;; the single hello frame.
  (let [{:keys [out sent]}
        (run-uds-capturing ["{:event :error :reason :nope}"])]
    (is (re-find #"expected :welcome" out)
        "an error at hello is reported, not thrown")
    (is (= 1 (count sent)) "and the sequence stops after the failed hello")))

(deftest run-uds-stops-cleanly-on-refused-use-world
  ;; A rejection at use-world prints "refused" and halts before propose.
  (let [{:keys [out sent]}
        (run-uds-capturing [uds-welcome-edn
                            "{:event :rejected :reason :no-such-world}"])]
    (is (re-find #"use-world refused" out) "the refusal is reported")
    (is (= 2 (count sent)) "and no propose frame is sent after the refusal")))

;; ===========================================================================
;; (F) -main dispatch — argv routing to the right transport.
;;
;; with-redefs over run-http / run-uds to RECORD their argument instead of
;; touching any transport. Covers the three routes: an explicit `uds <path>`,
;; a bare base URL, and no args (HTTP default).
;; ===========================================================================

(deftest main-routes-uds-path-to-run-uds
  (let [got (atom ::unset)]
    (with-redefs [lc/run-uds  (fn [p] (reset! got [:uds p]))
                  lc/run-http (fn [b] (reset! got [:http b]))]
      (lc/-main "uds" "/x.sock"))
    (is (= [:uds "/x.sock"] @got)
        "(\"uds\" \"/x.sock\") dispatches to run-uds with that socket path")))

(deftest main-routes-bare-uds-to-default-socket
  (let [got (atom ::unset)]
    (with-redefs [lc/run-uds  (fn [p] (reset! got [:uds p]))
                  lc/run-http (fn [b] (reset! got [:http b]))]
      (lc/-main "uds"))
    (is (= [:uds lc/default-socket] @got)
        "a bare \"uds\" arg falls back to default-socket")))

(deftest main-routes-url-to-run-http
  (let [got (atom ::unset)]
    (with-redefs [lc/run-uds  (fn [p] (reset! got [:uds p]))
                  lc/run-http (fn [b] (reset! got [:http b]))]
      (lc/-main "http://host:9999"))
    (is (= [:http "http://host:9999"] @got)
        "a non-uds first arg is treated as the HTTP base URL")))

(deftest main-routes-no-args-to-run-http-default-base
  (let [got (atom ::unset)]
    (with-redefs [lc/run-uds  (fn [p] (reset! got [:uds p]))
                  lc/run-http (fn [b] (reset! got [:http b]))]
      (lc/-main))
    (is (= [:http lc/default-base] @got)
        "no args runs the HTTP round-trip against default-base")))

;; ===========================================================================
;; (G) read-welcome — parse the :welcome surface into a queryable ServerInfo.
;;
;; The welcome (SPEC §10) advertises :capabilities (a set), :limits (a map),
;; and the :verbs / :predicates surfaces (each {:core #{…} :extensions {pack
;; #{…}}}). read-welcome flattens those surfaces and exposes the rest as plain
;; data so supports? / max-message-bytes can interrogate it. Every section is
;; optional: a minimal welcome must yield empty defaults, never an error.
;; ===========================================================================

(def realistic-welcome-edn
  (str "{:event :welcome :version 1 :session #session \"s-1\" :world nil"
       " :capabilities #{:lemma/v1 :lemma/cursor-pagination :lemma/watch}"
       " :limits {:max-message-bytes 1048576}"
       " :predicates {:core #{equivalent subset-of} :extensions {}}"
       " :verbs {:core #{hello query continue} :extensions {}}}"))

(deftest read-welcome-advertised-capability-is-supported
  (let [info (lc/read-welcome (read-body realistic-welcome-edn))]
    (is (true? (lc/supports? info :lemma/cursor-pagination))
        "a capability the welcome lists is reported supported")))

(deftest read-welcome-unadvertised-capability-is-not-supported
  (let [info (lc/read-welcome (read-body realistic-welcome-edn))]
    (is (false? (lc/supports? info :lemma/nope))
        "a capability the welcome omits is reported unsupported")))

(deftest read-welcome-exposes-max-message-bytes-limit
  (let [info (lc/read-welcome (read-body realistic-welcome-edn))]
    (is (= 1048576 (lc/max-message-bytes info))
        "the :max-message-bytes limit is read straight off :limits")))

(deftest read-welcome-flattens-verb-and-predicate-core-members
  (let [info (lc/read-welcome (read-body realistic-welcome-edn))]
    (is (every? (:verbs info) '[hello query continue])
        "the core verbs appear in the flattened :verbs set")
    (is (every? (:predicates info) '[equivalent subset-of])
        "the core predicates appear in the flattened :predicates set")))

(deftest read-welcome-without-limits-yields-nil-max-message-bytes
  ;; A welcome that omits :limits entirely must not throw — max-message-bytes
  ;; returns nil ("unadvertised"), which within-message-limit? treats as
  ;; unlimited.
  (let [info (lc/read-welcome
              (read-body "{:event :welcome :version 1 :capabilities #{:lemma/v1}}"))]
    (is (nil? (lc/max-message-bytes info))
        "no :limits section => max-message-bytes is nil, no exception")))

(deftest read-welcome-flattens-extension-pack-members-into-verbs
  ;; An :extensions {pack #{foo}} surface must be unioned into the flat verb
  ;; set alongside :core, so a client asking "every verb this server knows"
  ;; sees the pack's verbs too.
  (let [info (lc/read-welcome
              (read-body (str "{:event :welcome"
                              " :verbs {:core #{hello} :extensions {pack #{foo}}}}")))]
    (is (contains? (:verbs info) 'foo)
        "an extension-pack verb is flattened into :verbs")
    (is (contains? (:verbs info) 'hello)
        "and the :core verbs remain alongside the extension members")))

;; ===========================================================================
;; (H) within-message-limit? — the outbound byte-cap guard.
;;
;; Measured in UTF-8 bytes against the welcome's :max-message-bytes (SPEC §10).
;; A small message passes a generous cap; an oversize one fails a tiny cap; an
;; unadvertised limit (nil) means unlimited so everything passes.
;; ===========================================================================

(defn info-with-limit
  "A ServerInfo whose :max-message-bytes is `n` (nil => no limit advertised)."
  [n]
  (lc/read-welcome {:limits (when n {:max-message-bytes n})}))

(deftest within-message-limit-small-message-passes-generous-cap
  (let [info (info-with-limit 1048576)]
    (is (true? (lc/within-message-limit? info "(hello)"))
        "a tiny message fits comfortably under a 1 MB cap")))

(deftest within-message-limit-oversize-message-fails-tiny-cap
  (let [info (info-with-limit 4)]
    (is (false? (lc/within-message-limit? info "(hello)"))
        "a 7-byte message exceeds a 4-byte cap and is rejected")))

(deftest within-message-limit-nil-limit-means-unlimited
  ;; A welcome with no :limits => max-message-bytes nil => every message passes,
  ;; however large.
  (let [info (info-with-limit nil)]
    (is (nil? (lc/max-message-bytes info)) "no limit is advertised")
    (is (true? (lc/within-message-limit? info (apply str (repeat 10000 "x"))))
        "with no advertised cap even a large message passes")))

;; ===========================================================================
;; (I) capability gating — when the welcome OMITS :lemma/cursor-pagination, the
;; paged-query block is skipped on BOTH transports. We drive the real round-trips
;; (run-http via the http-send seam, run-uds via the frame seams) with a
;; welcome whose :capabilities lacks cursor-pagination and assert: the skip note
;; is printed, no (continue …) / batched propose is sent, and the run is SHORTER
;; than the full paged run (5 sends vs. 9).
;; ===========================================================================

(def no-pagination-welcome-edn
  "{:event :welcome :version \"1.0\" :world #world \"default\" :capabilities #{:lemma/v1 :lemma/watch} :limits {:max-message-bytes 1048576}}")

(def uds-no-pagination-welcome-edn
  "{:event :welcome :version \"1.0\" :session #session \"sess-uds\" :world #world \"default\" :capabilities #{:lemma/v1 :lemma/watch} :limits {:max-message-bytes 1048576}}")

;; The un-paginated round-trip skips the paged block but — because this welcome
;; still advertises :lemma/watch — runs the watch tail: hello, use-world,
;; propose, assert, query (5 pre-pagination) + watch-pattern, probe propose,
;; probe assert, unwatch (4 watch-tail) => 9 sends, with the pagination skip note
;; between them. The SSE seam is mocked so no socket is touched.
(def http-unpaged-responses
  [no-pagination-welcome-edn world-edn proposed-edn asserted-edn result-edn
   watch-established-edn watch-probe-proposed-edn watch-probe-asserted-edn unwatch-ok-edn])

;; UDS un-paginated: the same 9 outbound frames, plus the extra inbound
;; :watch-event push frame uds-await-watch-event reads => 10 canned reply frames.
(def uds-unpaged-responses
  [uds-no-pagination-welcome-edn uds-world-edn uds-proposed-edn uds-asserted-edn uds-result-edn
   uds-watch-established-edn uds-watch-probe-proposed-edn uds-watch-probe-asserted-edn
   uds-watch-event-edn uds-unwatch-ok-edn])

(deftest run-http-skips-paged-query-when-cursor-pagination-not-advertised
  (let [[reqs send] (capturing-send
                     (into [(fake-response no-pagination-welcome-edn "sid-NOPAGE")]
                           (map fake-response (rest http-unpaged-responses))))
        out (with-redefs [lc/http-send send
                          lc/open-sse-stream mock-open-sse-stream
                          lc/read-sse-events mock-read-sse-events]
              (with-out-str (lc/-main)))]
    (is (re-find #"does not advertise cursor pagination; skipping" out)
        "the skip note is printed when the server omits the capability")
    (is (not (re-find #"rows over .* page\(s\)" out))
        "no paged-query result line is reached")
    (is (= 9 (count @reqs))
        "the 5 pre-pagination sends plus the 4 watch-tail sends fire — fewer than the full 13-send paged+watch run")
    (let [bodies (map #(publisher->string (.get (.bodyPublisher %))) @reqs)]
      (is (not-any? #(re-find #"continue" %) bodies)
          "no (continue #cursor …) frame is sent")
      (is (not-any? #(re-find #"sub-a" %) bodies)
          "and the batched subset-of propose (sub-a/sub-b/sub-c) is never sent"))))

(deftest run-uds-skips-paged-query-when-cursor-pagination-not-advertised
  (let [{:keys [out sent]} (run-uds-capturing uds-unpaged-responses)]
    (is (re-find #"does not advertise cursor pagination; skipping" out)
        "the skip note is printed when the server omits the capability")
    (is (not (re-find #"rows over .* page\(s\)" out))
        "no paged-query result line is reached")
    (is (= 9 (count sent))
        "the 5 pre-pagination frames plus the 4 watch-tail frames fire — fewer than the full 13-frame paged+watch run")
    (is (not-any? #(re-find #"continue" %) sent)
        "no (continue #cursor …) frame is sent")
    (is (not-any? #(re-find #"sub-a" %) sent)
        "and the batched subset-of propose (sub-a/sub-b/sub-c) is never framed out")))

;; ===========================================================================
;; (J) read-sse-events — chunked transfer-decode + SSE framing of the watch
;; event stream. We feed a CANNED chunked SSE byte stream through the seam over a
;; real in-process loopback TCP socket pair: a ServerSocket on an ephemeral port
;; accepts one client Socket; we write the canned chunked bytes from the accepted
;; end and call read-sse-events on the client end (its SO_TIMEOUT set for a
;; backstop; the writer closes its end so the reader hits a deterministic EOF).
;; No HTTP server, no Dianoia — just the chunked + SSE decoder under test.
;;
;; A chunk is `<hex-size>CRLF<body>CRLF`; a size-0 chunk is http-kit's
;; header-flush keep-alive (NOT end-of-stream). An SSE event is the run of lines
;; up to a blank line; `:`-prefixed lines are comment keep-alives; only `data:`
;; lines carry payload. read-sse-events concatenates an event's data: payloads
;; and parses the result as one EDN envelope (unknown tags -> tagged-literal).
;; ===========================================================================

(defn chunk-frame
  "Encode `body` (a String) as one HTTP chunked-transfer chunk:
  `<hex-byte-count>CRLF<body>CRLF`. The size is the UTF-8 BYTE count, as the
  decoder reads exactly that many bytes for the chunk body."
  [^String body]
  (let [n (alength (.getBytes body StandardCharsets/UTF_8))]
    (str (Integer/toString n 16) "\r\n" body "\r\n")))

(defn sse-event
  "Build the SSE wire text for one event from its `data:`/`:`-comment `lines`
  (each already including its leading `data:` or `:` marker). Lines are joined
  with \\n and the event is terminated by the blank line (\\n\\n) the decoder
  splits on."
  [lines]
  (str (str/join "\n" lines) "\n\n"))

(defn with-sse-socket-pair
  "Open a loopback TCP Socket pair in-process and call `(f client-socket
  write-fn)`. `write-fn` takes a String, writes its UTF-8 bytes to the SERVER
  end, and flushes; calling `(write-fn nil)` closes the server output so the
  client read sees EOF. The client socket carries a 2 s SO_TIMEOUT as a hang
  backstop. Tears down both sockets and the listener in a `finally`."
  [f]
  (let [srv (doto (ServerSocket.)
              (.bind (InetSocketAddress. (InetAddress/getLoopbackAddress) 0)))
        port (.getLocalPort srv)
        cli  (doto (Socket.)
               (.connect (InetSocketAddress. (InetAddress/getLoopbackAddress) port) 2000)
               (.setSoTimeout 2000))
        acc  (.accept srv)]
    (try
      (let [out (.getOutputStream acc)
            write-fn (fn [s]
                       (if (nil? s)
                         (.shutdownOutput acc)
                         (do (.write out (.getBytes ^String s StandardCharsets/UTF_8))
                             (.flush out))))]
        (f cli write-fn))
      (finally
        (.close acc)
        (.close cli)
        (.close srv)))))

(def sse-watch-event-edn
  "{:event :watch-event :type :asserted :data #fact {:predicate subset-of :subject #entity \"watch-probe-1\" :object #entity \"group\"}}")

(deftest read-sse-events-parses-the-watch-event-envelope
  (with-sse-socket-pair
    (fn [cli write]
      ;; A header-flush keep-alive (size-0 chunk), then one real event chunk
      ;; carrying the :watch-event, then EOF.
      (write (chunk-frame ""))                       ; size-0 keep-alive flush
      (write (chunk-frame (sse-event [(str "data: " sse-watch-event-edn)])))
      (write nil)                                    ; close => deterministic EOF
      (let [events (lc/read-sse-events {:sock cli :buf (StringBuilder.)} 4)
            evt (first events)]
        (is (= 1 (count events)) "exactly one envelope is parsed from the stream")
        (is (= :watch-event (:event evt)) "the parsed envelope is a :watch-event")
        (is (= :asserted (:type evt)) "its :type is carried through")
        (is (= (tagged-literal 'fact {:predicate 'subset-of
                                      :subject (lc/ent "watch-probe-1")
                                      :object  (lc/ent "group")})
               (:data evt))
            "its :data #fact round-trips with #entity tags intact")))))

(deftest read-sse-events-skips-colon-comment-keep-alives
  ;; A `:`-prefixed SSE comment line is a keep-alive that carries no data:, so it
  ;; must NOT yield an envelope — only the real data: event does.
  (with-sse-socket-pair
    (fn [cli write]
      (write (chunk-frame (sse-event [": this is a keep-alive comment"])))
      (write (chunk-frame (sse-event [(str "data: " sse-watch-event-edn)])))
      (write nil)
      (let [events (lc/read-sse-events {:sock cli :buf (StringBuilder.)} 4)]
        (is (= 1 (count events))
            "the :-comment block yields nothing; only the data: event is parsed")
        (is (= :watch-event (:event (first events)))
            "and that one parsed envelope is the watch event")))))

(deftest read-sse-events-treats-size-0-chunk-as-keep-alive-not-eof
  ;; http-kit writes an immediate size-0 chunk to flush headers; the decoder must
  ;; SKIP it and keep reading, not treat it as end-of-stream. A size-0 chunk
  ;; sandwiched BETWEEN two real event chunks must not truncate the read.
  (with-sse-socket-pair
    (fn [cli write]
      (write (chunk-frame (sse-event [(str "data: " sse-watch-event-edn)])))
      (write (chunk-frame ""))                       ; mid-stream keep-alive flush
      (write (chunk-frame (sse-event ["data: {:event :watch-event :type :retracted :data nil}"])))
      (write nil)
      (let [events (lc/read-sse-events {:sock cli :buf (StringBuilder.)} 4)]
        (is (= 2 (count events))
            "both events arrive — the interleaved size-0 chunk is a keep-alive, not EOF")
        (is (= [:asserted :retracted] (map :type events))
            "and they arrive in order across the keep-alive boundary")))))

(deftest read-sse-events-honors-max-events-and-terminates
  ;; With more events available than max-events, read-sse-events returns exactly
  ;; max-events and stops — it does not drain the whole stream.
  (with-sse-socket-pair
    (fn [cli write]
      (write (chunk-frame (sse-event ["data: {:event :watch-event :type :asserted :data 1}"])))
      (write (chunk-frame (sse-event ["data: {:event :watch-event :type :asserted :data 2}"])))
      (write (chunk-frame (sse-event ["data: {:event :watch-event :type :asserted :data 3}"])))
      (write nil)
      (let [events (lc/read-sse-events {:sock cli :buf (StringBuilder.)} 2)]
        (is (= 2 (count events)) "exactly max-events envelopes are returned")
        (is (= [1 2] (map :data events))
            "the first two events in order, and the read stops there")))))

(deftest read-sse-events-returns-empty-on-immediate-eof
  ;; A stream that closes before any event arrives degrades to an empty vector,
  ;; not an exception — the quiet-stream / closed-connection contract.
  (with-sse-socket-pair
    (fn [cli write]
      (write nil)                                    ; close with no bytes at all
      (is (= [] (lc/read-sse-events {:sock cli :buf (StringBuilder.)} 4))
          "an immediate EOF yields no events and does not throw"))))

;; ===========================================================================
;; (K) uds-await-watch-event — demux the :watch-event push out of the interleaved
;; UDS frame stream. recv-fn is a `(fn [] body)` returning successive canned
;; parsed bodies; the demux skips command-reply frames and returns the first
;; :watch-event, bounded by max-frames, and yields nil on no-event / premature
;; close. Pure in-memory — no socket.
;; ===========================================================================

(defn scripted-recv
  "Return `[calls-atom recv-fn]`. `recv-fn` is a `(fn [] body)` over a seq of
  canned parsed bodies: each call pops and returns the next body (recording how
  many times it was called). A body of ::throw makes recv-fn throw the same
  ex-info uds-recv-frame raises on a closed connection, so the demux's EOF path
  is exercised."
  [bodies]
  (let [remaining (atom bodies)
        calls     (atom 0)]
    [calls
     (fn []
       (swap! calls inc)
       (let [[b & more] @remaining]
         (reset! remaining more)
         (if (= ::throw b)
           (throw (ex-info "connection closed" {:reason :eof}))
           b)))]))

(deftest uds-await-watch-event-picks-the-watch-event-skipping-command-echoes
  ;; The push interleaves with command replies on the one frame stream: an
  ;; :asserted echo precedes the :watch-event. The demux must skip the echo and
  ;; return the :watch-event.
  (let [echo  {:event :asserted}
        watch {:event :watch-event :type :asserted
               :data  (tagged-literal 'fact {:predicate 'subset-of})}
        [calls recv] (scripted-recv [echo watch])
        evt (lc/uds-await-watch-event recv 8)]
    (is (= watch evt) "the :watch-event is returned, not the preceding :asserted echo")
    (is (= 2 @calls) "exactly two frames were read: the skipped echo, then the event")))

(deftest uds-await-watch-event-returns-the-first-watch-event
  ;; With the :watch-event in the very first frame, the demux returns immediately.
  (let [watch {:event :watch-event :type :retracted :data nil}
        [calls recv] (scripted-recv [watch {:event :asserted}])
        evt (lc/uds-await-watch-event recv 8)]
    (is (= watch evt) "the first frame already being a :watch-event is returned as-is")
    (is (= 1 @calls) "and no further frames are read once it is found")))

(deftest uds-await-watch-event-is-bounded-by-max-frames
  ;; If no :watch-event arrives within max-frames command echoes, the demux gives
  ;; up and returns nil — it reads AT MOST max-frames frames.
  (let [[calls recv] (scripted-recv (repeat {:event :asserted}))
        evt (lc/uds-await-watch-event recv 5)]
    (is (nil? evt) "no :watch-event within the budget yields nil")
    (is (= 5 @calls) "and the read is bounded to exactly max-frames frames")))

(deftest uds-await-watch-event-returns-nil-on-premature-close
  ;; A read failure (EOF / closed connection, surfaced as the :eof ex-info) ends
  ;; the loop early and yields nil rather than propagating the exception.
  (let [[calls recv] (scripted-recv [{:event :asserted} ::throw {:event :watch-event}])
        evt (lc/uds-await-watch-event recv 8)]
    (is (nil? evt) "a premature close ends the demux with nil")
    (is (= 2 @calls)
        "the loop stops at the closed-connection frame, before the later event")))

(deftest uds-await-watch-event-returns-nil-with-zero-budget
  ;; A non-positive max-frames reads nothing and returns nil immediately.
  (let [[calls recv] (scripted-recv [{:event :watch-event}])
        evt (lc/uds-await-watch-event recv 0)]
    (is (nil? evt) "a zero frame budget yields nil")
    (is (= 0 @calls) "and recv-fn is never called")))

;; ===========================================================================
;; (L) watch capability gating — when the welcome OMITS :lemma/watch, the watch
;; demo is skipped on BOTH transports: the skip note is printed, no
;; (watch-pattern …) is sent, and open-sse-stream is NEVER reached (we redef it
;; to THROW if called, so any stray socket open fails the test loudly). The run
;; stops after the paged round-trip.
;; ===========================================================================

(def no-watch-welcome-edn
  "{:event :welcome :version \"1.0\" :world #world \"default\" :capabilities #{:lemma/v1 :lemma/cursor-pagination} :limits {:max-message-bytes 1048576}}")

(def uds-no-watch-welcome-edn
  "{:event :welcome :version \"1.0\" :session #session \"sess-uds\" :world #world \"default\" :capabilities #{:lemma/v1 :lemma/cursor-pagination} :limits {:max-message-bytes 1048576}}")

;; The no-watch round-trip runs the full paged block (cursor-pagination IS
;; advertised) but skips the watch tail: 9 sends, no watch-pattern/unwatch.
(def http-no-watch-responses
  [no-watch-welcome-edn world-edn proposed-edn asserted-edn result-edn
   proposed-batch-edn asserted-edn page1-edn page2-edn])

(def uds-no-watch-responses
  [uds-no-watch-welcome-edn uds-world-edn uds-proposed-edn uds-asserted-edn uds-result-edn
   uds-proposed-batch-edn uds-asserted-edn uds-page1-edn uds-page2-edn])

(defn throwing-open-sse-stream
  "A redef for open-sse-stream that FAILS the test if reached: when watch is
  unadvertised the watch demo must be skipped, so this seam must never be
  called."
  [& _]
  (throw (ex-info "open-sse-stream must not be called when :lemma/watch is unadvertised" {})))

(deftest run-http-skips-watch-demo-when-watch-not-advertised
  (let [[reqs send] (capturing-send
                     (into [(fake-response no-watch-welcome-edn "sid-NOWATCH")]
                           (map fake-response (rest http-no-watch-responses))))
        out (with-redefs [lc/http-send send
                          lc/open-sse-stream throwing-open-sse-stream]
              (with-out-str (lc/-main)))]
    (is (re-find #"does not advertise watch; skipping watch demo" out)
        "the watch skip note is printed when the server omits :lemma/watch")
    (is (re-find #"paged query .* 3 rows over 2 page\(s\)" out)
        "the paged block still runs — only the watch demo is gated out")
    (is (= 9 (count @reqs))
        "exactly the 9 paged-roundtrip sends fire — the 4 watch-tail sends are skipped")
    (let [bodies (map #(publisher->string (.get (.bodyPublisher %))) @reqs)]
      (is (not-any? #(re-find #"watch-pattern" %) bodies)
          "no (watch-pattern …) frame is sent")
      (is (not-any? #(re-find #"unwatch" %) bodies)
          "and no (unwatch …) frame is sent"))))

(deftest run-uds-skips-watch-demo-when-watch-not-advertised
  (let [{:keys [out sent]} (run-uds-capturing uds-no-watch-responses)]
    (is (re-find #"does not advertise watch; skipping watch demo" out)
        "the watch skip note is printed when the server omits :lemma/watch")
    (is (re-find #"paged query .* 3 rows over 2 page\(s\)" out)
        "the paged block still runs — only the watch demo is gated out")
    (is (= 9 (count sent))
        "exactly the 9 paged-roundtrip frames fire — the 4 watch-tail frames are skipped")
    (is (not-any? #(re-find #"watch-pattern" %) sent)
        "no (watch-pattern …) frame is sent")
    (is (not-any? #(re-find #"unwatch" %) sent)
        "and no (unwatch …) frame is sent")))
