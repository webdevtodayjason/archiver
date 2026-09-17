#!/usr/bin/env python3
"""The LAST LIGHT server, over a corpus and a device that do not exist.

Everything here runs offline. The device is four fakes - the answer, the model
chooser, the raw endpoint call entities.profile() makes on its own, and the
reachability probe - and the corpus is three books written into a temp database,
because the real one is a read-only artefact that took days to build and a test
that needs it is a test nobody can run.

What is actually being guarded, in order of how quietly it would break:

  archivist.ask() returns three different shapes and the server must not tidy
  them. cockpit.ask() normalises with `or []` and silently loses `via`, `hits`
  and `model`; the tests below send all three shapes through and assert the dict
  came out the far side with exactly the keys it went in with.

  An unreachable device must be found before the model call, not during it, or
  the person waits out a 420 second timeout holding a spinner.

  A handler that raises must answer 500 with a body. A dropped connection shows
  a blank screen, and a blank screen on this product reads as the box being
  broken when it is fine.
"""
import http.client
import importlib
import json
import os
import pathlib
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest

import archive
import archivist
import entities
import lastlight

PORT = 8710
PAGE_STANDIN = b"<!doctype html><title>LAST LIGHT</title><p>an off-grid digital " \
               b"grimoire<script>fetch('/api/ask')</script>"


def call(method, path, body=None, timeout=20):
    h = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    h.request(method, path, json.dumps(body) if body is not None else None,
              {"Content-Type": "application/json"} if body is not None else {})
    r = h.getresponse()
    raw = r.read()
    h.close()
    return r.status, raw


def get_json(path):
    code, raw = call("GET", path)
    return code, json.loads(raw)


STATE = {}


def setUpModule():  # noqa: N802
    """One corpus and one server for the whole file.

    Per-class would rebind the same port while the previous server is still
    listening on it, and the second bind fails inside a daemon thread where
    nothing sees it: the tests then talk to the first server over the second
    class's corpus and pass for the wrong reason.
    """
    tmp = tempfile.mkdtemp(prefix="lastlight-test-")
    STATE["tmp"] = tmp
    # A config the machine cannot have written, so a real ~/.config file cannot
    # reach in and point these tests at a real device.
    os.environ["LASTLIGHT_CONFIG"] = os.path.join(tmp, "cfg.json")
    STATE["was"] = lastlight.fake_device()
    STATE["c"] = lastlight.fake_corpus(pathlib.Path(tmp) / "corpus")

    # The real page when it exists, so the offline assertions start guarding it
    # the moment it lands; a stand-in until then.
    if not lastlight.PAGE.exists():
        page = pathlib.Path(tmp) / "lastlight.html"
        page.write_bytes(PAGE_STANDIN)
        lastlight.PAGE = page

    threading.Thread(target=lastlight.run,
                     kwargs={"port": PORT, "host": "127.0.0.1"},
                     daemon=True).start()
    for _ in range(100):
        try:
            if call("GET", "/api/health", timeout=2)[0] == 200:
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"the server never answered on {PORT}")


def tearDownModule():  # noqa: N802
    lastlight.restore(STATE["was"])
    shutil.rmtree(STATE["tmp"], ignore_errors=True)


class Served(unittest.TestCase):
    """Helpers over the one server setUpModule started."""

    @property
    def c(self):
        return STATE["c"]

    @property
    def was(self):
        return STATE["was"]

    def setUp(self):
        # Each test gets the device back up and the probe cache cleared, or the
        # one that takes it away decides the outcome of whatever runs next.
        lastlight._probe = lambda url, key, timeout: True
        lastlight._device_seen["at"], lastlight._device_seen["state"] = 0.0, None

    def down(self):
        lastlight._probe = lambda url, key, timeout: False
        lastlight._device_seen["at"], lastlight._device_seen["state"] = 0.0, None

    def answering(self, reply):
        """Point archivist.ask at one canned reply and count the calls."""
        calls = []

        def ask_(question, show_sources=True, quiet=False):
            calls.append((question, show_sources, quiet))
            return dict(reply)

        archivist.ask = ask_
        self.addCleanup(lambda: setattr(archivist, "ask", self.was[0]))
        return calls


# ------------------------------------------------------------------- the page
class Page(Served):
    def test_root_serves_the_page(self):
        code, raw = call("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"LAST LIGHT", raw)

    def test_nothing_on_the_page_comes_off_the_internet(self):
        """The box has no route off its own network, so a CDN is a blank page."""
        raw = call("GET", "/")[1]
        remote = [m.group(0) for m in re.finditer(
            rb"""(?:src|href)\s*=\s*['"]?\s*(?:https?:)?//[^'">\s]+""", raw, re.I)]
        self.assertEqual(remote, [], f"the page reaches off the box: {remote[:3]}")

    def test_the_page_can_reach_the_archivist(self):
        self.assertIn(b"/api/ask", call("GET", "/")[1])

    def test_every_ordering_the_page_asks_for_is_one_the_route_has(self):
        """The index asked for order=aboutness while the route read no order."""
        asked = set(re.findall(rb"order=([a-z]+)", call("GET", "/")[1]))
        self.assertLessEqual(asked, {b"aboutness", b"documents"}, asked)
        for want in asked:
            d = get_json("/api/subjects?limit=200&order=" + want.decode())[1]
            self.assertEqual(sorted(d["subjects"][0]),
                             ["documents", "mentions", "name"], want)

    def test_a_source_row_with_no_number_can_still_wrap(self):
        """Three of the four row renderers draw no [n] column: the entity
        answer, the refusal's IT READ and a subject profile. Below 480px the
        locator takes a line of its own, and a title next to it with a zero flex
        basis makes the line sum to exactly the width on offer, so the row never
        wraps and the title is squeezed to no width at all. Measured at 400px
        before the basis was made content-sized: a title 0px wide and 420px tall,
        one letter per line, on the panel whose whole job is to show what the
        Archivist read.
        """
        css = call("GET", "/")[1].decode()
        flex = re.search(r"\.cite \.t\{[^}]*?flex:([^;}]+)", css, re.S).group(1)
        self.assertEqual(flex.split()[-1], "auto", f".cite .t is flex:{flex}")

    def test_the_profile_paths_print_no_citation_number(self):
        """entities.profile() asks the model to cite over the thirty passages it
        read and then returns only the books those passages came from, so a [n]
        on the entity answer or a subject profile indexes something this page
        never shows and could not show. Measured on the device: 49 markers over
        8 books on one answer. Both screens strip them. The model path keeps
        its uncited brackets, because there they are the only sign a reader gets
        that the Archivist pointed at nothing.
        """
        page = call("GET", "/")[1].decode()
        self.assertEqual(re.findall(r"(?<!function )prose\((\w+)", page),
                         ["r", "unmarked", "unmarked"])


# ----------------------------------------------------------------- the corpus
class Health(Served):
    def test_shape(self):
        code, d = get_json("/api/health")
        self.assertEqual(code, 200)
        self.assertEqual(sorted(d), ["corpus", "device", "shelves"])
        self.assertEqual(d["corpus"], {"documents": 3, "chunks": 7,
                                       "vectors": 0, "subjects": 2})
        self.assertEqual(d["device"], {"reachable": True, "chat_model": "fake-chat"})
        self.assertEqual(sorted(d["shelves"][0]), ["documents", "name", "pages"])

    def test_subjects_are_counted_not_every_entity(self):
        """entity holds names and subjects both, and the totals are not the same."""
        self.c.execute("INSERT INTO entity(name,kind,mentions,docs) "
                       "VALUES('Richard',NULL,9,2)")
        self.c.commit()
        self.addCleanup(self.c.commit)
        self.addCleanup(self.c.execute, "DELETE FROM entity WHERE name='Richard'")
        self.assertEqual(get_json("/api/health")[1]["corpus"]["subjects"], 2)

    def test_device_down_is_a_state_not_a_failure(self):
        self.down()
        code, d = get_json("/api/health")
        self.assertEqual(code, 200)
        self.assertEqual(d["device"], {"reachable": False, "chat_model": None})
        self.assertEqual(d["corpus"]["documents"], 3, "browsing still works")

    def test_the_probe_is_cached(self):
        """A page that polls health must not poll the device with it."""
        hits = []
        lastlight._probe = lambda url, key, timeout: hits.append(url) or True
        lastlight._device_seen["at"], lastlight._device_seen["state"] = 0.0, None
        for _ in range(4):
            get_json("/api/health")
        self.assertEqual(len(hits), 1, hits)


class Shelves(Served):
    def test_shelves(self):
        code, d = get_json("/api/shelves")
        self.assertEqual(code, 200)
        self.assertEqual({s["name"]: s["documents"] for s in d},
                         {"survival shelf": 2, "encyclopaedia": 1})
        self.assertEqual({s["name"]: s["pages"] for s in d},
                         {"survival shelf": 8, "encyclopaedia": 1})

    def test_books_in_title_order(self):
        code, d = get_json("/api/books?shelf=survival%20shelf")
        self.assertEqual(code, 200)
        self.assertEqual(d["shelf"], "survival shelf")
        self.assertEqual([b["title"] for b in d["books"]],
                         ["Field Guide to Fire", "Manual of Water"])
        self.assertEqual(sorted(d["books"][0]), ["doc_id", "pages", "title"])
        self.assertEqual(d["books"][0]["pages"], 4)

    def test_books_on_a_shelf_that_is_not_there(self):
        code, d = get_json("/api/books?shelf=Vikidia")
        self.assertEqual(code, 404)
        self.assertIn("error", d)

    def test_books_with_no_shelf_named(self):
        self.assertEqual(get_json("/api/books")[0], 404)


class Subjects(Served):
    def test_shape_and_order(self):
        code, d = get_json("/api/subjects?limit=200")
        self.assertEqual(code, 200)
        self.assertEqual(sorted(d["subjects"][0]), ["documents", "mentions", "name"])
        self.assertEqual({s["name"] for s in d["subjects"]}, {"boil", "tourniquet"})

    def test_limit_is_honoured(self):
        self.assertEqual(len(get_json("/api/subjects?limit=1")[1]["subjects"]), 1)

    def test_a_limit_that_is_not_a_number_does_not_break_the_index(self):
        self.assertEqual(len(get_json("/api/subjects?limit=nine")[1]["subjects"]), 2)

    def wide_subject(self):
        """A subject in every book, so the two orderings cannot agree by luck.

        boil and tourniquet are each one document with 24 occurrences, so they
        score high on aboutness and low on document count; this one is the other
        way round, and which end it comes out at is the whole assertion.
        """
        self.c.execute("INSERT INTO entity(name,kind,mentions,docs) "
                       "VALUES('water','subject',3,3)")
        self.c.commit()
        self.addCleanup(self.c.commit)
        self.addCleanup(self.c.execute, "DELETE FROM entity WHERE name='water'")

    def test_order_defaults_to_document_count(self):
        self.wide_subject()
        d = get_json("/api/subjects?limit=200")[1]["subjects"]
        self.assertEqual(d[0]["name"], "water", d)
        self.assertEqual([s["documents"] for s in d], [3, 1, 1], d)

    def test_order_aboutness_is_the_index_not_the_word_count(self):
        """The page asks for this by name, and for a round nothing read it."""
        self.wide_subject()
        d = get_json("/api/subjects?limit=200&order=aboutness")[1]["subjects"]
        self.assertEqual(d[-1]["name"], "water", d)
        self.assertEqual({s["name"] for s in d}, {"boil", "tourniquet", "water"})

    def test_an_ordering_nobody_implemented_gets_the_default(self):
        self.wide_subject()
        d = get_json("/api/subjects?limit=200&order=zzz")[1]["subjects"]
        self.assertEqual([s["name"] for s in d],
                         [s["name"] for s in get_json("/api/subjects")[1]["subjects"]])

    def test_a_shelf_narrows_the_index(self):
        d = get_json("/api/subjects?shelf=survival%20shelf")[1]
        self.assertEqual({s["name"] for s in d["subjects"]}, {"boil", "tourniquet"})
        self.assertEqual(get_json("/api/subjects?shelf=encyclopaedia")[1]["subjects"], [])

    def test_a_subject_profile_comes_back_under_its_own_keys(self):
        code, d = get_json("/api/subject?name=boil")
        self.assertEqual(code, 200)
        self.assertEqual(sorted(d),
                         ["answer", "docs", "mentions", "name", "read", "sources"])
        self.assertEqual(d["name"], "boil")
        self.assertEqual(d["docs"], 1)

    def test_a_subject_nobody_indexed(self):
        code, d = get_json("/api/subject?name=hydrofoil")
        self.assertEqual(code, 404)
        self.assertIn("error", d)

    def test_a_subject_needs_the_device_too(self):
        """profile() reads every mention through the model, same as an answer."""
        self.down()
        code, d = get_json("/api/subject?name=boil")
        self.assertEqual(code, 503)
        self.assertEqual(d, {"error": lastlight.UNREACHABLE})


# ---------------------------------------------------------------- the answer
class Ask(Served):
    MODEL = {"answer": "Boil it for one minute [1].", "refused": False,
             "cited": [1], "sources": ["Manual of Water · pp1-2"], "model": "fake"}
    ENTITY = {"answer": "A deadfall is a weighted trap.", "refused": False,
              "cited": [], "sources": ["SAS Survival Handbook · zimgit"],
              "via": "10 documents"}
    FLOOR = {"answer": "Nothing I retrieved covers that.", "refused": True,
             "hits": [{"id": 4, "sim": 0.11, "text": "...", "title": "Manual of Water",
                       "source": "survival shelf", "doc_pages": 4, "pages": [1, 2]}]}

    def passthrough(self, reply):
        calls = self.answering(reply)
        code, d = call("POST", "/api/ask", {"q": "how do I purify water"})
        self.assertEqual(code, 200)
        d = json.loads(d)
        self.assertEqual(set(d), set(reply) | {"q", "secs"},
                         "the server added or dropped a key")
        for k, v in reply.items():
            self.assertEqual(d[k], v, f"{k} came back changed")
        self.assertEqual(d["q"], "how do I purify water")
        self.assertIsInstance(d["secs"], float)
        return calls

    def test_the_model_shape_goes_through_untouched(self):
        calls = self.passthrough(self.MODEL)
        self.assertEqual(calls, [("how do I purify water", False, True)],
                         "ask() must be called quiet and without printed sources")

    def test_the_entity_shape_keeps_via_and_its_empty_citations(self):
        """`cited: []` here means the entity path, not a failure. `or []` loses that."""
        self.passthrough(self.ENTITY)

    def test_the_similarity_floor_keeps_hits_and_grows_no_sources(self):
        self.passthrough(self.FLOOR)

    def test_a_blank_question(self):
        calls = self.answering(self.MODEL)
        for body in ({"q": ""}, {"q": "   "}, {}):
            code, raw = call("POST", "/api/ask", body)
            self.assertEqual(code, 400, body)
            self.assertIn("error", json.loads(raw))
        self.assertEqual(calls, [], "a blank question reached the model")

    def test_a_question_that_is_not_a_question(self):
        calls = self.answering(self.MODEL)
        self.assertEqual(call("POST", "/api/ask", {"q": "a" * 501})[0], 400)
        self.assertEqual(call("POST", "/api/ask", {"q": "a" * 500})[0], 200)
        self.assertEqual(len(calls), 1, "the long one was answered anyway")

    def test_an_unreachable_device_answers_before_the_model_does(self):
        calls = self.answering(self.MODEL)
        self.down()
        code, raw = call("POST", "/api/ask", {"q": "how do I stop severe bleeding"})
        self.assertEqual(code, 503)
        self.assertEqual(json.loads(raw), {"error": lastlight.UNREACHABLE})
        self.assertEqual(calls, [], "the model was called with the box switched off")

    def test_a_body_that_is_not_json(self):
        h = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        h.request("POST", "/api/ask", "{not json", {"Content-Type": "application/json"})
        r = h.getresponse()
        self.assertEqual(r.status, 400)
        h.close()


# ------------------------------------------------------------- what goes wrong
class Failing(Served):
    def test_an_unknown_path_is_json(self):
        code, d = get_json("/api/everything")
        self.assertEqual(code, 404)
        self.assertEqual(d, {"error": "not found"})

    def test_an_unknown_post_path_is_json(self):
        code, raw = call("POST", "/api/ask/now", {"q": "x"})
        self.assertEqual(code, 404)
        self.assertEqual(json.loads(raw), {"error": "not found"})

    def test_traversal_is_refused(self):
        """Sent raw, because http.client tidies a path before it goes out."""
        for path in ("/../lastlight.py", "/static/../../etc/passwd",
                     "/..%2f..%2flastlight.py", "/static/lastlight.html"):
            s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
            s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
                      .encode())
            raw = b""
            while True:
                b = s.recv(65536)
                if not b:
                    break
                raw += b
            s.close()
            self.assertIn(b"404", raw.split(b"\r\n")[0], path)
            self.assertNotIn(b"import archive", raw, f"{path} served source")

    def test_an_exception_becomes_a_500_with_a_body(self):
        """The connection must survive a fault, or the screen just goes blank."""
        def boom(c):
            raise RuntimeError("the shelf fell over")

        was, lastlight.shelves = lastlight.shelves, boom
        self.addCleanup(lambda: setattr(lastlight, "shelves", was))
        code, raw = call("GET", "/api/shelves")
        self.assertEqual(code, 500)
        d = json.loads(raw)
        self.assertEqual(sorted(d), ["error"])
        self.assertTrue(d["error"].endswith("."), d["error"])

    def test_the_terminal_exit_becomes_a_sentence(self):
        """archivist.die() raises SystemExit, which no `except Exception` catches."""
        def gone(question, show_sources=True, quiet=False):
            raise SystemExit("  No chat model is loaded. Load one, or set LASTLIGHT_CHAT.")

        archivist.ask = gone
        self.addCleanup(lambda: setattr(archivist, "ask", self.was[0]))
        code, raw = call("POST", "/api/ask", {"q": "how do I purify water"})
        self.assertEqual(code, 500)
        self.assertEqual(json.loads(raw)["error"],
                         "No chat model is loaded. Load one, or set LASTLIGHT_CHAT.")


class Selfcheck(unittest.TestCase):
    def test_selfcheck_passes_in_process(self):
        """The farm runs this one, so it has to be the thing that fails loudly."""
        try:
            self.assertEqual(lastlight.selfcheck(), 0)
        finally:
            # It builds its own corpus, puts the real functions back and deletes
            # the directory on its way out, which is right for the farm and
            # leaves this file's fixture pointing at nothing. Put it back, or
            # whichever class unittest runs next fails for no visible reason.
            os.environ["ARCHIVER_HOME"] = str(pathlib.Path(STATE["tmp"]) / "corpus")
            os.environ["LASTLIGHT_CONFIG"] = os.path.join(STATE["tmp"], "cfg.json")
            importlib.reload(archive)
            importlib.reload(entities)
            STATE["was"] = lastlight.fake_device()


if __name__ == "__main__":
    unittest.main()
