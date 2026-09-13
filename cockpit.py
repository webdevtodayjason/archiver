#!/usr/bin/env python3
"""The cockpit: a bounded window onto an archive's entity graph.

The graph does not fit. 6,162 entities over 2,382 notes make 207,018
co-occurrence edges, and no amount of layout makes that legible. Three things
cut it to something a person can read, in this order, because each one is
cheaper than the next:

  1. A document-frequency ceiling. A name in a quarter of all notes is a section
     heading, not an entity - "Known Gotchas" carried 1,951 edges on its own.
  2. Pointwise mutual information. Raw co-occurrence rewards ubiquity: Jason
     appears with everything, so Jason-with-anything means nothing. PMI asks
     whether two names appear together more than chance would predict, which is
     how SHAKEN and STIR rise above API and CLI.
  3. Each node keeps only its strongest few edges, so one hub cannot flood the
     view and nothing ends up stranded.

What is drawn is then a window over that: the best-connected slice, or the
neighbourhood of whatever you asked about.
"""
import collections
import json
import math
import os
import pathlib
import sqlite3
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import archive

HERE = pathlib.Path(__file__).resolve().parent
STATIC = HERE / "static"

DF_CEILING = 0.12     # named in more than this share of notes -> boilerplate
MIN_SHARED = 3        # notes two entities must share before it is an edge
TOP_EDGES = 6         # strongest edges kept per entity


def _db():
    c = archive.db()
    c.row_factory = sqlite3.Row
    return c


def _graph(c):
    """(nodes, edges) after the three cuts. Cached per process - it is a scan."""
    if getattr(_graph, "_cache", None):
        return _graph._cache
    n_docs = c.execute("SELECT COUNT(*) FROM doc").fetchone()["c"] \
        if False else c.execute("SELECT COUNT(*) n FROM doc").fetchone()["n"]
    ceiling = max(3, int(n_docs * DF_CEILING))
    ents = {r["id"]: {"id": r["id"], "name": r["name"], "docs": r["docs"],
                      "mentions": r["mentions"]}
            for r in c.execute("SELECT id,name,docs,mentions FROM entity")
            if r["docs"] <= ceiling}
    raw = c.execute(
        "SELECT a.entity_id x, b.entity_id y, COUNT(DISTINCT a.doc_id) n "
        "FROM mention a JOIN mention b "
        "ON a.doc_id=b.doc_id AND a.entity_id<b.entity_id "
        "GROUP BY a.entity_id,b.entity_id HAVING n>=?", (MIN_SHARED,)).fetchall()
    scored = []
    for r in raw:
        x, y = r["x"], r["y"]
        if x not in ents or y not in ents:
            continue
        px = ents[x]["docs"] / n_docs
        py = ents[y]["docs"] / n_docs
        pxy = r["n"] / n_docs
        if px <= 0 or py <= 0:
            continue
        s = math.log(pxy / (px * py))
        if s > 0:
            scored.append((s, x, y, r["n"]))
    by = collections.defaultdict(list)
    for e in scored:
        by[e[1]].append(e)
        by[e[2]].append(e)
    kept = {}
    for eid, es in by.items():
        for s, x, y, n in sorted(es, key=lambda t: -t[0])[:TOP_EDGES]:
            kept[(x, y)] = (s, n)
    deg = collections.Counter()
    for (x, y) in kept:
        deg[x] += 1
        deg[y] += 1
    for eid in ents:
        ents[eid]["deg"] = deg.get(eid, 0)
    _graph._cache = (ents, kept, n_docs)
    return _graph._cache


def _adj(kept):
    a = collections.defaultdict(list)
    for (x, y), (w, n) in kept.items():
        a[x].append((w, y))
        a[y].append((w, x))
    return a


def bridges(ents, kept, n=40):
    """Entities that connect otherwise separate neighbourhoods.

    A second brain is mostly noise by design - it is everything you wrote. The
    interesting thing is rarely the biggest node, which you already know about.
    It is the name sitting between two clusters that have nothing else in
    common, because that is a connection you made once and have not noticed.
    Cheap proxy for betweenness: strong edges whose two ends share almost no
    other neighbours.
    """
    a = _adj(kept)
    nb = {e: {y for _, y in a[e]} for e in a}
    out = []
    for (x, y), (w, cnt) in kept.items():
        ox, oy = nb.get(x, set()), nb.get(y, set())
        if len(ox) < 2 or len(oy) < 2:
            continue
        shared = len(ox & oy)
        union = len(ox | oy) or 1
        # high weight, low overlap = a link between two different worlds
        out.append((w * (1 - shared / union), x, y, w, cnt, shared))
    out.sort(reverse=True)
    return out[:n]


def window(c, focus=None, limit=220, shelf=None):
    """The slice to draw.

    Not "the top N entities" - those are the ones you already know about, and
    they are not connected to each other, so it renders as scattered dots.
    Grow outward from strong edges instead, so what appears is a piece of graph
    with shape, whatever it is centred on.
    """
    ents, kept, n_docs = _graph(c)
    a = _adj(kept)
    seeds = []
    if shelf:
        # A project is a cluster, not a node. Its constellation is every entity
        # named in its notes - which is the shape of the work, drawn from the
        # filing the author already did rather than from clustering.
        rows = c.execute(
            "SELECT m.entity_id eid, COUNT(DISTINCT m.doc_id) n FROM mention m "
            "JOIN doc d ON d.id=m.doc_id WHERE d.source=? "
            "GROUP BY m.entity_id ORDER BY n DESC LIMIT ?", (shelf, limit)).fetchall()
        ids = {r["eid"] for r in rows if r["eid"] in ents}
        nodes = [dict(ents[i], inshelf=1) for i in ids]
        edges = [{"a": x, "b": y, "w": round(w, 2), "n": n}
                 for (x, y), (w, n) in kept.items() if x in ids and y in ids]
        return {"nodes": nodes, "edges": edges, "total_entities": len(ents),
                "total_edges": len(kept), "notes": n_docs, "focus": "",
                "shelf": shelf}
    if focus:
        hit = next((e for e in ents.values()
                    if e["name"].lower() == focus.lower()), None) \
            or next((e for e in ents.values()
                     if focus.lower() in e["name"].lower()), None)
        if hit:
            seeds = [hit["id"]]
    if not seeds:
        # start from the strongest association in the archive, not the loudest
        # node, then grow: the result is a connected region rather than a list.
        top = sorted(kept.items(), key=lambda kv: -kv[1][0])[:12]
        seeds = [x for (x, y), _ in top] + [y for (x, y), _ in top]
    ids, frontier = set(seeds), list(seeds)
    while frontier and len(ids) < limit:
        cur = frontier.pop(0)
        for w, other in sorted(a.get(cur, []), reverse=True)[:TOP_EDGES]:
            if other not in ids and len(ids) < limit:
                ids.add(other)
                frontier.append(other)
    nodes = [ents[i] for i in ids if i in ents]
    edges = [{"a": x, "b": y, "w": round(w, 2), "n": n}
             for (x, y), (w, n) in kept.items() if x in ids and y in ids]
    return {"nodes": nodes, "edges": edges,
            "total_entities": len(ents), "total_edges": len(kept),
            "notes": n_docs, "focus": focus or ""}


def vitals(c):
    q = lambda s: c.execute(s).fetchone()[0]
    ents, kept, n_docs = _graph(c)
    return {
        "notes": q("SELECT COUNT(*) FROM doc"),
        "chunks": q("SELECT COUNT(*) FROM chunk"),
        "vectors": q("SELECT COUNT(*) FROM vec") if c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vec'"
        ).fetchone() else 0,
        "entities": q("SELECT COUNT(*) FROM entity"),
        "mentions": q("SELECT COUNT(*) FROM mention"),
        "drawable_entities": len(ents),
        "drawable_edges": len(kept),
        "shelves": [dict(r) for r in c.execute(
            "SELECT source AS name, COUNT(*) n FROM doc GROUP BY source "
            "ORDER BY n DESC LIMIT 8")],
    }


def entity(c, name):
    row = c.execute("SELECT id,name,docs,mentions FROM entity "
                    "WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if not row:
        return {"error": f"no entity called {name!r}"}
    notes = [dict(r) for r in c.execute(
        "SELECT DISTINCT d.id, d.title, d.source FROM mention m "
        "JOIN doc d ON d.id=m.doc_id WHERE m.entity_id=? LIMIT 24", (row["id"],))]
    ents, kept, _ = _graph(c)
    near = []
    for (x, y), (s, n) in kept.items():
        other = y if x == row["id"] else (x if y == row["id"] else None)
        if other is not None and other in ents:
            near.append({"name": ents[other]["name"], "w": round(s, 2), "n": n})
    near.sort(key=lambda d: -d["w"])
    return {"name": row["name"], "docs": row["docs"], "mentions": row["mentions"],
            "notes": notes, "near": near[:14]}


def shelves(c):
    """The folders a vault was organised into are already its project list.

    Nobody filed 2,382 notes at random. The directory a note sits in is the
    author's own taxonomy, arrived at over years, and it is better than anything
    clustering would recover. Surfacing it as navigation costs one GROUP BY.
    """
    return [dict(r) for r in c.execute(
        "SELECT source AS name, COUNT(*) notes, SUM(pages) pages "
        "FROM doc GROUP BY source ORDER BY notes DESC")]


def shelf(c, name, limit=200):
    notes = [dict(r) for r in c.execute(
        "SELECT id, title FROM doc WHERE source=? ORDER BY title LIMIT ?",
        (name, limit))]
    top = [dict(r) for r in c.execute(
        "SELECT e.name, COUNT(DISTINCT m.doc_id) n FROM mention m "
        "JOIN entity e ON e.id=m.entity_id JOIN doc d ON d.id=m.doc_id "
        "WHERE d.source=? GROUP BY e.id ORDER BY n DESC LIMIT 18", (name,))]
    return {"name": name, "notes": notes, "entities": top}


def note(c, doc_id):
    d = c.execute("SELECT id,title,source,path FROM doc WHERE id=?", (doc_id,)).fetchone()
    if not d:
        return {"error": "no such note"}
    text = "\n\n".join(r["text"] or "" for r in c.execute(
        "SELECT text FROM page WHERE doc_id=? ORDER BY page_no", (doc_id,)))
    return {"id": d["id"], "title": d["title"], "source": d["source"],
            "path": d["path"], "text": text[:60000]}


def ask(c, question, focus=""):
    """Put the question to the archive, narrowed to an entity when one is in view.

    Without this the cockpit is a map with no way to ask it anything: you can
    see that HoLaCe is named in 68 notes and still have no route to what they
    say. Focus supplies the context the question was asked inside.
    """
    import archivist
    q = f"{focus}: {question}" if focus else question
    try:
        r = archivist.ask(q, show_sources=False, quiet=True)
    except SystemExit as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
    return {"answer": r.get("answer"), "refused": r.get("refused"),
            "sources": r.get("sources") or [], "cited": r.get("cited") or []}


class Handler(BaseHTTPRequestHandler):
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

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        c = _db()
        if u.path == "/":
            return self._send(200, (STATIC / "cockpit.html").read_bytes(),
                              "text/html; charset=utf-8")
        if u.path == "/api/vitals":
            return self._json(vitals(c))
        if u.path == "/api/graph":
            return self._json(window(c, (q.get("focus") or [""])[0],
                                     int((q.get("limit") or ["220"])[0]),
                                     (q.get("shelf") or [""])[0] or None))
        if u.path == "/api/bridges":
            ents, kept, _ = _graph(c)
            return self._json([
                {"a": ents[x]["name"], "b": ents[y]["name"],
                 "w": round(w, 2), "shared_notes": cnt, "common": sh}
                for _, x, y, w, cnt, sh in bridges(ents, kept, 30)])
        if u.path == "/api/shelves":
            return self._json(shelves(c))
        if u.path == "/api/shelf":
            return self._json(shelf(c, (q.get("name") or [""])[0]))
        if u.path == "/api/note":
            return self._json(note(c, int((q.get("id") or ["0"])[0])))
        if u.path == "/api/entity":
            return self._json(entity(c, (q.get("name") or [""])[0]))
        return self._send(404, "no such path", "text/plain")

    def do_POST(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or "{}")
        except ValueError:
            body = {}
        if u.path == "/api/ask":
            qn = (body.get("q") or "").strip()
            if not qn:
                return self._json({"error": "ask something"}, 400)
            return self._json(ask(_db(), qn, (body.get("focus") or "").strip()))
        return self._send(404, "no such path", "text/plain")


def run(port=8500):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"\n  cockpit   http://127.0.0.1:{port}/")
    print(f"  archive   {archive.HOME}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye")


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 8500)
