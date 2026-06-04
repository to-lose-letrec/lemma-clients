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
           (java.util.concurrent Flow$Subscriber Flow$Subscription)
           (java.util.concurrent.atomic AtomicReference)
           (java.nio.charset StandardCharsets)
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

(deftest main-walks-full-roundtrip-to-result-without-throwing
  (let [welcome (fake-response welcome-edn "sid-MAIN")
        responses [welcome
                   (fake-response world-edn)
                   (fake-response proposed-edn)
                   (fake-response asserted-edn)
                   (fake-response result-edn)]
        [reqs send] (capturing-send responses)
        out (with-redefs [lc/http-send send]
              (with-out-str (lc/-main)))]
    (is (re-find #"hello -> :welcome" out) "prints the welcome line")
    (is (re-find #"query .* -> rows=" out)
        "reaches the final query/result line")
    (is (not (re-find #"refused" out)) "no step is reported as refused")
    ;; The welcome-header session must flow into every later endpoint URI.
    (let [uris (map #(str (.uri %)) @reqs)]
      (is (= 5 (count uris)) "all five protocol steps were sent")
      (is (= (str lc/default-base "/v1/messages") (first uris))
          "first call is the anonymous hello on /v1/messages")
      (is (every? #(re-find #"/v1/sessions/sid-MAIN/messages" %) (rest uris))
          "the welcome session id threads into the named-endpoint URIs"))))

(deftest main-threads-proposal-handle-from-propose-into-assert
  ;; Step 4 returns #proposal "p-1"; step 5 (assert) must carry that exact
  ;; tagged literal back as its verb argument.
  (let [responses [(fake-response welcome-edn "sid-X")
                   (fake-response world-edn)
                   (fake-response proposed-edn)
                   (fake-response asserted-edn)
                   (fake-response result-edn)]
        [reqs send] (capturing-send responses)]
    (with-redefs [lc/http-send send]
      (with-out-str (lc/-main)))
    ;; The 4th request (index 3) is the assert. Its body should be
    ;; (assert #proposal "p-1") — the proposal handle from step 3.
    (let [assert-body (publisher->string (.get (.bodyPublisher (nth @reqs 3))))]
      (is (= "(assert #proposal \"p-1\")" assert-body)
          "the #proposal from propose threads verbatim into the assert verb"))))

(deftest main-sets-session-header-on-named-calls
  (let [responses [(fake-response welcome-edn "sid-H")
                   (fake-response world-edn)
                   (fake-response proposed-edn)
                   (fake-response asserted-edn)
                   (fake-response result-edn)]
        [reqs send] (capturing-send responses)]
    (with-redefs [lc/http-send send]
      (with-out-str (lc/-main)))
    ;; The hello (req 0) carries no session header; the named calls all do.
    (let [hdr (fn [i] (-> (.headers (nth @reqs i))
                          (.firstValue "x-lemma-session") (.orElse nil)))]
      (is (nil? (hdr 0)) "hello is anonymous")
      (is (= "sid-H" (hdr 1)) "use-world rides the session")
      (is (= "sid-H" (hdr 4)) "query rides the same session"))))

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
