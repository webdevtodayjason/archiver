#!/usr/bin/env python3
"""Loading notes, from the page instead of from a terminal.

Everything here was already possible on the command line: walk a folder of
markdown, file each note, cut it into chunks, embed the chunks on the device,
rebuild the name index. Five commands, in one order, and a person who has never
opened a terminal had no way to reach any of them. So the board came up empty
and stayed empty, which reads as a broken tool rather than an empty one.

Same five steps, one button. Nothing is reimplemented: the walk is
md2jsonl.records, the chunker is archive.chunk_doc, the embedder is
archivist.embed, the names are entities.build. What is new is the order, a
progress report a page can poll, and the honest answer when a step cannot run.

Two rules it is built around.

**Loading twice must be cheap.** A note already filed, unchanged, is skipped
before it is read any further, and only new or changed notes are chunked and
embedded. Re-pointing at the same vault after writing one note costs one note,
not the whole archive. archive.chunk_all() cannot be used here for that reason:
it empties the chunk table and every vector with it, because chunk ids are
rowids and sqlite reuses them.

**A step that cannot run says which step and why.** The Tiiny holds the
embedding model, and a Tiiny with no embedding model loaded is a normal Tuesday,
not a fault. The notes and their chunks are safe on disk by then, so the answer
is to say the embedding is waiting and let the person press it again once a
model is up, never to throw the reading away.

No subprocess, no third-party package: the farm's archive scanner refuses a
shipped tree that can start another program.
"""
import hashlib
import json
import os
import pathlib
import threading
import time

import archive
import md2jsonl

# A note shorter than this has nothing in it to retrieve. md2jsonl's own floor
# is 200 characters, which is right for a vault of thousands and wrong for the
# person trying the tool out with six notes, so the loader uses a lower one and
# says how many it passed over.
MIN_CHARS = int(os.environ.get("BRAIN_MIN_NOTE_CHARS", "40"))
EMBED_BATCH = int(os.environ.get("BRAIN_EMBED_BATCH", "16"))

# What a note loaded from a folder is filed under. One prefix for the lot, so a
# second load of the same vault finds the same rows and does nothing twice.
KEY = "vault#"

# Browsing is a person clicking through folders, so it has to answer now, not
# when the walk finishes. Both caps are generous for a vault and short of a
# whole home directory.
COUNT_DEADLINE = 1.2      # seconds spent counting what is in a folder
COUNT_CAP = 20000         # files looked at before the count admits it stopped


def key_for(root_id, rel_posix):
    """Where a note is filed, as a string that is the same on every machine.

    The folder is part of it, not only the path inside it. A work vault and a
    personal one both have an Inbox at the top, and filed on the relative path
    alone the second one loaded quietly replaces the first: one row, one title,
    and a note the person can still see on disk gone off their board. Moving a
    vault to a new path reads as a new vault, which is the right way round to be
    wrong.
    """
    return f"{KEY}{root_id}#{rel_posix}"


def root_id(root):
    """A short stable name for a folder, from its path."""
    return hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:8]


def expand(path):
    """A folder path as a person typed it.

    Left as it was found otherwise. A Windows path handed to a Mac is not a
    folder on that Mac and the answer is to say so, not to guess at a
    translation of it.
    """
    text = (path or "").strip().strip('"').strip("'")
    if not text:
        return pathlib.Path.home()
    try:
        return pathlib.Path(text).expanduser()
    except (RuntimeError, OSError):       # ~someone-who-does-not-exist
        return pathlib.Path(text)


def shortcuts():
    """The handful of places a vault actually is, minus the ones that are not."""
    home = pathlib.Path.home()
    picks = [
        ("Home", home),
        ("Documents", home / "Documents"),
        ("Desktop", home / "Desktop"),
        ("Obsidian Vault", home / "Obsidian Vault"),
        ("iCloud Obsidian", home / "Library" / "Mobile Documents"
                            / "iCloud~md~obsidian" / "Documents"),
    ]
    out, seen = [], set()
    for label, p in picks:
        try:
            ok = p.is_dir()
        except OSError:
            ok = False
        if ok and str(p) not in seen:
            seen.add(str(p))
            out.append({"label": label, "path": str(p)})
    return out


def survey(root):
    """How many markdown notes and how many PDFs are under here.

    Bounded, because this runs while somebody is clicking. When it runs out of
    budget it says so rather than quietly reporting a number that is only part
    of the folder.
    """
    md = pdf = looked = 0
    capped = False
    deadline = time.time() + COUNT_DEADLINE
    for here, dirs, files in os.walk(str(root)):
        # Exactly what the walk skips, so the number on the button is the
        # number that gets loaded.
        dirs[:] = [d for d in dirs if d not in md2jsonl.SKIP_DIRS]
        for name in files:
            looked += 1
            low = name.lower()
            if low.endswith(".md"):
                md += 1
            elif low.endswith(".pdf"):
                pdf += 1
        if looked >= COUNT_CAP or time.time() > deadline:
            capped = True
            break
    return {"md": md, "pdf": pdf, "capped": capped}


def folders(path=""):
    """What is inside a folder: the subfolders, and what is worth loading.

    The server runs on the person's own machine, on loopback, so this browses
    their filesystem the way a file dialog would. It is here because the page
    cannot: a browser hands over uploaded copies of files, and these files are
    already on disk and are meant to stay there.
    """
    root = expand(path)
    try:
        ok = root.is_dir()
    except OSError:
        ok = False
    if not ok:
        return {"path": str(root), "parent": "", "folders": [],
                "shortcuts": shortcuts(),
                "error": f"{root} is not a folder on this machine"}
    subs = []
    try:
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith(".") or child.name in md2jsonl.SKIP_DIRS:
                continue
            try:
                if child.is_dir():
                    subs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
    except (PermissionError, OSError) as e:      # noqa: PERF203
        return {"path": str(root), "parent": str(root.parent), "folders": [],
                "shortcuts": shortcuts(),
                "error": f"this folder cannot be read: {str(e)[:80]}"}
    counts = survey(root)
    parent = "" if root.parent == root else str(root.parent)
    return {"path": str(root), "parent": parent, "folders": subs,
            "shortcuts": shortcuts(), "md": counts["md"], "pdf": counts["pdf"],
            "capped": counts["capped"]}


# ----------------------------------------------------------------- the job
# One load at a time, in a thread, with a dict the page polls. The same shape
# overview.py uses for the audio job, for the same reason: the work takes
# minutes and the browser cannot hold a request open for it.
_JOB = {"running": False, "stage": "", "done": 0, "total": 0, "errors": [],
        "started": None, "finished": None, "state": "idle", "message": "",
        "folder": ""}
_LOCK = threading.RLock()


def state():
    """What the loader is doing, as the page reads it."""
    with _LOCK:
        return dict(_JOB)


def _set(**kw):
    with _LOCK:
        _JOB.update(kw)


def _fail(state_name, message):
    _set(running=False, state=state_name, message=message, finished=time.time())


def models():
    """What the device says it is serving, or why it could not be asked.

    Names the models rather than only saying yes or no, because "no embedding
    model is loaded" is worth reading beside the list of what is.
    """
    import urllib.request
    import archivist
    base = (f"http://{archivist.HOST}:{archivist.PORT}"
            if str(archivist.PORT) != "80" else f"http://{archivist.HOST}")
    req = urllib.request.Request(base + "/v1/models")
    if archivist.KEY:
        req.add_header("Authorization", f"Bearer {archivist.KEY}")
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            d = json.load(r)
    except Exception as e:                       # noqa: BLE001
        return {"reachable": False, "base": base, "error": str(e)[:120], "ids": []}
    ids = [m.get("id") for m in (d.get("data") or []) if m.get("id")]
    return {"reachable": True, "base": base, "ids": ids}


def start(folder, embed=None):
    """Begin a load. Returns the state the page should start polling."""
    with _LOCK:
        if _JOB.get("running"):
            d = dict(_JOB)
            d["busy"] = True
            d["error"] = "notes are already being loaded"
            return d
        _JOB.clear()
        _JOB.update({"running": True, "stage": "reading", "done": 0, "total": 0,
                     "errors": [], "started": time.time(), "finished": None,
                     "state": "running", "message": "reading the folder",
                     "folder": str(expand(folder)),
                     "added": 0, "updated": 0, "unchanged": 0, "short": 0,
                     "pdfs": 0, "chunks": 0, "embedded": 0, "entities": 0,
                     "skipped": ""})
    t = threading.Thread(target=_run, args=(folder, embed), daemon=True)
    t.start()
    return state()


def _run(folder, embed):
    """The one place a step that blows up turns into an answer.

    BaseException, not Exception. archivist.die() calls sys.exit when the device
    has never been configured, and SystemExit in a worker thread kills the
    thread silently and leaves the job marked running. The page then polls a
    load that will never finish and says "loading" for the rest of the evening,
    which is the failure it is hardest to work out from the outside.
    """
    try:
        _load(folder, embed)
    except BaseException as e:                   # noqa: BLE001 - it is a thread
        import traceback
        traceback.print_exc()
        _fail("error", f"{type(e).__name__}: {e}"[:300])


def _load(folder, embed):
    if not (folder or "").strip():
        return _fail("no-folder", "no folder chosen yet")
    root = expand(folder)
    if not root.is_dir():
        return _fail("no-folder", f"{root} is not a folder on this machine")

    # ------------------------------------------------------------- reading
    _set(stage="reading", message="reading the folder")
    notes, short = [], 0
    for rec, _rel in md2jsonl.records(root, MIN_CHARS):
        if rec is None:
            short += 1
            continue
        notes.append(rec)
    pdfs = survey(root)["pdf"]
    _set(short=short, pdfs=pdfs, total=len(notes))
    if not notes:
        if short:
            return _fail("too-short", f"{short} markdown files here, and every one "
                                      f"is too short to be worth indexing")
        return _fail("empty", "no markdown files in that folder")

    # -------------------------------------------------------------- filing
    _set(stage="filing", done=0, total=len(notes),
         message=f"filing {len(notes):,} notes")
    c = archive.db()
    touched, added, updated, unchanged, errors = [], 0, 0, 0, []
    rid = root_id(root)
    for i, rec in enumerate(notes, 1):
        key = key_for(rid, rec["path"])
        sha = hashlib.sha1(rec["text"].encode("utf-8")).hexdigest()[:16]
        try:
            row = c.execute("SELECT id, sha FROM doc WHERE path=?", (key,)).fetchone()
            if row and row["sha"] == sha:
                unchanged += 1
            elif row:
                # The note changed on disk. Its text is replaced in place, which
                # keeps the doc id and so keeps anything already pointing at it.
                c.execute("UPDATE doc SET title=?, source=?, sha=? WHERE id=?",
                          (rec["title"][:200], rec["shelf"][:120], sha, row["id"]))
                c.execute("DELETE FROM page WHERE doc_id=?", (row["id"],))
                c.execute("INSERT INTO page(doc_id,page_no,status,engine,conf,"
                          "chars,text,done_at) VALUES(?,1,'text','markdown',1.0,?,?,?)",
                          (row["id"], len(rec["text"]), rec["text"], archive.now()))
                touched.append(row["id"])
                updated += 1
            else:
                cur = c.execute("INSERT INTO doc(path,title,source,pages,sha,added_at) "
                                "VALUES(?,?,?,1,?,?)",
                                (key, rec["title"][:200], rec["shelf"][:120], sha,
                                 archive.now()))
                c.execute("INSERT INTO page(doc_id,page_no,status,engine,conf,"
                          "chars,text,done_at) VALUES(?,1,'text','markdown',1.0,?,?,?)",
                          (cur.lastrowid, len(rec["text"]), rec["text"], archive.now()))
                touched.append(cur.lastrowid)
                added += 1
        except Exception as e:                   # noqa: BLE001 - one bad note
            errors.append(f"{rec['path']}: {str(e)[:90]}")
        if i % 25 == 0 or i == len(notes):
            c.commit()
            _set(done=i, added=added, updated=updated, unchanged=unchanged,
                 errors=errors[:20])
    c.commit()
    _set(done=len(notes), added=added, updated=updated, unchanged=unchanged,
         errors=errors[:20])

    # ------------------------------------------------------------ chunking
    made = 0
    if touched:
        _set(stage="chunking", done=0, total=len(touched),
             message=f"cutting {len(touched):,} notes into passages")
        for i, doc_id in enumerate(touched, 1):
            # A re-read note's old chunks go, and so must the vectors that
            # described them: chunk ids are rowids and sqlite hands them out
            # again, so a vector left behind would answer with somebody else's
            # passage behind the citation.
            ids = [r[0] for r in c.execute(
                "SELECT id FROM chunk WHERE doc_id=?", (doc_id,)).fetchall()]
            if ids:
                c.executemany("DELETE FROM vec WHERE chunk_id=?",
                              [(i2,) for i2 in ids])
            made += archive.chunk_doc(c, doc_id)
            if i % 10 == 0 or i == len(touched):
                _set(done=i, chunks=made)
        if not c.execute("SELECT id FROM chunk LIMIT 1").fetchone():
            c.execute("DELETE FROM vec")          # nothing left for them to name
        c.commit()
        _set(chunks=made)

    # ----------------------------------------------------------- embedding
    stopped = None
    todo = [dict(r) for r in c.execute(
        "SELECT ch.id, ch.text FROM chunk ch LEFT JOIN vec v ON v.chunk_id=ch.id "
        "WHERE v.chunk_id IS NULL ORDER BY ch.id").fetchall()]
    if todo:
        _set(stage="embedding", done=0, total=len(todo),
             message=f"embedding {len(todo):,} passages on the Tiiny")
        if _embed_all(c, todo, embed or _device_embed) is None:
            # Asked only now it has gone wrong. A device that is answering costs
            # nothing to not ask, and the question worth answering is which of
            # these this is: a Tiiny nobody has pointed this at, a Tiiny that is
            # not there, or a Tiiny that is there and serves no embedding model.
            import archivist
            found = models()
            # Named, so the panel can mark that one step and tick the rest. The
            # pipeline runs to the end either way; only this step did not.
            _set(skipped="embedding")
            if not (archivist.HOST and archivist.KEY):
                stopped = ("unset", "no Tiiny is set up yet. Open the light in the "
                                    "top bar and give it a host and a key. Your notes "
                                    "are on the board; embedding is the step that "
                                    "makes them answerable.")
            elif not found["reachable"]:
                stopped = ("device", f"the Tiiny at {found['base']} did not answer. "
                                     "Your notes are on the board; embedding is what "
                                     "Ask needs, and it is the step still waiting.")
            else:
                loaded = ", ".join(found["ids"][:6]) or "nothing"
                stopped = ("model", "no embedding model is loaded on the Tiiny "
                                    f"(it is serving {loaded}). Your notes are on the "
                                    "board. Load an embedding model, then press Try "
                                    "again and only the waiting passages are done.")

    # --------------------------------------------------------------- names
    # Built whether or not the embedding ran. The names come out of the chunks
    # and need no device, and they are what the board draws: a person whose
    # Tiiny is asleep should still see their notes, minus the ability to ask.
    if touched:
        _set(stage="names", done=0, total=1, message="rebuilding the name index")
        import entities
        entities.build()

    n = c.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
    board_is_stale()
    _set(entities=n)
    if stopped:
        return _fail(*stopped)
    _set(running=False, state="done", stage="done", finished=time.time(),
         message=_done_line())


def board_is_stale():
    """Drop the cockpit's cached entity graph, on whichever copy is serving.

    The graph is a scan, so it is cached for the life of the process, and a load
    is the one thing that invalidates it. `import cockpit` is not enough to
    reach it: the app starts as `python3 cockpit.py`, so the module answering
    requests is __main__ and importing it by name builds a second module object
    with its own cache. Clearing the second one leaves the page drawing a graph
    built when the archive was empty, while the vitals underneath it count the
    notes that just arrived. An empty board with notes in the numbers is exactly
    the failure this release exists to fix, and an import was enough to put it
    back.
    """
    import sys
    for name in ("cockpit", "__main__"):
        g = getattr(sys.modules.get(name), "_graph", None)
        if g is not None:
            g._cache = None


def _done_line():
    d = state()
    bits = []
    if d.get("added"):
        bits.append(f"{d['added']:,} new")
    if d.get("updated"):
        bits.append(f"{d['updated']:,} changed")
    if d.get("unchanged"):
        bits.append(f"{d['unchanged']:,} already here")
    if d.get("embedded"):
        bits.append(f"{d['embedded']:,} passages embedded")
    if d.get("short"):
        bits.append(f"{d['short']:,} too short")
    if d.get("pdfs"):
        bits.append(f"{d['pdfs']:,} PDFs left alone")
    return "grown: " + (", ".join(bits) or "nothing new")


def _device_embed(texts):
    import archivist
    return archivist.embed(texts)


def _embed_all(c, todo, embed):
    """Embed every chunk that has no vector. None means nothing could embed.

    A batch that fails on the first try is the device saying it does not serve
    the model, which is a state and not an error. A batch that fails later took
    some vectors with it, so the count stands and re-running finishes the rest.
    """
    import struct
    import archivist
    done = 0
    for i in range(0, len(todo), EMBED_BATCH):
        part = todo[i:i + EMBED_BATCH]
        try:
            vecs = embed([r["text"][:2000] for r in part])
        except (Exception, SystemExit) as e:      # noqa: BLE001
            # SystemExit among them: archivist.die() stops the command line that
            # way when nothing has been configured, and here that is a state to
            # report rather than a thread to lose.
            if done == 0:
                return None
            _set(errors=(state().get("errors") or [])[:19] + [str(e)[:90]])
            break
        for r, v in zip(part, vecs):
            vn = archivist.norm(v)
            c.execute("INSERT OR REPLACE INTO vec(chunk_id,dim,v) VALUES(?,?,?)",
                      (r["id"], len(vn), struct.pack(f"{len(vn)}f", *vn)))
        c.commit()
        done += len(part)
        _set(done=done, embedded=done)
    return done
