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
            "GROUP BY m.entity_id ORDER BY n DESC LIMIT ?",
            (shelf, max(40, limit - 60))).fetchall()
        ids = {r["eid"] for r in rows if r["eid"] in ents}
        strength = {r["eid"]: r["n"] for r in rows}
        # One ring of everything the project touches but does not own, so the
        # project reads as a shape against a background instead of filling the
        # screen with no edge to it.
        ring = set()
        for (x, y) in kept:
            if x in ids and y not in ids:
                ring.add(y)
            elif y in ids and x not in ids:
                ring.add(x)
        ring = set(sorted(ring, key=lambda i: -ents[i]["deg"])[:60])
        nodes = [dict(ents[i], inshelf=1) for i in ids]
        nodes += [dict(ents[i], inshelf=0) for i in ring if i in ents]
        edges = [{"a": x, "b": y, "w": round(w, 2), "n": n}
                 for (x, y), (w, n) in kept.items()
                 if (x in ids or x in ring) and (y in ids or y in ring)]
        # The project itself, as a node. It gives the cluster something to orbit
        # and something to centre on - a folder is a real thing in this archive,
        # it simply had no dot before.
        pid = -1
        nodes.append({"id": pid, "name": shelf, "docs": max(20, len(ids)),
                      "mentions": sum(strength.values()), "deg": len(ids),
                      "inshelf": 1, "isproject": 1})
        for eid in sorted(ids, key=lambda i: -strength.get(i, 0))[:28]:
            edges.append({"a": pid, "b": eid,
                          "w": 3.0, "n": strength.get(eid, 1)})
        return {"nodes": nodes, "edges": edges, "total_entities": len(ents),
                "total_edges": len(kept), "notes": n_docs, "focus": shelf,
                "shelf": shelf}
    if focus:
        hit = next((e for e in ents.values()
                    if e["name"].lower() == focus.lower()), None) \
            or next((e for e in ents.values()
                     if focus.lower() in e["name"].lower()), None)
        if hit:
            seeds = [hit["id"]]
    if not seeds:
        # Seed on the best-connected names, not the strongest edges. PMI is the
        # right weight for an edge - it strips the bias toward names that appear
        # everywhere - but it is exactly the wrong way to choose where to start,
        # because it peaks on the rarest pairs. Two names in three notes that
        # always co-occur score higher than anything real, so seeding that way
        # opens on the most obscure corner of the archive and the crawl stalls
        # there for want of edges to follow.
        seeds = [e["id"] for e in
                 sorted(ents.values(), key=lambda e: -e["deg"])[:10]]
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


def ask_doc(c, doc_id, question):
    """Answer about one note, from that note.

    Retrieval is the wrong tool when the document is already in front of you:
    it would fetch the passages most like the question from anywhere in the
    archive, when what was asked was about this note. Hand over the note.
    """
    import archivist
    d = note(c, doc_id)
    if "error" in d:
        return d
    model, base, key = archivist.pick_chat()
    body = {"model": model, "max_tokens": 700, "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {"role": "system", "content":
                 "You are answering questions about one note from someone's own "
                 "notes, reproduced below. Answer from it and nothing else. If "
                 "the note does not say, say that it does not say - do not "
                 "reach for what you know about the subject generally."},
                {"role": "user", "content":
                 f"NOTE: {d['title']} ({d['source']})\n\n{d['text'][:24000]}\n\n"
                 f"QUESTION: {question}"}]}
    try:
        r = archivist.api("/v1/chat/completions", body, timeout=420,
                          base=base, key=key)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
    return {"answer": (r["choices"][0]["message"].get("content") or "").strip(),
            "scope": d["title"]}


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


VAULT_CFG = pathlib.Path(os.environ.get("BRAIN_VAULT_CONFIG") or
                         (pathlib.Path.home() / ".config" / "tiiny-brain.json"))


def vault_root():
    """Where new notes are written.

    Not a vault format of our own. New notes are markdown files in a folder,
    which means an Obsidian user watches them appear in Obsidian and everyone
    else just gets a folder. The promise was that your notes stay yours and stay
    where they are; inventing a database to hold them would break it on the
    first write.
    """
    try:
        v = json.loads(VAULT_CFG.read_text()).get("vault")
        if v and pathlib.Path(v).is_dir():
            return pathlib.Path(v)
    except Exception:  # noqa: BLE001
        pass
    d = pathlib.Path.home() / "brain" / "notes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_vault(path):
    d = pathlib.Path(path).expanduser()
    if not d.is_dir():
        return {"error": f"{d} is not a directory"}
    VAULT_CFG.parent.mkdir(parents=True, exist_ok=True)
    VAULT_CFG.write_text(json.dumps({"vault": str(d)}, indent=1))
    return {"ok": True, "vault": str(d)}


def new_note(c, title, text, shelf=""):
    """Write a note to disk, then bring just that note into the archive."""
    import archivist
    title = " ".join((title or "").split())[:120]
    if not title or not (text or "").strip():
        return {"error": "a note needs a title and something in it"}
    root = vault_root()
    folder = root / shelf if shelf else root
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in title if ch not in '\\/:*?"<>|').strip() or "note"
    path = folder / (safe + ".md")
    i = 2
    while path.exists():
        path = folder / f"{safe} ({i}).md"
        i += 1
    body = text if text.lstrip().startswith("#") else f"# {title}\n\n{text}"
    path.write_text(body, encoding="utf-8")

    key = f"brain#{path}"
    try:
        cur = c.execute(
            "INSERT INTO doc(path,title,source,pages,sha,added_at) VALUES(?,?,?,?,?,?)",
            (key, title, shelf or "(root)", 1,
             __import__("hashlib").sha1(key.encode()).hexdigest()[:16],
             archive.now()))
    except sqlite3.IntegrityError:
        return {"error": "a note with that path is already in the archive"}
    doc_id = cur.lastrowid
    c.execute("INSERT INTO page(doc_id,page_no,status,engine,conf,chars,text,done_at) "
              "VALUES(?,1,'text','written',1.0,?,?,?)",
              (doc_id, len(text), text, archive.now()))
    c.commit()
    made = archive.chunk_doc(c, doc_id)

    embedded = 0
    try:
        rows = c.execute("SELECT ch.id, ch.text FROM chunk ch "
                         "LEFT JOIN vec v ON v.chunk_id=ch.id "
                         "WHERE ch.doc_id=? AND v.chunk_id IS NULL", (doc_id,)).fetchall()
        if rows:
            archivist._schema(c)
            vecs = archivist.embed([r["text"][:2000] for r in rows])
            for r, v in zip(rows, vecs):
                arr = archivist.norm(v)
                c.execute("INSERT OR REPLACE INTO vec(chunk_id,dim,v) VALUES(?,?,?)",
                          (r["id"], len(arr),
                           __import__("struct").pack(f"{len(arr)}f", *arr)))
            c.commit()
            embedded = len(rows)
    except Exception as e:  # noqa: BLE001 - the note is safe on disk either way
        return {"ok": True, "id": doc_id, "path": str(path), "chunks": made,
                "embedded": 0, "warning": f"written and indexed, not embedded: {str(e)[:120]}"}
    _graph._cache = None          # the entity graph is stale now
    return {"ok": True, "id": doc_id, "path": str(path),
            "chunks": made, "embedded": embedded}


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
        if u.path == "/api/vault":
            return self._json({"vault": str(vault_root())})
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
        if u.path == "/api/note/new":
            r = new_note(_db(), body.get("title"), body.get("text") or "",
                         (body.get("shelf") or "").strip())
            return self._json(r, 200 if r.get("ok") else 400)
        if u.path == "/api/vault":
            return self._json(set_vault(body.get("path") or ""))
        if u.path == "/api/ask":
            qn = (body.get("q") or "").strip()
            if not qn:
                return self._json({"error": "ask something"}, 400)
            doc = body.get("doc")
            if doc:
                return self._json(ask_doc(_db(), int(doc), qn))
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
