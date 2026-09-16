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
import hashlib
import json
import math
import os
import pathlib
import re
import sqlite3
import struct
import sys
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import archive
import overview

HERE = pathlib.Path(__file__).resolve().parent
STATIC = HERE / "static"

DF_CEILING = 0.12     # named in more than this share of notes -> boilerplate
MIN_SHARED = 3        # notes two entities must share before it is an edge
MIN_SHARED_CHUNKS = 8  # ...chunks, when the unit is a chunk. See _shape().
TOP_EDGES = 6         # strongest edges kept per entity


def _db():
    c = archive.db()
    c.row_factory = sqlite3.Row
    return c


def _shape(c):
    """What kind of archive this is, measured off the corpus rather than configured.

    Two of the reduction's assumptions are properties of a note vault rather
    than of archives in general. They are asked separately, because a corpus can
    be one and not the other.

    Co-occurrence needs a unit of roughly constant size that is about one thing.
    In a vault that unit is the note, so two names in one note means something.
    A 676-page survival manual is not about one thing: at document scale every
    term in it co-occurs with every other, and on the LAST LIGHT corpus that
    made "urethral - thrombosis" a maximum-PMI edge, because three medical
    manuals each contain both words somewhere in five hundred pages. Every edge
    in that graph sat on the MIN_SHARED floor and the ten highest-degree names
    were each in three documents. Chunks are the note-sized unit a library
    already has, so when most of the corpus's pages sit inside multi-page
    documents, co-occur over chunks instead.

    Which population to draw is the second question. entities.py maintains two
    in one table, built by different extractors: capitalised names (kind NULL)
    and lowercase subjects (kind 'subject'). Their base rates are nothing alike
    - on LAST LIGHT a subject averages 143 mentions and a name 15 - so a PMI
    taken across both is comparing two different measurements, and the larger
    population wins on volume rather than on meaning. Draw one. Running
    `entities.py subjects` over a corpus is a statement that its proper nouns
    were not the answer, so where that index exists it is the one to draw.
    """
    p = c.execute("SELECT COALESCE(SUM(pages),0) all_p, "
                  "COALESCE(SUM(CASE WHEN pages>1 THEN pages END),0) book_p "
                  "FROM doc").fetchone()
    unit = "chunk_id" if p["all_p"] and p["book_p"] * 2 > p["all_p"] else "doc_id"
    subj = c.execute(
        "SELECT COUNT(*) n FROM entity WHERE kind='subject'").fetchone()["n"] > 0
    return unit, subj


def _graph(c):
    """(nodes, edges) after the three cuts. Cached per process - it is a scan."""
    if getattr(_graph, "_cache", None):
        return _graph._cache
    n_docs = c.execute("SELECT COUNT(*) n FROM doc").fetchone()["n"]
    unit, subj = _shape(c)
    n_units = n_docs if unit == "doc_id" else \
        c.execute("SELECT COUNT(*) n FROM chunk").fetchone()["n"]
    ceiling = max(3, int(n_docs * DF_CEILING))
    ents = {r["id"]: {"id": r["id"], "name": r["name"], "docs": r["docs"],
                      "mentions": r["mentions"]}
            for r in c.execute("SELECT id,name,docs,mentions,kind FROM entity")
            if r["docs"] <= ceiling and (r["kind"] == "subject") == subj}
    # PMI wants each entity's frequency in the unit being counted, and
    # entity.docs is documents whichever unit that is. The two agree row for row
    # when the unit is the document, so this costs the vault nothing.
    freq = {r["e"]: r["n"] for r in c.execute(
        f"SELECT entity_id e, COUNT(DISTINCT {unit}) n FROM mention "
        "GROUP BY entity_id")}
    # DISTINCT before the join, not COUNT(DISTINCT) after it. mention carries a
    # row per chunk, so a corpus whose largest document holds 19,750 of them
    # feeds the self-join 1.9 billion rows to produce the same 1.5 million
    # answers: 834 seconds against 3 on an M5 Max. COUNT(DISTINCT a.doc_id) was
    # already collapsing those duplicates, it was just paying for them first,
    # which is why the result is unchanged and only the bill moved.
    raw = c.execute(
        f"WITH m AS (SELECT DISTINCT entity_id, {unit} u FROM mention) "
        "SELECT a.entity_id x, b.entity_id y, COUNT(*) n "
        "FROM m a JOIN m b ON a.u=b.u AND a.entity_id<b.entity_id "
        "GROUP BY a.entity_id,b.entity_id HAVING n>=?",
        (MIN_SHARED if unit == "doc_id" else MIN_SHARED_CHUNKS,)).fetchall()
    scored = []
    for r in raw:
        x, y = r["x"], r["y"]
        if x not in ents or y not in ents:
            continue
        px = freq.get(x, 0) / n_units
        py = freq.get(y, 0) / n_units
        pxy = r["n"] / n_units
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


def bridges(ents, kept, n=40, sources=300, seed=7):
    """Entities that sit on the paths between otherwise separate neighbourhoods.

    A second brain is mostly noise by design, because it is everything you
    wrote. The interesting name is rarely the biggest one, which you already
    know about. It is the one standing between two clusters that have nothing
    else in common, because that is a connection you made once and have not
    noticed since.

    That is betweenness, and it is worth computing properly. The previous
    version scored edges by how little their two ends had in common, weighted by
    edge strength. Those weights come from a PMI-like measure that peaks on
    rarity, so it reliably surfaced two names that had co-occurred twice in one
    odd note, which is the opposite of a bridge: a bridge carries traffic.

    Brandes' algorithm, on the unweighted shape of the kept graph. Sampled from
    a fixed set of sources rather than every node, because exact betweenness is
    a BFS per node and this answers a web request; 300 sources over a couple of
    thousand nodes ranks the top of the list the same way exhaustive does, and
    the seed is fixed so two calls agree. Scores are relative, so they are
    returned normalised against the highest.
    """
    import random
    a = _adj(kept)
    nodes = list(a)
    if not nodes:
        return []
    # strongest first, so the neighbours reported for a bridge are the ones
    # carrying the traffic rather than whichever names sort early
    nb = {v: [y for _, y in sorted(a[v], reverse=True)] for v in nodes}
    pick = nodes if len(nodes) <= sources else random.Random(seed).sample(nodes, sources)

    cb = dict.fromkeys(nodes, 0.0)
    for s0 in pick:
        stack, pred = [], {v: [] for v in nodes}
        sigma = dict.fromkeys(nodes, 0.0); sigma[s0] = 1.0
        dist = dict.fromkeys(nodes, -1); dist[s0] = 0
        q = collections.deque([s0])
        while q:
            v = q.popleft(); stack.append(v)
            dv = dist[v]
            for w in nb[v]:
                if dist[w] < 0:
                    dist[w] = dv + 1
                    q.append(w)
                if dist[w] == dv + 1:
                    sigma[w] += sigma[v]
                    pred[w].append(v)
        delta = dict.fromkeys(nodes, 0.0)
        while stack:
            w = stack.pop()
            coeff = (1.0 + delta[w]) / sigma[w]
            for v in pred[w]:
                delta[v] += sigma[v] * coeff
            if w != s0:
                cb[w] += delta[w]

    top = sorted(cb.items(), key=lambda kv: -kv[1])[:n]
    best = top[0][1] or 1.0
    return [{"name": ents[v]["name"],
             "score": round(c / best, 4),
             "degree": len(nb[v]),
             "docs": ents[v].get("docs"),
             "between": [ents[y]["name"] for y in nb[v][:6]]}
            for v, c in top if c > 0]


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
        # The page calls a document a note, a shelf a project and offers to
        # write one, all of which are true of a vault and none of a library of
        # scanned books. One flag, decided the same way the graph decides.
        "library": _shape(c)[0] == "chunk_id",
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


def shelf(c, name, limit=200, q=""):
    """What is on one shelf, and honestly how much of it this is.

    A vault shelf is a project of 90 notes and the list was the whole of it. The
    Vikidia shelf is 5,934 articles, so the same LIMIT 200 silently showed 3% of
    it in alphabetical order - everything from A to Ba. The count is returned so
    the page can say so, and a filter is the only way to reach the rest.
    """
    where, args = "WHERE source=?", [name]
    if q:
        where += " AND title LIKE ? COLLATE NOCASE"
        args.append(f"%{q}%")
    total = c.execute(f"SELECT COUNT(*) n FROM doc {where}", args).fetchone()["n"]
    # Longest first. Every note is one page, so this is title order in a vault,
    # but on a shelf of books it puts the books above the one-page stubs.
    notes = [dict(r) for r in c.execute(
        f"SELECT id, title, pages FROM doc {where} ORDER BY pages DESC, title "
        "LIMIT ?", args + [limit])]
    _, subj = _shape(c)
    kind = "e.kind='subject'" if subj else "(e.kind IS NULL OR e.kind<>'subject')"
    top = [dict(r) for r in c.execute(
        "SELECT e.name, COUNT(DISTINCT m.doc_id) n FROM mention m "
        "JOIN entity e ON e.id=m.entity_id JOIN doc d ON d.id=m.doc_id "
        f"WHERE d.source=? AND {kind} GROUP BY e.id ORDER BY n DESC LIMIT 18",
        (name,))]
    return {"name": name, "notes": notes, "entities": top,
            "total": total, "shown": len(notes), "q": q}


def note(c, doc_id, first=1, budget=60000):
    """A document's text from page `first`, about `budget` characters of it.

    A note is one page and comes back whole. A book does not: Nuclear War
    Survival Skills is 1,087,591 characters over 510 pages, and a flat
    text[:60000] returned 5.5% of it with no page numbers and nothing saying it
    had stopped. That is the failure this repo cares about most - it reads
    exactly like the whole book. So pages are marked, whole pages are the unit,
    and the caller is told which ones it is holding and whether there are more.
    """
    d = c.execute("SELECT id,title,source,path FROM doc WHERE id=?",
                  (doc_id,)).fetchone()
    if not d:
        return {"error": "no such note"}
    last = c.execute("SELECT COALESCE(MAX(page_no),1) n FROM page WHERE doc_id=?",
                     (doc_id,)).fetchone()["n"]
    out, used, to = [], 0, first - 1
    for r in c.execute("SELECT page_no, text FROM page WHERE doc_id=? AND page_no>=? "
                       "ORDER BY page_no", (doc_id, first)):
        t = r["text"] or ""
        head = f"[p{r['page_no']}]\n\n" if last > 1 else ""
        if out and used + len(t) + len(head) > budget:
            break
        out.append(head + t)
        used += len(t) + len(head)
        to = r["page_no"]
    return {"id": d["id"], "title": d["title"], "source": d["source"],
            "path": d["path"],
            # the slice still gets a hard cap, because one page can be longer
            # than the budget and a vault note always was capped here
            "text": "\n\n".join(out)[:budget],
            "pages": last, "from": first, "to": max(to, first), "more": to < last}


def _passages(c, doc_id, question, budget=24000):
    """(text, whole) - what of one document to put in front of the model.

    A note fits in a prompt entire and is sent entire. A 510-page book does not,
    and the old text[:24000] answered questions about Nuclear War Survival
    Skills out of its front matter while the system prompt swore the document
    was reproduced below. That is this repo's worst failure mode: an answer
    drawn from 5% of a book reads exactly like an answer drawn from the book.

    So when it does not fit, the document's own chunks are ranked against the
    question and the best of them are sent in page order, each headed by its
    page. Lexical rather than vector: the field is already one document, the
    device runs one inference at a time, and an embedding call to rank two
    hundred chunks of one book is not worth its place in that queue.
    """
    rows = c.execute("SELECT text, page_from, page_to FROM chunk WHERE doc_id=? "
                     "ORDER BY page_from, id", (doc_id,)).fetchall()
    if not rows:
        return "", True
    if sum(len(r["text"]) for r in rows) <= budget:
        return "\n\n".join(r["text"] for r in rows), True

    import entities
    terms = {w for w in re.findall(r"[a-z][a-z'-]{2,}", question.lower())
             if w not in entities.STOP}
    n = len(rows)
    low = [r["text"].lower() for r in rows]
    df = collections.Counter()
    for t in terms:
        df[t] = sum(1 for s in low if t in s)
    scored = []
    for i, s in enumerate(low):
        # length-normalised, or a long chunk wins every question by holding
        # more of every word
        hit = sum(math.log(1 + n / (1 + df[t])) * s.count(t)
                  for t in terms if df[t])
        scored.append((hit / math.sqrt(len(s) + 1), i))
    scored.sort(reverse=True)
    take, used = [], 0
    for s, i in scored:
        if s <= 0:
            break
        if used + len(rows[i]["text"]) > budget:
            continue
        take.append(i)
        used += len(rows[i]["text"])
    if not take:                      # nothing matched: the opening is the honest default
        take = list(range(min(n, 6)))
    take.sort()
    out = []
    for i in take:
        r = rows[i]
        p = (f"[p{r['page_from']}]" if r["page_from"] == r["page_to"]
             else f"[pp{r['page_from']}-{r['page_to']}]")
        out.append(f"{p}\n{r['text']}")
    return "\n\n[...]\n\n".join(out), False


def ask_doc(c, doc_id, question):
    """Answer about one note, from that note.

    Retrieval is the wrong tool when the document is already in front of you:
    it would fetch the passages most like the question from anywhere in the
    archive, when what was asked was about this note. Hand over the note.
    """
    import archivist
    d = c.execute("SELECT id,title,source FROM doc WHERE id=?", (doc_id,)).fetchone()
    if not d:
        return {"error": "no such note"}
    text, whole = _passages(c, doc_id, question)
    model, base, key = archivist.pick_chat()
    # The prompt has to describe what it was actually given. A vault note is
    # always whole, so that wording is left exactly as it was.
    system = ("You are answering questions about one note from someone's own "
              "notes, reproduced below. Answer from it and nothing else. If "
              "the note does not say, say that it does not say - do not "
              "reach for what you know about the subject generally.") if whole else \
             ("You are answering questions about one document from an archive. "
              "Below are the passages of it that match the question, each "
              "headed by the page it is on; the rest of the document is not "
              "shown. Answer from these passages and nothing else, and cite the "
              "page. If they do not say, say that they do not say, and say that "
              "the rest of the document was not searched - do not reach for "
              "what you know about the subject generally.")
    body = {"model": model, "max_tokens": 700, "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content":
                 f"NOTE: {d['title']} ({d['source']})\n\n{text}\n\n"
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
    if focus.strip().lower() == "everything":
        focus = ""          # a UI label, not an entity: prepending it poisons the query

    # "who is Richard" is not a retrieval question. Nearest-neighbour search hands
    # back whichever notes happen to name him near matching words; what answers it
    # is every passage that names him at all. When the question is that shape and
    # the name is one the archive actually knows, take the mention path instead.
    import entities
    named = entities.asked_about(question)
    if named:
        d = entities.profile(named)
        if not d.get("error"):
            return {"answer": d["answer"], "refused": False,
                    "sources": d["sources"][:8], "cited": [],
                    "via": f"named in {d['docs']} notes"}

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
    _save_cfg(vault=str(d))
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
             hashlib.sha1(key.encode()).hexdigest()[:16],
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
                           struct.pack(f"{len(arr)}f", *arr)))
            c.commit()
            embedded = len(rows)
    except Exception as e:  # noqa: BLE001 - the note is safe on disk either way
        return {"ok": True, "id": doc_id, "path": str(path), "chunks": made,
                "embedded": 0, "warning": f"written and indexed, not embedded: {str(e)[:120]}"}
    _graph._cache = None          # the entity graph is stale now
    return {"ok": True, "id": doc_id, "path": str(path),
            "chunks": made, "embedded": embedded}


def _cfg():
    try:
        return json.loads(VAULT_CFG.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save_cfg(**kw):
    c = _cfg()
    c.update({k: v for k, v in kw.items() if v is not None})
    VAULT_CFG.parent.mkdir(parents=True, exist_ok=True)
    VAULT_CFG.write_text(json.dumps(c, indent=1))
    try:
        VAULT_CFG.chmod(0o600)
    except OSError:
        pass
    return c


def probe(url, key=None, timeout=4):
    """Does this endpoint answer, and what does it say. Never raises."""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"ok": True, "code": r.status}
    except urllib.error.HTTPError as e:
        # 401 means it is there and wants a key, which is still "reachable"
        return {"ok": e.code in (401, 403), "code": e.code}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": 0, "error": str(e)[:90]}


def apply_cfg():
    """Carry saved settings into this process. Env always wins."""
    import archivist
    c = _cfg()
    if c.get("host") and not os.environ.get("TIINY_HOST"):
        archivist.HOST = c["host"]
    if c.get("port") and not os.environ.get("TIINY_PORT"):
        archivist.PORT = str(c["port"])
    if c.get("key") and not os.environ.get("TIINY_KEY"):
        archivist.KEY = c["key"]
    if c.get("chat_url") and not os.environ.get("LASTLIGHT_CHAT_URL"):
        os.environ["LASTLIGHT_CHAT_URL"] = c["chat_url"]


def settings(c):
    """Everything a person needs to answer 'is this working?' without a terminal."""
    import archivist
    host, port = archivist.HOST, archivist.PORT
    base = f"http://{host}:{port}" if str(port) != "80" else f"http://{host}"
    dev = probe(f"{base}/v1/models", archivist.KEY)
    emb = {"ok": False}
    if dev.get("ok"):
        try:
            archivist.embed(["ping"])
            emb = {"ok": True}
        except Exception as e:  # noqa: BLE001
            emb = {"ok": False, "error": str(e)[:110]}
    chat_url = os.environ.get("LASTLIGHT_CHAT_URL") or _cfg().get("chat_url") or ""
    chat = probe((chat_url or base).rstrip("/") + "/v1/models",
                 os.environ.get("LASTLIGHT_CHAT_KEY") or archivist.KEY)
    q = lambda sql: c.execute(sql).fetchone()[0]
    vec = q("SELECT COUNT(*) FROM vec") if c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vec'"
    ).fetchone() else 0
    ch = q("SELECT COUNT(*) FROM chunk")
    return {
        "device": {"host": host, "port": port, "reachable": dev.get("ok"),
                   "code": dev.get("code"), "error": dev.get("error"),
                   "key_set": bool(archivist.KEY),
                   "key_hint": (archivist.KEY[:4] + "\u2026" + archivist.KEY[-4:])
                               if archivist.KEY else ""},
        "embedding": {"model": archivist.EMBED_MODEL, "working": emb.get("ok"),
                      "error": emb.get("error")},
        "chat": {"url": chat_url or base, "source": "override" if chat_url else "device",
                 "reachable": chat.get("ok"), "code": chat.get("code")},
        "vault": str(vault_root()),
        "archive": str(archive.HOME),
        "corpus": {"notes": q("SELECT COUNT(*) FROM doc"), "chunks": ch,
                   "vectors": vec, "entities": q("SELECT COUNT(*) FROM entity"),
                   "unembedded": ch - vec},
        "config_file": str(VAULT_CFG),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_response(self, code, message=None):  # noqa: N802
        # Remembered so the guard below knows whether a reply is already going
        # out. Half a response plus a 500 is worse than half a response.
        self._answered = True
        BaseHTTPRequestHandler.send_response(self, code, message)

    def _guard(self, handle):
        """The one place a handler that raises turns into an answer.

        Without this an exception reaches socketserver, which prints it and
        closes the connection having written nothing. The browser calls that
        ERR_EMPTY_RESPONSE and the page shows "Failed to fetch", which names
        neither the route nor the cause. That is how a missing table went
        unnoticed through two releases. A 500 carrying the exception puts the
        reason in the network tab, and the traceback still goes to farm.log.
        """
        self._answered = False
        try:
            return handle()
        except Exception as e:  # noqa: BLE001 - every route, deliberately
            traceback.print_exc()
            sys.stderr.flush()
            if self._answered:
                return None
            try:
                return self._json({"error": f"{type(e).__name__}: {e}"[:400]}, 500)
            except Exception:  # noqa: BLE001 - the socket is gone; nothing to do
                return None

    def do_GET(self):  # noqa: N802
        return self._guard(self._get)

    def do_POST(self):  # noqa: N802
        return self._guard(self._post)

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

    def _audio(self, path):
        """Serve a wav, honouring Range.

        Not decoration. An <audio> element asks for bytes=0- before it will play
        anything, and Safari refuses a response that answers 200 with the whole
        file instead of 206 with the range it asked for. The overview is five
        minutes of 24kHz PCM, so the seek bar has to work too.
        """
        size = path.stat().st_size
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        start, end = 0, size - 1
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), end)
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        with path.open("rb") as fh:
            fh.seek(start)
            blob = fh.read(end - start + 1)
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _get(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/favicon.ico":
            # The page carries its own mark inline. This is for the browser that
            # asks anyway, so the console stays clean enough that a real error
            # in it means something.
            self.send_response(204)
            self.end_headers()
            return None
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
            return self._json(bridges(ents, kept, 30))
        if u.path == "/api/vault":
            return self._json({"vault": str(vault_root())})
        if u.path == "/api/settings":
            return self._json(settings(c))
        if u.path == "/api/shelves":
            return self._json(shelves(c))
        if u.path == "/api/shelf":
            return self._json(shelf(c, (q.get("name") or [""])[0],
                                    q=(q.get("q") or [""])[0].strip()))
        if u.path == "/api/note":
            return self._json(note(c, int((q.get("id") or ["0"])[0]),
                                   max(1, int((q.get("from") or ["1"])[0]))))
        if u.path == "/api/entity":
            return self._json(entity(c, (q.get("name") or [""])[0]))
        if u.path == "/api/overview":
            return self._json({"job": overview.job(), "saved": overview.saved()})
        if u.path == "/api/overview/get":
            d = overview.load((q.get("slug") or [""])[0])
            return self._json(d or {"error": "no such overview"}, 200 if d else 404)
        if u.path == "/api/overview/audio":
            p = overview.audio_path((q.get("slug") or [""])[0])
            if not p:
                return self._send(404, "no such overview", "text/plain")
            return self._audio(p)
        return self._send(404, "no such path", "text/plain")

    def _post(self):
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
        if u.path == "/api/settings":
            import archivist
            changed = {}
            if body.get("host"):
                archivist.HOST = body["host"].strip(); changed["host"] = archivist.HOST
            if body.get("port"):
                archivist.PORT = str(body["port"]).strip(); changed["port"] = archivist.PORT
            if body.get("key"):
                archivist.KEY = body["key"].strip(); changed["key"] = "set"
            if body.get("chat_url") is not None:
                _save_cfg(chat_url=body["chat_url"].strip())
                os.environ["LASTLIGHT_CHAT_URL"] = body["chat_url"].strip()
                changed["chat_url"] = body["chat_url"].strip()
            if changed.get("host") or changed.get("key") or changed.get("port"):
                _save_cfg(host=changed.get("host"), port=changed.get("port"),
                          key=(archivist.KEY if body.get("key") else None))
            return self._json({"ok": True, "changed": list(changed),
                               "settings": settings(_db())})
        if u.path == "/api/ask":
            qn = (body.get("q") or "").strip()
            if not qn:
                return self._json({"error": "ask something"}, 400)
            doc = body.get("doc")
            if doc:
                return self._json(ask_doc(_db(), int(doc), qn))
            return self._json(ask(_db(), qn, (body.get("focus") or "").strip()))
        if u.path == "/api/overview":
            r = overview.start_job((body.get("kind") or "corpus").strip(),
                                   (body.get("name") or "").strip() or None)
            # 409 rather than an error field, because the browser polls this and
            # "already running" is the normal answer to a double click, not a
            # fault worth showing anybody.
            return self._json(r, 409 if r.get("busy")
                              else (400 if r.get("error") else 200))
        return self._send(404, "no such path", "text/plain")


def run(port=8500):
    apply_cfg()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"\n  cockpit   http://127.0.0.1:{port}/")
    print(f"  archive   {archive.HOME}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye")


def selfcheck():
    """Prove the offline half works, on a corpus built from nothing.

    Everything here runs without a device, without a network and without a
    third-party package, because that is the only part that can be checked
    somewhere other than the machine it will live on. Embedding and answering
    need the Tiiny and are checked by the Setup panel at runtime instead.
    """
    import tempfile
    tmp = tempfile.mkdtemp(prefix="brain-selfcheck-")
    os.environ["ARCHIVER_HOME"] = tmp
    os.environ["BRAIN_VAULT_CONFIG"] = os.path.join(tmp, "cfg.json")
    import importlib
    importlib.reload(archive)
    import entities
    importlib.reload(entities)

    print(archive._chunk_selftest())
    print("  " + entities._fold_selftest())

    notes = os.path.join(tmp, "notes.jsonl")
    with open(notes, "w") as fh:
        for i in range(9):
            fh.write(json.dumps({
                "title": f"Note {i}", "shelf": "Selfcheck",
                "text": ("The platform Argus runs on is Keelpin, and the "
                         "security work under Keelpin is owned by Argus. " * 9
                         + f"My partner Richard reviewed revision {i} of Keelpin "
                           f"and Richard signed it off. ") * 3}) + "\n")
    archive.add_text([notes], source="selfcheck")
    c = archive.db()
    docs = c.execute("SELECT COUNT(*) n FROM doc").fetchone()["n"]
    assert docs == 9, f"expected 9 notes, got {docs}"
    archive.chunk_all()
    chunks = c.execute("SELECT COUNT(*) n FROM chunk").fetchone()["n"]
    assert chunks > 0, "chunker produced nothing"
    print(f"  ingest: {docs} notes -> {chunks} chunks")

    entities.build(min_docs=3)
    names = {r["name"] for r in c.execute("SELECT name FROM entity")}
    assert "Keelpin" in names and "Richard" in names, sorted(names)[:20]
    print(f"  entities: {len(names)} found, Keelpin and Richard among them")

    assert _shape(c) == ("doc_id", False), _shape(c)
    d = window(archive.db(), "", 40)
    assert set(d) >= {"nodes", "edges", "total_entities", "total_edges"}, sorted(d)
    assert 0 <= d["total_entities"] <= len(names), (d["total_entities"], len(names))
    assert isinstance(d["nodes"], list) and isinstance(d["edges"], list)
    print(f"  graph: reduction ran over {d['total_entities']} entities, "
          f"{d['total_edges']} co-occurrences, over whole notes")

    # the routing rule is the thing most likely to rot silently
    print("  " + entities._routing_selftest())

    # The audio overview, as far as it goes without a device. The citation
    # filter, the request splitter and the concatenation are all checkable here
    # and all three are places where a break looks like success: a script that
    # lost its grounding still reads well, an oversized request only fails on a
    # busy device, and a mismatched sample rate concatenates perfectly happily.
    print("  " + overview._script_selftest())
    print("  " + overview._speech_split_selftest())
    print("  " + overview._stitch_selftest())
    print("  " + overview._plan_selftest(archive.db()))

    page = (STATIC / "cockpit.html").read_bytes()
    assert page.count(b"<title>") == 1
    assert b"/api/overview" in page, "the page has no way to reach the overview"
    print("  routing and page: ok")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print(_library_selfcheck())
    print("\n  selfcheck passed")
    return 0


def _library_selfcheck():
    """The same reduction over a corpus of books, which takes the other branch.

    Everything above is a note vault, and a note vault exercises none of the
    library path: one page per document, no subject index, so the shape test
    picks whole documents and capitalised names exactly as it always did. This
    builds forty eight-page documents and a subject index over them, which is
    the smallest thing that is a library rather than a vault.
    """
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="brain-selfcheck-lib-")
    os.environ["ARCHIVER_HOME"] = tmp
    import importlib
    importlib.reload(archive)
    import entities
    importlib.reload(entities)
    _graph._cache = None

    filler = ("The chapter continues with general remarks about equipment and "
              "weather and the ordinary business of being outdoors for a long "
              "time without help arriving. ")
    topics = [("deadfall", "trigger", "bait"), ("tinder", "kindling", "ember"),
              ("tourniquet", "haemorrhage", "wound"), ("snare", "wire", "trail")]
    c = archive.db()
    for i in range(40):
        cur = c.execute(
            "INSERT INTO doc(path,title,source,pages,sha,added_at) "
            "VALUES(?,?,?,?,?,?)",
            (f"lib#{i}", f"Manual {i}", "selfcheck shelf", 8,
             hashlib.sha1(str(i).encode()).hexdigest()[:16], archive.now()))
        doc_id = cur.lastrowid
        # each topic lands in exactly four documents, which is inside the
        # document-frequency window subjects() keeps at this corpus size
        t = topics[i % 4] if i < 16 else None
        for p in range(1, 9):
            body = filler * 4
            if t and p <= 4:
                body += (f"The {t[0]} is set with a {t[1]} and a {t[2]}. "
                         f"A {t[0]} without a {t[1]} will not fall. ") * 6
            c.execute("INSERT INTO page(doc_id,page_no,status,engine,conf,chars,"
                      "text,done_at) VALUES(?,?,'text','selfcheck',1.0,?,?,?)",
                      (doc_id, p, len(body), body, archive.now()))
        c.commit()
        archive.chunk_doc(c, doc_id)

    entities.subjects(min_docs=3)
    subs = {r["name"] for r in c.execute(
        "SELECT name FROM entity WHERE kind='subject'")}
    assert "deadfall" in subs and "tourniquet" in subs, sorted(subs)[:30]

    shape = _shape(c)
    assert shape == ("chunk_id", True), shape

    ids = {r["id"] for r in c.execute(
        "SELECT id FROM entity WHERE kind='subject'")}
    ents, kept, _ = _graph(c)
    assert ents and set(ents) <= ids, "the graph drew something that is not a subject"

    d = window(archive.db(), "", 40)
    assert isinstance(d["nodes"], list) and isinstance(d["edges"], list)

    # a book is read a page at a time, and says so
    doc_id = c.execute("SELECT id FROM doc LIMIT 1").fetchone()["id"]
    n = note(c, doc_id, budget=2000)
    assert n["pages"] == 8 and n["from"] == 1, (n["pages"], n["from"])
    assert "[p1]" in n["text"], n["text"][:200]
    assert n["more"] and n["to"] < 8, (n["to"], n["more"])
    n2 = note(c, doc_id, first=n["to"] + 1, budget=2000)
    assert n2["from"] == n["to"] + 1 and f"[p{n2['from']}]" in n2["text"], n2["from"]

    # and a question about a book gets passages, not the first 24,000 characters
    whole_text, whole = _passages(c, doc_id, "how is a deadfall triggered", 90000)
    assert whole, "a document under budget should go over whole"
    part, whole = _passages(c, doc_id, "how is a deadfall triggered", 1500)
    assert not whole and "[p" in part, part[:200]
    assert len(part) < len(whole_text), (len(part), len(whole_text))
    assert "deadfall" in part, "the excerpt missed the term that was asked about"

    shutil.rmtree(tmp, ignore_errors=True)
    _graph._cache = None
    return (f"  library: {len(subs)} subjects over 40 books, graph drew "
            f"{len(ents)} of them across {len(kept)} links, chunk-scoped; "
            f"reader paged and passages excerpted")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(selfcheck())
    # The farm hands the port in TIINYAPP_PORT, so farm start --port N moves it.
    run(int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("TIINYAPP_PORT") or 8500))
