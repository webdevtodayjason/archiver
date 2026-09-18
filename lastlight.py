#!/usr/bin/env python3
"""LAST LIGHT - the off-grid library, served to whoever is holding the phone.

    python3 lastlight.py               serve on 8700
    python3 lastlight.py 8711          serve somewhere else
    python3 lastlight.py --selfcheck   prove it works with no device and no corpus

The cockpit is a desk tool for reading your own notes and it looks like one. This
is the other thing the same corpus can be: a box with no internet that answers a
question out of real books and names the page, opened by somebody who cannot look
anything up any other way. So this file's whole job is to put archivist.ask() and
four shelf queries behind seven routes and then get out of the way. Retrieval, the
similarity floor and the citation guard all stay where they are; nothing here
re-implements any of them, because a second copy of the guard is a second copy
that can be wrong.

Two rules it keeps that the cockpit does not:

  Nothing is normalised. archivist.ask() returns three different shapes - the
  entity path, the similarity floor, the model - and which one arrived is
  information the person needs. cockpit.ask() flattens them with `or []` and the
  path, the floor and the model name are lost on the way out. Here every key
  ask() produced is passed through under its own name and the page decides.

  Nothing drops a connection. A handler that raises leaves the browser on a dead
  socket with nothing on screen, which on this product reads as the box being
  broken, which is the one thing it must never say when it is fine. Every route
  returns a status and a JSON body, the failures included.
"""
import json
import math
import os
import pathlib
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import archive
import archivist
import entities

HERE = pathlib.Path(__file__).resolve().parent
PAGE = HERE / "static" / "lastlight.html"

# A question longer than this is not a question. The cap is here rather than in
# archivist because the cost of finding out is a 420 second model call.
NOTHING_ON_THE_SHELF = ("There is nothing on the shelf yet. Download it first, and then every answer comes with the book and the page it came from.")
NO_SHELF_YET = ("The shelf has not been published yet, so there is nothing to download. This build asks and cites; it does not ingest PDFs, because that needs a program the farm will not run.")
# One download at a time; two would append two streams into one part file.
DOWNLOAD = threading.Semaphore(1)
MAX_QUESTION = 500
# What a browser polling /api/health costs the device: one GET every twenty
# seconds at most, and three seconds before it gives up. The page asks on every
# view change and the device is busy answering somebody.
DEVICE_TTL = 20.0
DEVICE_TIMEOUT = 3.0


def _config_path():
    """Where the device settings live, read at call time rather than at import.

    archive.py computes HOME from the environment once at import and the rest of
    this repo works around it with importlib.reload. Reading the path when it is
    needed costs nothing and means a test can point this somewhere else without
    reloading the module.
    """
    return pathlib.Path(os.environ.get("LASTLIGHT_CONFIG") or
                        pathlib.Path.home() / ".config" / "tiiny-brain.json")


def apply_cfg():
    """Carry the saved device settings into archivist. Env always wins.

    The host, the port and the key describe the box, not the app talking to it,
    so this reads the file the cockpit already writes instead of asking a person
    to type the same three values a second time. Two copies of a device address
    is how one of them ends up stale.
    """
    try:
        c = json.loads(_config_path().read_text())
    except Exception:  # noqa: BLE001
        return
    if c.get("host") and not os.environ.get("TIINY_HOST"):
        archivist.HOST = c["host"]
    if c.get("port") and not os.environ.get("TIINY_PORT"):
        archivist.PORT = str(c["port"])
    if c.get("key") and not os.environ.get("TIINY_KEY"):
        archivist.KEY = c["key"]
    if c.get("chat_url") and not os.environ.get("LASTLIGHT_CHAT_URL"):
        archivist.CHAT_URL = c["chat_url"]


# ------------------------------------------------------------------ the device
_device_lock = threading.Lock()
_device_seen = {"at": 0.0, "state": None}


def _probe(url, key, timeout):
    """Does this endpoint answer. Never raises.

    A 401 is reachable: the box is there and wants a key, which is a different
    fault from the box being off and has to be reported as one.
    """
    req = urllib.request.Request(url)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code in (401, 403)
    except Exception:  # noqa: BLE001
        return False


def chat_model():
    """What pick_chat() would choose, or None when it would give up.

    pick_chat() is written for a terminal, so its failure is die() and the
    process ends. Here the answer is one field of a health payload, so the exit
    and the urllib failures underneath it both become null and the page says the
    Tiiny is not answering rather than showing nothing at all.
    """
    try:
        return archivist.pick_chat()[0]
    except SystemExit:
        return None
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return None


def device():
    """{"reachable": bool, "chat_model": str or None}, cached for DEVICE_TTL.

    Reachability is asked of the device rather than of an alternate chat
    endpoint even when one is configured, because the embeddings can only come
    from the machine holding the index: no device, no retrieval, and nothing to
    answer out of whatever else is running.
    """
    now = time.time()
    with _device_lock:
        seen = _device_seen
        if seen["state"] is not None and now - seen["at"] < DEVICE_TTL:
            return dict(seen["state"])
    base = f"http://{archivist.HOST}:{archivist.PORT}"
    up = bool(archivist.HOST) and _probe(base + "/v1/models", archivist.KEY,
                                         DEVICE_TIMEOUT)
    state = {"reachable": up, "chat_model": chat_model() if up else None}
    with _device_lock:
        _device_seen["at"], _device_seen["state"] = time.time(), dict(state)
    return state


# ------------------------------------------------------------------ the corpus
def _tables(c):
    return {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def corpus(c):
    """The four counts, taken straight rather than through cockpit.vitals().

    vitals() runs the graph reduction on its way past, which is a scan of 437,370
    mention rows; the CHANGELOG has that at 834 seconds on this corpus before it
    was cut to 3. A health poll must not pay for a picture this product does not
    draw. The vec table is archivist's and the entity table is entities', so a
    corpus that was never embedded or never indexed answers zero instead of
    raising on a table that is not there.
    """
    have = _tables(c)
    q = lambda s: c.execute(s).fetchone()[0]
    return {
        "documents": q("SELECT COUNT(*) FROM doc"),
        "chunks": q("SELECT COUNT(*) FROM chunk"),
        "vectors": q("SELECT COUNT(*) FROM vec") if "vec" in have else 0,
        # The subject index, not every entity. entity holds both populations and
        # the capitalised names outnumber the subjects three to one, so the total
        # is not the number of subjects and saying so on screen would be wrong.
        "subjects": q("SELECT COUNT(*) FROM entity WHERE kind='subject'")
                    if "entity" in have else 0,
    }


def shelves(c):
    """The shelves, counted in documents.

    The same GROUP BY the cockpit's shelf list uses, with the count named
    documents rather than notes. Calling A Book for Midwives a note is exactly
    the vocabulary this product exists to leave behind.
    """
    return [{"name": r["name"], "documents": r["documents"], "pages": r["pages"] or 0}
            for r in c.execute(
                "SELECT source AS name, COUNT(*) documents, SUM(pages) pages "
                "FROM doc GROUP BY source ORDER BY documents DESC")]


def books(c, shelf):
    """Every book on one shelf in title order, or None when there is no shelf.

    Whole, with no LIMIT. cockpit.shelf() caps at 200 and sorts longest first,
    which on the 5,934 article Vikidia shelf silently showed A through Ba; the
    count it returns alongside exists to admit that. A browse view that has the
    whole shelf can filter it locally instead, and the biggest shelf here is
    336 KB of JSON over a LAN with nothing else on it.
    """
    n = c.execute("SELECT COUNT(*) n FROM doc WHERE source=?", (shelf,)).fetchone()["n"]
    if not n:
        return None
    return {"shelf": shelf,
            "books": [{"doc_id": r["id"], "title": r["title"], "pages": r["pages"] or 0}
                      for r in c.execute(
                          "SELECT id, title, pages FROM doc WHERE source=? "
                          "ORDER BY title", (shelf,))]}


def subjects(c, shelf="", limit=200, order="documents"):
    """The subject index, in one of the two orderings, named by the caller.

    `documents` is the default and is the plain document count the route was
    specified to return. It reads like what it is: water, ground, side, keep,
    leaves, a list of common words.

    `aboutness` is what the index screen asks for by name, and it is the score
    entities.subjects() used when it chose these rows - occurrences per document,
    damped by how few documents that is - which returns influenza, radiological,
    suture, antiviral, quarantine, fallout. The score is not stored, so it is
    recomputed here from the two columns that are.

    Anything else gets the default. The parameter exists because the page was
    already sending order=aboutness while this function read no order at all and
    sorted one way regardless: it agreed with the page by luck, and the next
    person to implement the parameter would have silently reordered the index
    without touching the screen that draws it.

    A shelf narrows which subjects are listed and nothing else. The counts stay
    corpus-wide on purpose: the mention table holds one row per chunk, so a
    shelf-scoped recount would report chunks where the unscoped list reports
    occurrences, and a number that changes meaning between two views of one index
    is worse than a number that is always the same measurement.
    """
    if "entity" not in _tables(c):
        return {"subjects": []}
    ndocs = c.execute("SELECT COUNT(*) n FROM doc").fetchone()["n"] or 1
    sql = "SELECT name, docs, mentions FROM entity e WHERE kind='subject'"
    args = []
    if shelf:
        sql += (" AND EXISTS (SELECT 1 FROM mention m JOIN doc d ON d.id=m.doc_id "
                "WHERE m.entity_id=e.id AND d.source=?)")
        args.append(shelf)
    rows = [{"name": r["name"], "documents": r["docs"], "mentions": r["mentions"]}
            for r in c.execute(sql, args) if r["docs"] > 0]
    if order == "aboutness":
        rows.sort(key=lambda r: -(r["mentions"] / r["documents"]
                                  * math.log(max(ndocs / r["documents"], 1.0000001))))
    else:
        rows.sort(key=lambda r: (-r["documents"], r["name"]))
    return {"subjects": rows[:limit]}


# ------------------------------------------------------------------ answering
def ask(question):
    """archivist.ask(), with the question and what it cost alongside.

    Passed through whole. Which of ask()'s three shapes came back is the most
    useful thing on the reply - an answer off the entity path carries `via` and
    no citations, the similarity floor carries `hits` and no sources, the model
    carries `cited` and `model` - and flattening them into one shape throws away
    the only thing that tells a person how their answer was arrived at.
    """
    t = time.time()
    r = archivist.ask(question, quiet=True, show_sources=False)
    r["q"] = question
    r["secs"] = round(time.time() - t, 2)
    return r


UNREACHABLE = "The Archivist cannot reach the Tiiny."


# --------------------------------------------------------------------- serving
class Handler(BaseHTTPRequestHandler):
    server_version = "lastlight"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _finish(self, route):
        """Run a route to completion before a single byte is written.

        A route returns its status and its body and this sends them, so an
        exception is always raised while nothing is on the wire and can always
        become a 500 with a body. Writing as we went would make a late failure
        unreportable: half a JSON object and a closed socket.

        SystemExit is caught with the rest because archivist.die() raises it -
        "No chat model is loaded" is a sentence worth showing somebody, and
        letting it out of a handler thread drops the connection instead.
        """
        try:
            code, body, ctype = route()
        except (Exception, SystemExit) as e:  # noqa: BLE001
            traceback.print_exc()
            msg = str(e).strip() if isinstance(e, SystemExit) else ""
            code, ctype = 500, "application/json"
            body = json.dumps({"error": msg or "The library could not answer that."})
        self._send(code, body, ctype)

    def _json(self, obj, code=200):
        return code, json.dumps(obj), "application/json"

    def do_GET(self):  # noqa: N802
        self._finish(lambda: self._get(urllib.parse.urlparse(self.path)))

    def do_POST(self):  # noqa: N802
        self._finish(lambda: self._post(urllib.parse.urlparse(self.path)))

    def _get(self, u):
        q = urllib.parse.parse_qs(u.query)
        one = lambda k, d="": (q.get(k) or [d])[0]

        # The page is served from one fixed path and there is no route that
        # builds a path out of the URL, so there is nothing for ../ to reach:
        # anything that is not exactly "/" falls through to the 404 below.
        if u.path == "/":
            if not PAGE.exists():
                return self._json({"error": "The page is missing from static/."}, 500)
            return 200, PAGE.read_bytes(), "text/html; charset=utf-8"

        if u.path == "/api/health":
            c = archive.db()
            return self._json({"corpus": corpus(c), "shelves": shelves(c),
                               "device": device()})
        # The farm build arrives with no corpus, so the page has to be able to
        # ask whether there is one before it asks anything of it. Kept out of
        # /api/health on purpose: health is about the corpus that exists, this
        # is about whether one does.
        if u.path == "/api/shelf":
            import shelf
            return self._json(shelf.state())
        if u.path == "/api/shelves":
            return self._json(shelves(archive.db()))
        if u.path == "/api/books":
            d = books(archive.db(), one("shelf"))
            return self._json(d or {"error": "There is no shelf by that name."},
                              200 if d else 404)
        if u.path == "/api/subjects":
            try:
                limit = max(1, min(int(one("limit", "200")), 2500))
            except ValueError:
                limit = 200
            return self._json(subjects(archive.db(), one("shelf").strip(), limit,
                                       one("order", "documents")))
        if u.path == "/api/subject":
            name = one("name").strip()
            if not name:
                return self._json({"error": "Name a subject."}, 400)
            # profile() reads every mention through the chat model, so it costs
            # what an answer costs and fails the same way when the box is off.
            if not device()["reachable"]:
                return self._json({"error": UNREACHABLE}, 503)
            d = entities.profile(name)
            return self._json(d, 404 if d.get("error") else 200)
        return self._json({"error": "not found"}, 404)

    def _post(self, u):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or "{}")
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if u.path == "/api/ask":
            question = (body.get("q") or "").strip()
            if not question:
                return self._json({"error": "Ask something."}, 400)
            if len(question) > MAX_QUESTION:
                return self._json(
                    {"error": f"Ask something shorter than {MAX_QUESTION} characters."},
                    400)
            # An empty corpus is checked first of all. archivist.search() calls
            # die() on one, which is sys.exit inside a request handler: the
            # browser got a 500 quoting "run: archivist index", a command the
            # farm build does not ship because it cannot ingest. Say the true
            # thing instead, which is that the shelf is not here yet.
            import shelf
            if not shelf.installed():
                return self._json({"error": NOTHING_ON_THE_SHELF}, 503)
            # Checked before the model rather than after, because the failure of
            # an unreachable box is a urllib timeout inside a 420 second call and
            # the person is left holding a spinner for the whole of it.
            if not device()["reachable"]:
                return self._json({"error": UNREACHABLE}, 503)
            return self._json(ask(question))
        if u.path == "/api/shelf/download":
            import shelf
            if shelf.installed():
                return self._json(shelf.state())
            if not shelf.configured():
                return self._json({"error": NO_SHELF_YET}, 503)
            # One at a time. A second press while the first is running would
            # append two streams into the same part file.
            if not DOWNLOAD.acquire(blocking=False):
                return self._json({"error": "The shelf is already downloading."},
                                  409)
            try:
                return self._json(shelf.install())
            except RuntimeError as exc:
                return self._json({"error": str(exc)}, 502)
            finally:
                DOWNLOAD.release()
        return self._json({"error": "not found"}, 404)


def _serve(host, port):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


def run(port=8700, host="0.0.0.0"):
    """Serve, on every interface this box has.

    The cockpit binds 127.0.0.1 because it is a desk tool on the machine holding
    the notes. This is opened one-handed on a phone joined to the Tiiny's own
    network, and a loopback bind makes that impossible, so the bind is wide and
    deliberate. The device has no route off its network for it to be wide on.
    """
    apply_cfg()
    srv = _serve(host, port)
    print(f"\n  LAST LIGHT   http://{host}:{port}/")
    print(f"  archive      {archive.HOME}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye")
    finally:
        srv.server_close()


# ------------------------------------------------------------------ selfcheck
def fake_corpus(tmp):
    """Two shelves, three books and two subjects, built from nothing.

    ARCHIVER_HOME is read once when archive.py is imported, so pointing it
    somewhere else means reloading the module; entities follows because the
    cockpit's own selfchecks reload it in that order and a half-reloaded pair is
    a bad afternoon. Returns the connection.
    """
    import importlib
    os.environ["ARCHIVER_HOME"] = str(tmp)
    importlib.reload(archive)
    importlib.reload(entities)

    filler = ("The chapter continues with general remarks about equipment and "
              "weather and the ordinary business of being outdoors for a long "
              "time without help arriving. ")
    shelf = [("Manual of Water", "survival shelf", 4,
              "Boil the water for one minute to make it safe to drink. "),
             ("Field Guide to Fire", "survival shelf", 4,
              "A tourniquet goes above the wound and the time goes on the skin. "),
             ("Water", "encyclopaedia", 1, "Water is a liquid that freezes. ")]
    c = archive.db()
    entities._schema(c)
    for i, (title, source, pages, body) in enumerate(shelf):
        cur = c.execute(
            "INSERT INTO doc(path,title,source,pages,sha,added_at) "
            "VALUES(?,?,?,?,?,?)",
            (f"book#{i}", title, source, pages, f"sha{i}", archive.now()))
        doc_id = cur.lastrowid
        for p in range(1, pages + 1):
            text = filler * 3 + body * 6
            c.execute("INSERT INTO page(doc_id,page_no,status,engine,conf,chars,"
                      "text,done_at) VALUES(?,?,'text','selfcheck',1.0,?,?,?)",
                      (doc_id, p, len(text), text, archive.now()))
        c.commit()
        archive.chunk_doc(c, doc_id)

    # Written in rather than built with entities.subjects(), which needs a corpus
    # wide enough to have a document-frequency window at all; three books have
    # none. The rows are the shape subjects() writes: occurrences in mentions, a
    # mention row per chunk that carries the term.
    for name, docs, occurrences, title in (("boil", 1, 24, "Manual of Water"),
                                           ("tourniquet", 1, 24, "Field Guide to Fire")):
        cur = c.execute("INSERT INTO entity(name,kind,mentions,docs) "
                        "VALUES(?,'subject',?,?)", (name, occurrences, docs))
        eid = cur.lastrowid
        c.executemany(
            "INSERT INTO mention(entity_id,doc_id,chunk_id) VALUES(?,?,?)",
            [(eid, r["doc_id"], r["id"]) for r in c.execute(
                "SELECT ch.id, ch.doc_id FROM chunk ch JOIN doc d ON d.id=ch.doc_id "
                "WHERE d.title=?", (title,))])
    c.commit()
    return c


ANSWER = {"answer": "Boil it for one minute [1].", "refused": False,
          "cited": [1], "sources": ["Manual of Water · pp1-2"], "model": "fake"}


def fake_device(up=True, model="fake-chat"):
    """Everything that would touch the network, replaced in one call.

    Returns what to put back. The four seams are the whole surface: the answer,
    the model chooser, the raw endpoint call that entities.profile() makes on its
    own, and the reachability probe. Miss any one of them and the check reaches
    for a device that is not there, which is the failure this is written to make
    impossible to ship.
    """
    was = (archivist.ask, archivist.pick_chat, archivist.api, globals()["_probe"],
           archivist.HOST, archivist.KEY)

    def ask_(question, show_sources=True, quiet=False):
        d = dict(ANSWER)
        d["asked"] = question
        return d

    def api_(path, body, timeout=300, base=None, key=None):
        return {"choices": [{"message": {"content": "It is a subject [1]."}}]}

    archivist.ask = ask_
    archivist.pick_chat = lambda: (model, None, "k")
    archivist.api = api_
    # A host with no device behind it. device() refuses to call out at all when
    # nothing is configured, which is right and would also hide the probe.
    archivist.HOST, archivist.KEY = "selfcheck.invalid", "k"
    globals()["_probe"] = lambda url, key, timeout: up
    _device_seen["at"], _device_seen["state"] = 0.0, None
    return was


def restore(was):
    archivist.ask, archivist.pick_chat, archivist.api = was[0], was[1], was[2]
    globals()["_probe"] = was[3]
    archivist.HOST, archivist.KEY = was[4], was[5]
    _device_seen["at"], _device_seen["state"] = 0.0, None


def selfcheck():
    """Every route, over a corpus and a device that do not exist.

    No network, no device, no package, temp files under /tmp, because this is the
    half that can be checked somewhere other than the machine it will live on and
    it runs in CI on the farm with the app directory read only.
    """
    import http.client
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="lastlight-selfcheck-")
    os.environ["LASTLIGHT_CONFIG"] = os.path.join(tmp, "cfg.json")
    was = fake_device()
    srv = None
    try:
        c = fake_corpus(pathlib.Path(tmp) / "corpus")
        assert c.execute("SELECT COUNT(*) n FROM doc").fetchone()["n"] == 3

        srv = _serve("127.0.0.1", 0)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def call(method, path, body=None):
            h = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            h.request(method, path, json.dumps(body) if body else None,
                      {"Content-Type": "application/json"} if body else {})
            r = h.getresponse()
            raw = r.read()
            h.close()
            return r.status, raw

        code, raw = call("GET", "/api/health")
        d = json.loads(raw)
        assert code == 200, code
        assert d["corpus"]["documents"] == 3, d["corpus"]
        assert d["corpus"]["subjects"] == 2, d["corpus"]
        assert d["device"] == {"reachable": True, "chat_model": "fake-chat"}, d["device"]
        assert {s["name"] for s in d["shelves"]} == {"survival shelf", "encyclopaedia"}

        code, raw = call("POST", "/api/ask", {"q": "how do I purify water"})
        a = json.loads(raw)
        assert code == 200, code
        # the passthrough: every key ask() produced, under its own name, plus two
        assert set(a) == set(ANSWER) | {"asked", "q", "secs"}, sorted(a)
        assert a["q"] == "how do I purify water" and isinstance(a["secs"], float)

        assert call("POST", "/api/ask", {"q": "  "})[0] == 400
        assert call("POST", "/api/ask", {"q": "a" * (MAX_QUESTION + 1)})[0] == 400

        code, raw = call("GET", "/api/books?shelf=survival%20shelf")
        b = json.loads(raw)
        assert code == 200 and [x["title"] for x in b["books"]] == [
            "Field Guide to Fire", "Manual of Water"], b
        assert call("GET", "/api/books?shelf=nothing")[0] == 404

        code, raw = call("GET", "/api/subjects?limit=5")
        s = json.loads(raw)["subjects"]
        assert {x["name"] for x in s} == {"boil", "tourniquet"}, s
        assert call("GET", "/api/subject?name=boil")[0] == 200
        assert call("GET", "/api/subject?name=nobody")[0] == 404

        assert call("GET", "/../lastlight.py")[0] == 404
        assert json.loads(call("GET", "/nope")[1]) == {"error": "not found"}

        # the box goes away mid-session: no answer, no model call, and a sentence
        globals()["_probe"] = lambda url, key, timeout: False
        _device_seen["at"], _device_seen["state"] = 0.0, None
        code, raw = call("POST", "/api/ask", {"q": "how do I purify water"})
        assert code == 503 and json.loads(raw) == {"error": UNREACHABLE}, raw

        page = PAGE.read_bytes() if PAGE.exists() else b""
        if page:
            assert page.count(b"<title>") == 1, "the page needs exactly one title"
            assert b"/api/ask" in page, "the page has no way to reach the Archivist"
            assert b"LAST LIGHT" in page, "the page does not name itself"
        print(f"  lastlight: 3 books on 2 shelves, 2 subjects, every route "
              f"answered, ask passed through {len(ANSWER)} keys"
              f"{'' if page else ', page not built yet'}")
        print("\n  selfcheck passed")
        return 0
    finally:
        if srv:
            srv.shutdown()
            srv.server_close()
        restore(was)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(selfcheck())
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 8700)
