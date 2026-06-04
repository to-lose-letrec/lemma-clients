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
            [lemma-client :as lc])
  (:import (java.net.http HttpResponse HttpHeaders)
           (java.net UnixDomainSocketAddress StandardProtocolFamily)
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

(def welcome-edn "{:event :welcome :version \"1.0\" :world #world \"default\"}")
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

;; The full 8-send HTTP round-trip plus the trailing (continue #cursor) = 9
;; canned replies: hello, use-world, propose, assert, query (single page),
;; propose(batch), assert, query (page 1 :done? false), continue (page 2 :done? true).
(def main-handshake-edns
  [welcome-edn world-edn proposed-edn asserted-edn result-edn
   proposed-batch-edn asserted-edn page1-edn page2-edn])

(defn main-handshake-responses
  "The 9 canned HttpResponses for the full paged round-trip; the welcome carries
  `session` so the named-endpoint URIs can be asserted."
  [session]
  (into [(fake-response welcome-edn session)]
        (map fake-response (rest main-handshake-edns))))

(deftest main-walks-full-roundtrip-to-result-without-throwing
  (let [[reqs send] (capturing-send (main-handshake-responses "sid-MAIN"))
        out (with-redefs [lc/http-send send]
              (with-out-str (lc/-main)))]
    (is (re-find #"hello -> :welcome" out) "prints the welcome line")
    (is (re-find #"query .* -> rows=" out)
        "reaches the final query/result line")
    (is (re-find #"paged query .* 3 rows over 2 page\(s\)" out)
        "drains the limit-2 query across two pages")
    (is (not (re-find #"refused" out)) "no step is reported as refused")
    ;; The welcome-header session must flow into every later endpoint URI.
    (let [uris (map #(str (.uri %)) @reqs)]
      (is (= 9 (count uris))
          "all eight round-trip steps plus the (continue #cursor) were sent")
      (is (= (str lc/default-base "/v1/messages") (first uris))
          "first call is the anonymous hello on /v1/messages")
      (is (every? #(re-find #"/v1/sessions/sid-MAIN/messages" %) (rest uris))
          "the welcome session id threads into the named-endpoint URIs"))))

(deftest main-threads-proposal-handle-from-propose-into-assert
  ;; Step 4 returns #proposal "p-1"; step 5 (assert) must carry that exact
  ;; tagged literal back as its verb argument.
  (let [[reqs send] (capturing-send (main-handshake-responses "sid-X"))]
    (with-redefs [lc/http-send send]
      (with-out-str (lc/-main)))
    ;; The 4th request (index 3) is the assert. Its body should be
    ;; (assert #proposal "p-1") — the proposal handle from step 3.
    (let [assert-body (publisher->string (.get (.bodyPublisher (nth @reqs 3))))]
      (is (= "(assert #proposal \"p-1\")" assert-body)
          "the #proposal from propose threads verbatim into the assert verb"))))

(deftest main-sets-session-header-on-named-calls
  (let [[reqs send] (capturing-send (main-handshake-responses "sid-H"))]
    (with-redefs [lc/http-send send]
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

(def uds-welcome-edn  "{:event :welcome :version \"1.0\" :session #session \"sess-uds\" :world #world \"default\"}")
(def uds-world-edn    "{:event :world-selected :world #world \"default\"}")
(def uds-proposed-edn "{:event :proposed :proposal #proposal \"p-uds\"}")
(def uds-asserted-edn "{:event :asserted}")
(def uds-result-edn   "{:event :result :rows [[#entity \"venus\"]] :done? true}")
;; Same cursor-pagination tail as the HTTP path: batched propose/assert, then a
;; :limit-2 query drained over two pages via (continue #cursor "c-uds").
(def uds-proposed-batch-edn "{:event :proposed :proposal #proposal \"p-uds-2\"}")
(def uds-page1-edn "{:event :result :rows [[#entity \"sub-a\"] [#entity \"sub-b\"]] :done? false :cursor #cursor \"c-uds\"}")
(def uds-page2-edn "{:event :result :rows [[#entity \"sub-c\"]] :done? true}")

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

;; The full 8-frame UDS round-trip plus the trailing (continue #cursor) = 9
;; canned replies, mirroring main-handshake-edns.
(def uds-handshake-responses
  [uds-welcome-edn uds-world-edn uds-proposed-edn uds-asserted-edn uds-result-edn
   uds-proposed-batch-edn uds-asserted-edn uds-page1-edn uds-page2-edn])

(deftest run-uds-walks-full-roundtrip-to-result-without-throwing
  (let [{:keys [out sent]} (run-uds-capturing uds-handshake-responses)]
    (is (re-find #"hello -> :welcome" out) "prints the welcome line")
    (is (re-find #"query .* -> rows=" out) "reaches the final query/result line")
    (is (re-find #"paged query .* 3 rows over 2 page\(s\)" out)
        "drains the limit-2 query across two pages")
    (is (not (re-find #"refused" out)) "no step is reported as refused")
    (is (= 9 (count sent))
        "all eight round-trip frames plus the (continue #cursor) were framed out")))

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
