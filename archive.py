#!/usr/bin/env python3
"""Archiver - turns a shelf of PDFs into text the Archivist can cite.

    archiver engines                 what OCR is usable here, and why not
    archiver add <folder|pdf>        take a source into the corpus
    archiver run                     do the work; safe to stop and restart
    archiver run --shard 1/3         same corpus, one machine's share
    archiver status                  what is done, what is left, what is poor
    archiver search "tanning"        prove the text came out usable
    archiver export <dir>            chunks as jsonl, ready to embed

The job is grunt work, done once, so the Archivist can answer forever. Three
rules shape the whole thing:

  Never OCR what you can read. Most "scans" carry a text layer. pdftotext first
  and OCR only the pages that come back empty; on a mixed corpus that is the
  difference between a week and an afternoon.

  Provenance or it did not happen. Every chunk keeps its book, its page number
  and its source file, because the Archivist must cite a page a human can go
  and open. A chunk without a page number is unusable no matter how clean.

  Bad text is worse than no text. This ends up in a vault consulted by someone
  who cannot look anything up. A page that reads "50 mg" where the paper said
  "5 mg" will be quoted with total confidence. Every page carries a confidence,
  anything under the floor is quarantined rather than indexed, and the Archivist
  is expected to say "the scan is poor here, read the original".
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
import time

import drivers

HERE = pathlib.Path(__file__).resolve().parent
HOME = pathlib.Path(os.environ.get("ARCHIVER_HOME", HERE / "corpus"))
DB = HOME / "archive.db"
PAGES = HOME / "pages"

# Below this, text is quarantined instead of indexed. Tuned so an honest page of
# printed prose clears it and a page of OCR hash does not.
CONF_FLOOR = float(os.environ.get("ARCHIVER_CONF_FLOOR", "0.55"))
# A page with at least this much extractable text is treated as already digital.
TEXT_LAYER_MIN = 180
CHUNK_CHARS = 1400
CHUNK_OVERLAP = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS doc (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  title TEXT,
  source TEXT,                 -- what shelf it came off
  pages INTEGER,
  sha TEXT,
  added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS page (
  id INTEGER PRIMARY KEY,
  doc_id INTEGER NOT NULL REFERENCES doc(id),
  page_no INTEGER NOT NULL,    -- 1-based, as printed, for citation
  status TEXT NOT NULL,        -- todo|text|ocr|poor|blank|failed
  engine TEXT,                 -- how the text was obtained
  conf REAL,
  chars INTEGER,
  text TEXT,
  ms INTEGER,
  done_at TEXT,
  UNIQUE(doc_id, page_no)
);
CREATE TABLE IF NOT EXISTS chunk (
  id INTEGER PRIMARY KEY,
  doc_id INTEGER NOT NULL REFERENCES doc(id),
  page_from INTEGER NOT NULL,
  page_to INTEGER NOT NULL,
  text TEXT NOT NULL,
  chars INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS page_todo ON page(status);
CREATE INDEX IF NOT EXISTS chunk_doc ON chunk(doc_id);
"""


def db():
    HOME.mkdir(parents=True, exist_ok=True)
    PAGES.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=60)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    # Several machines can share one corpus over the network; WAL plus a busy
    # timeout is all the coordination the shard scheme needs.
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def need(tool):
    if not shutil.which(tool):
        sys.exit(f"{tool} is required. brew install poppler")
    return tool


# ------------------------------------------------------------------ add
def page_count(pdf):
    try:
        out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True,
                             text=True, errors="replace", timeout=120).stdout
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        return int(m.group(1)) if m else 0
    except Exception:  # noqa: BLE001
        return 0


def add(paths, source=None):
    need("pdfinfo")
    c = db()
    found = []
    for p in paths:
        p = pathlib.Path(p).expanduser()
        if p.is_dir():
            found += sorted(p.rglob("*.pdf"))
        elif p.suffix.lower() == ".pdf":
            found.append(p)
    if not found:
        sys.exit("no PDFs found there")
    new = skipped = 0
    for pdf in found:
        n = page_count(pdf)
        if not n:
            print(f"  unreadable, skipped: {pdf.name}")
            continue
        sha = hashlib.sha1(str(pdf.resolve()).encode()).hexdigest()[:16]
        try:
            cur = c.execute(
                "INSERT INTO doc(path,title,source,pages,sha,added_at) VALUES(?,?,?,?,?,?)",
                (str(pdf.resolve()), pdf.stem.replace("_", " ")[:200],
                 source or pdf.parent.name, n, sha, now()))
        except sqlite3.IntegrityError:
            skipped += 1
            continue
        doc_id = cur.lastrowid
        c.executemany("INSERT INTO page(doc_id,page_no,status) VALUES(?,?,'todo')",
                      [(doc_id, i) for i in range(1, n + 1)])
        new += 1
    c.commit()
    todo = c.execute("SELECT COUNT(*) c FROM page WHERE status='todo'").fetchone()["c"]
    print(f"  added {new} documents ({skipped} already known)")
    print(f"  {todo:,} pages waiting. Run: archiver run")


# ----------------------------------------------------------------- work
def text_layer(pdf, page_no):
    """What poppler can already read off the page, if anything."""
    try:
        out = subprocess.run(
            ["pdftotext", "-f", str(page_no), "-l", str(page_no), "-layout",
             str(pdf), "-"], capture_output=True, text=True, errors="replace", timeout=120)
        return (out.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def render(pdf, page_no, dest, tries=2):
    """One page to PNG at 300dpi, which is what OCR engines want.

    Retried once and the reason kept. Under heavy concurrency pdftoppm
    occasionally comes back with nothing, and an early version recorded that as
    a bare 'failed' with no explanation - which on a million-page run is the
    difference between a fixable problem and a mystery."""
    stem = str(dest.with_suffix(""))
    why = ""
    for attempt in range(tries):
        try:
            r = subprocess.run(
                ["pdftoppm", "-f", str(page_no), "-l", str(page_no),
                 "-r", "300", "-png", "-singlefile", str(pdf), stem],
                capture_output=True, timeout=300)
            if dest.exists() and dest.stat().st_size > 0:
                return dest, ""
            why = (r.stderr or b"").decode("utf-8", "replace").strip()[:70] \
                or f"pdftoppm produced nothing (rc {r.returncode})"
        except Exception as exc:  # noqa: BLE001
            why = str(exc)[:70]
        if attempt + 1 < tries:
            time.sleep(0.4)
    return None, why or "render failed"


def clean(text):
    """Undo the two things that wreck retrieval on scanned books: words broken
    across a line by a hyphen, and hard-wrapped lines inside a paragraph."""
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"(?<![.!?:;])\n(?!\n)", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def run(limit=None, shard=None, engine=None, force_ocr=False):
    need("pdftotext"); need("pdftoppm")
    c = db()
    q = "SELECT p.*, d.path, d.title FROM page p JOIN doc d ON d.id=p.doc_id " \
        "WHERE p.status='todo'"
    args = []
    if shard:
        i, n = shard
        q += " AND (p.id % ?) = ?"
        args += [n, i]
    q += " ORDER BY p.doc_id, p.page_no"
    if limit:
        q += f" LIMIT {int(limit)}"
    todo = c.execute(q, args).fetchall()
    if not todo:
        print("  nothing to do")
        return 0

    name, fn = None, None
    print(f"  {len(todo):,} pages to work through\n")
    t_start = time.time()
    counts = {"text": 0, "ocr": 0, "poor": 0, "blank": 0, "failed": 0}

    for k, row in enumerate(todo, 1):
        pdf = pathlib.Path(row["path"])
        t0 = time.time()
        status = engine_used = None
        text, conf = "", None

        if not pdf.exists():
            status, engine_used = "failed", "missing file"
        else:
            if not force_ocr:
                text = text_layer(pdf, row["page_no"])
                if len(text) >= TEXT_LAYER_MIN:
                    status, engine_used, conf = "text", "pdftotext", 1.0
            if status is None:
                # No usable text layer, so this page has to be looked at.
                if fn is None:
                    try:
                        name, fn = drivers.pick(engine)
                        print(f"  OCR engine: {name}\n")
                    except drivers.Unavailable as exc:
                        print(f"  {exc}\n")
                        print("  Pages with a text layer were still done. Install an")
                        print("  engine and run again to pick up the rest.")
                        break
                png, why = render(pdf, row["page_no"], PAGES / f"p{row['id']}.png")
                if png is None:
                    status, engine_used = "failed", f"render: {why}"
                else:
                    try:
                        text, conf = fn(str(png))
                        engine_used = name
                        if len(text.strip()) < 25:
                            status = "blank"
                        elif conf is not None and conf < CONF_FLOOR:
                            status = "poor"
                        else:
                            status = "ocr"
                    except Exception as exc:  # noqa: BLE001
                        status, engine_used = "failed", str(exc)[:80]
                    finally:
                        png.unlink(missing_ok=True)

        text = clean(text) if text else ""
        c.execute("UPDATE page SET status=?,engine=?,conf=?,chars=?,text=?,ms=?,"
                  "done_at=? WHERE id=?",
                  (status, engine_used, conf, len(text), text,
                   int((time.time() - t0) * 1000), now(), row["id"]))
        c.commit()          # every page, because this job gets interrupted
        counts[status] = counts.get(status, 0) + 1

        if k % 10 == 0 or k == len(todo):
            el = time.time() - t_start
            rate = k / el if el else 0
            left = (len(todo) - k) / rate if rate else 0
            print(f"  {k:>6,}/{len(todo):,}  {rate*60:5.1f} pages/min  "
                  f"{left/3600:5.1f}h left   "
                  + "  ".join(f"{v} {kk}" for kk, v in counts.items() if v))
    print()
    for kk, v in counts.items():
        if v:
            print(f"  {kk:>7}: {v:,}")
    if counts.get("poor"):
        print(f"\n  {counts['poor']:,} pages scored under {CONF_FLOOR} and were "
              f"quarantined.\n  They are kept but not indexed: archiver status --poor")
    chunk_all()
    return 0


# --------------------------------------------------------------- chunks
def is_junk(text):
    """Tables of contents and index pages are mostly leader dots and page
    numbers. They embed as plausible-looking text and then win searches they
    have nothing to do with - the first real query against this corpus returned
    a contents page ahead of the water-treatment shelf."""
    t = text.strip()
    if len(t) < 120:
        return True
    letters = sum(c.isalpha() for c in t)
    if letters / len(t) < 0.55:
        return True
    # leader dots: "Preservation . . . . . . . 44"
    if t.count(".") / len(t) > 0.12:
        return True
    return False


def add_text(jsonl_paths, source=None):
    """Ingest articles from a JSONL shelf: one object per line, {title, path, text}.

    Wiki-shaped ZIMs (Vikidia, Wikibooks, Gutenberg, Appropedia) are HTML, not
    scans, so there is nothing to render and nothing to OCR - the text is already
    text. unzim.py --html writes the JSONL; this puts it straight in the database
    with status 'text', and chunking picks it up from there.

    One document per article rather than one per collection, so a citation names
    the article the reader should go and read.
    """
    c = db()
    files = []
    for raw in jsonl_paths:
        pp = pathlib.Path(raw).expanduser()
        files += sorted(pp.rglob("*.jsonl")) if pp.is_dir() else [pp]
    if not files:
        sys.exit("no .jsonl shelves found there")
    new = skipped = empty = 0
    for f in files:
        for line in f.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            text = (rec.get("text") or "").strip()
            title = (rec.get("title") or rec.get("path") or "untitled").strip()
            if not text:
                empty += 1
                continue
            # A row may name its own shelf - a vault folder, a ZIM section -
            # which is usually a better source line than the filename.
            shelf = rec.get("shelf") or rec.get("source") or (source or f.stem)
            key = f"{f.stem}#{rec.get('path') or title}"
            try:
                cur = c.execute(
                    "INSERT INTO doc(path,title,source,pages,sha,added_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (key, title[:200], str(shelf)[:120], 1,
                     hashlib.sha1(key.encode()).hexdigest()[:16], now()))
            except sqlite3.IntegrityError:
                skipped += 1
                continue
            c.execute("INSERT INTO page(doc_id,page_no,status,engine,conf,chars,text,done_at) "
                      "VALUES(?,1,'text','zim-html',1.0,?,?,?)",
                      (cur.lastrowid, len(text), text, now()))
            new += 1
        c.commit()
        print(f"  {f.name}: {new:,} articles in")
    print(f"  {new:,} added, {skipped:,} already present, {empty:,} empty")
    return 0


def dedup(apply=False):
    """Drop books the shelf holds twice.

    The zimgit bundles overlap: food-preparation, post-disaster, medicine, water
    and knots ship some of the same books, so a shelf built from several of them
    ingests the same title more than once. The file hashes differ, so the sha
    check at add time never sees it.

    In a vault that cites its sources this is worse than wasted disk. The same
    passage comes back twice under two document ids, and the reader sees two
    sources agreeing when there is really only one book.

    Title and page count alone are not enough to call it: one pair here shares
    both and is genuinely two different documents. So the first real page has to
    match as well, and the copy with the most extracted text is the one kept.
    """
    import collections, difflib
    c = db()
    docs = c.execute("SELECT id,title,pages FROM doc ORDER BY id").fetchall()
    groups = collections.defaultdict(list)
    for d in docs:
        groups[(d["title"], d["pages"])].append(d["id"])

    def head(doc_id):
        r = c.execute("SELECT text FROM page WHERE doc_id=? AND text IS NOT NULL "
                      "AND length(text)>200 ORDER BY page_no LIMIT 1",
                      (doc_id,)).fetchone()
        return " ".join((r["text"] if r else "").split())[:400]

    def size(doc_id):
        return c.execute("SELECT COALESCE(SUM(length(text)),0) n FROM page "
                         "WHERE doc_id=?", (doc_id,)).fetchone()["n"]

    drop, kept_pairs = [], []
    for (title, _pages), ids in groups.items():
        if len(ids) < 2:
            continue
        keep = max(ids, key=size)
        base = head(keep)
        for other in ids:
            if other == keep:
                continue
            ratio = difflib.SequenceMatcher(None, base, head(other)).ratio()
            if ratio > 0.85:
                drop.append(other)
                kept_pairs.append((title, keep, other, ratio))
            else:
                print(f"  keeping both copies of {title.strip()!r} "
                      f"(ids {keep},{other} differ, first page {ratio:.2f})")

    if not drop:
        print("  no duplicate documents")
        return 0
    pg = c.execute(f"SELECT COUNT(*) n FROM page WHERE doc_id IN "
                   f"({','.join('?'*len(drop))})", drop).fetchone()["n"]
    print(f"  {len(drop)} duplicate documents, {pg:,} pages")
    if not apply:
        print("  nothing removed. Re-run with --apply to remove them.")
        return 0
    q = ",".join("?" * len(drop))
    has_vec = c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name='vec'").fetchone()
    if has_vec:
        c.execute(f"DELETE FROM vec WHERE chunk_id IN "
                  f"(SELECT id FROM chunk WHERE doc_id IN ({q}))", drop)
    c.execute(f"DELETE FROM chunk WHERE doc_id IN ({q})", drop)
    c.execute(f"DELETE FROM page  WHERE doc_id IN ({q})", drop)
    c.execute(f"DELETE FROM doc   WHERE id     IN ({q})", drop)
    c.commit()
    left = c.execute("SELECT COUNT(*) n FROM doc").fetchone()["n"]
    print(f"  removed. {left} documents remain")
    return 0


# A PDF Title field is very often the production filename, a job number, or
# nothing at all. Those are worse than the shelf label they would replace,
# because a wrong title reads as provenance.
BAD_TITLE = re.compile(r"^(untitled|unknown|microsoft word|document\d*|scan|print|"
                       r"[a-z]{0,3}\d{3,}[a-z]*$|[\w .-]+\.(pdf|doc|docx|indd|qxd|max|tif)$"
                       r")", re.I)


def _raw_pdf_title(path):
    """Whatever the PDF claims, junk included - useful as a hint to the model."""
    if not os.path.exists(path):
        return None
    try:
        out = subprocess.run(["pdfinfo", path], capture_output=True, text=True,
                             errors="replace", timeout=30).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        k, _, v = line.partition(":")
        if k.strip() == "Title":
            return " ".join(v.split())[:120] or None
    return None


def _title_from_pdf(path):
    """The PDF's own Title field, when it is a title and not a filename."""
    if not os.path.exists(path):
        return None
    try:
        out = subprocess.run(["pdfinfo", path], capture_output=True, text=True,
                             errors="replace", timeout=30).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        k, _, v = line.partition(":")
        if k.strip() == "Title":
            v = " ".join(v.split())
            if len(v) > 3 and not BAD_TITLE.match(v):
                return v[:120]
    return None


def _title_by_model(title_hint, page_text, inside_text=""):
    """Ask the local model what book this is.

    The first page of a scanned book is a cover, a copyright notice or a
    chapter opening, and no rule I wrote told those apart: the heuristic
    offered 'Add sauce to noodles. Stir and Heat through.' as a title. A model
    reading the page gets it right or says it cannot tell, which is the part
    that matters.
    """
    import archivist  # lazy: only the --ask path needs the model
    model, base, key = archivist.pick_chat()
    body = {"model": model, "max_tokens": 60, "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content":
                "Below are two pages of a scanned book and the title recorded "
                "in its PDF metadata, which is often a filename or junk.\n\n"
                "Reply with the book's actual title and nothing else. No "
                "quotes, no explanation. If you cannot tell, reply exactly: "
                "UNKNOWN\n\n"
                "Two cautions. The front page is usually a copyright page, so a "
                "publisher's name there is not the subject of the book: check it "
                "against what the inside page is actually about. And the text is "
                "OCR, so fix obvious character damage in a title you quote.\n\n"
                f"PDF metadata title: {title_hint or '(none)'}\n\n"
                f"FRONT PAGE:\n{(page_text or '')[:1200]}\n\n"
                f"A PAGE FROM INSIDE:\n{(inside_text or '')[:1200]}"}]}
    try:
        d = archivist.api("/v1/chat/completions", body, timeout=120, base=base, key=key)
        out = " ".join((d["choices"][0]["message"].get("content") or "").split())
    except Exception as exc:  # noqa: BLE001
        return None, f"model error: {str(exc)[:40]}"
    out = out.strip().strip('"').strip()
    if not out or out.upper().startswith("UNKNOWN") or len(out) > 120:
        return None, "model could not tell"
    return out, "model"


def retitle(apply=False, ask=False):
    """Give every document its real title.

    The shelf titles come from the ZIM filenames, so a citation reads
    'Nuclear Threats 5 - p160'. That is useless to somebody who wants to check
    what they were just told, and a citation nobody can follow is the same as
    no citation. The PDFs know better: that document is Nuclear War Survival
    Skills, and 'Emergency Preparedness 12' is army FM 3-05.70.
    """
    c = db()
    rows = c.execute("SELECT id,title,path,pages FROM doc ORDER BY id").fetchall()
    plan, kept = [], 0
    for d in rows:
        # With --ask the model decides every time. The PDF Title field survives
        # the junk filter too often ('22\\wq22.PDF', 'THIS CD PRODUCED BY') and
        # a plausible-looking wrong title is worse than an obviously useless one.
        new = None if ask else _title_from_pdf(d["path"])
        how = "pdf"
        if not new and ask:
            pg = c.execute("SELECT text FROM page WHERE doc_id=? AND text IS NOT NULL "
                           "AND length(text)>60 ORDER BY page_no LIMIT 1",
                           (d["id"],)).fetchone()
            hint = _raw_pdf_title(d["path"])
            mid = c.execute("SELECT text FROM page WHERE doc_id=? AND text IS NOT NULL "
                            "AND length(text)>300 ORDER BY page_no "
                            "LIMIT 1 OFFSET ?",
                            (d["id"], max(1, (d["pages"] or 10) // 4))).fetchone()
            new, how = _title_by_model(hint, pg["text"] if pg else "",
                                       mid["text"] if mid else "")
        if not new or new.strip() == (d["title"] or "").strip():
            kept += 1
            continue
        plan.append((d["id"], d["title"], new, how))

    for doc_id, old, new, how in plan:
        print(f"  {old.strip()[:34]:36} -> {new[:58]:60} [{how}]")
    print(f"\n  {len(plan)} documents would be retitled, {kept} left alone")
    if not apply:
        print("  nothing changed. Re-run with --apply.")
        return 0
    # Keep the shelf label. The model is right most of the time and confidently
    # wrong the rest, and a citation naming the wrong book is worse than one
    # naming no book, so the original has to stay recoverable.
    cols = [r[1] for r in c.execute("PRAGMA table_info(doc)")]
    if "title_orig" not in cols:
        c.execute("ALTER TABLE doc ADD COLUMN title_orig TEXT")
    for doc_id, old_t, new_t, _how in plan:
        c.execute("UPDATE doc SET title_orig=COALESCE(title_orig,?), title=? "
                  "WHERE id=?", (old_t, new_t, doc_id))
    c.commit()
    print("  applied. Citations now name the book.")
    return 0


def _chunk_selftest():
    """The chunker must always consume text, whatever the text looks like."""
    for name, body in (("no spaces at all", "x" * 9000),
                       ("one early space", "x" * 40 + " " + "y" * 9000),
                       ("normal prose", ("the quick brown fox " * 900))):
        buf, rounds = body, 0
        while len(buf) >= CHUNK_CHARS:
            cut = buf.rfind(" ", 0, CHUNK_CHARS)
            if cut <= CHUNK_OVERLAP:
                cut = CHUNK_CHARS
            buf = buf[max(0, cut - CHUNK_OVERLAP):]
            rounds += 1
            assert rounds < 1000, f"chunker did not advance on: {name}"
    return True


def chunk_doc(c, doc_id):
    """Chunk one document, leaving every other document's chunks alone.

    chunk_all() deletes the chunk table and rebuilds it, which also has to drop
    every vector, because chunk ids are rowids and SQLite reuses them. That is
    correct for a rebuild and catastrophic for adding one note: writing a note
    would cost a full re-embed of the archive. This adds the new rows instead,
    so only the new chunks need embedding.
    """
    pages = c.execute(
        "SELECT page_no,text FROM page WHERE doc_id=? AND status IN ('text','ocr') "
        "AND chars>0 ORDER BY page_no", (doc_id,)).fetchall()
    c.execute("DELETE FROM chunk WHERE doc_id=?", (doc_id,))
    buf, first, last, made = "", None, None, 0
    for p in pages:
        if first is None:
            first = p["page_no"]
        last = p["page_no"]
        buf += ("\n\n" if buf else "") + p["text"]
        while len(buf) >= CHUNK_CHARS:
            cut = buf.rfind(" ", 0, CHUNK_CHARS)
            if cut <= CHUNK_OVERLAP:
                cut = CHUNK_CHARS
            piece = buf[:cut]
            if not is_junk(piece):
                c.execute("INSERT INTO chunk(doc_id,page_from,page_to,text,chars) "
                          "VALUES(?,?,?,?,?)", (doc_id, first, last, piece, cut))
                made += 1
            buf = buf[max(0, cut - CHUNK_OVERLAP):]
            first = last
    if buf.strip() and not is_junk(buf):
        c.execute("INSERT INTO chunk(doc_id,page_from,page_to,text,chars) "
                  "VALUES(?,?,?,?,?)",
                  (doc_id, first or 1, last or 1, buf.strip(), len(buf)))
        made += 1
    c.commit()
    return made


def chunk_all():
    """Group consecutive good pages into overlapping chunks, carrying the page
    range so a citation can point at something a human can open."""
    c = db()
    # Chunk ids are rowids, and SQLite restarts them at 1 once the table is
    # empty. So a rebuild does not just orphan the old vectors, it silently
    # re-points them at different text - the index would keep answering, with
    # the wrong passage behind every citation. Clear them together or not at all.
    if c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vec'").fetchone():
        n = c.execute("SELECT COUNT(*) n FROM vec").fetchone()["n"]
        if n:
            c.execute("DELETE FROM vec")
            print(f"  dropped {n:,} vectors - they described the old chunks. Re-run: archivist index")
    c.execute("DELETE FROM chunk")
    made = 0
    skipped_junk = [0]
    # fetchall, so the outer read is not held open across every insert below
    for doc in c.execute("SELECT * FROM doc ORDER BY id").fetchall():
        pages = c.execute(
            "SELECT page_no,text FROM page WHERE doc_id=? AND status IN ('text','ocr') "
            "AND chars>0 ORDER BY page_no", (doc["id"],)).fetchall()
        buf, first, last = "", None, None
        junked = 0
        for p in pages:
            if first is None:
                first = p["page_no"]
            last = p["page_no"]
            buf += ("\n\n" if buf else "") + p["text"]
            while len(buf) >= CHUNK_CHARS:
                # Break on a space so chunks do not end mid-word. Two ways that
                # goes wrong, and both stall the loop rather than failing:
                # rfind returns -1 when the whole span is unbroken, and -1 is
                # truthy, so `or` does not catch it; and any break at or before
                # the overlap leaves the buffer exactly as long as it was.
                # Either way the text never advances and chunking spins forever
                # on one document. A 35,000-character list of moth genera found
                # this, after twenty-five minutes at 99% CPU.
                cut = buf.rfind(" ", 0, CHUNK_CHARS)
                if cut <= CHUNK_OVERLAP:
                    cut = CHUNK_CHARS          # no usable break: cut it hard
                piece = buf[:cut]
                if is_junk(piece):
                    junked += 1
                else:
                    c.execute("INSERT INTO chunk(doc_id,page_from,page_to,text,chars) "
                              "VALUES(?,?,?,?,?)",
                              (doc["id"], first, last, piece, cut))
                    made += 1
                buf = buf[max(0, cut - CHUNK_OVERLAP):]
                first = last
        if buf.strip() and not is_junk(buf):
            c.execute("INSERT INTO chunk(doc_id,page_from,page_to,text,chars) "
                      "VALUES(?,?,?,?,?)",
                      (doc["id"], first or 1, last or 1, buf.strip(), len(buf)))
            made += 1
        skipped_junk[0] += junked
    c.commit()
    print(f"  {made:,} chunks, each carrying its page range"
          + (f"  ({skipped_junk[0]:,} contents/index pages skipped)"
             if skipped_junk[0] else ""))


# --------------------------------------------------------------- report
def status(show_poor=False):
    c = db()
    d = c.execute("SELECT COUNT(*) n, SUM(pages) p FROM doc").fetchone()
    print(f"\n  {d['n'] or 0} documents, {d['p'] or 0:,} pages\n")
    rows = c.execute("SELECT status, COUNT(*) n, AVG(conf) cf, SUM(chars) ch "
                     "FROM page GROUP BY status ORDER BY n DESC").fetchall()
    for r in rows:
        cf = f"  avg conf {r['cf']:.2f}" if r["cf"] is not None else ""
        print(f"  {r['status']:>7}  {r['n']:>7,} pages  {(r['ch'] or 0)/1e6:6.1f}M chars{cf}")
    eng = c.execute("SELECT engine, COUNT(*) n FROM page WHERE engine IS NOT NULL "
                    "GROUP BY engine ORDER BY n DESC").fetchall()
    if eng:
        print("\n  by engine: " + ", ".join(f"{r['engine']} {r['n']:,}" for r in eng))
    ch = c.execute("SELECT COUNT(*) n, SUM(chars) ch FROM chunk").fetchone()
    print(f"\n  {ch['n'] or 0:,} chunks ready to embed ({(ch['ch'] or 0)/1e6:.1f}M chars)")
    if show_poor:
        print("\n  quarantined pages:")
        for r in c.execute(
                "SELECT d.title, p.page_no, p.conf FROM page p JOIN doc d ON d.id=p.doc_id "
                "WHERE p.status='poor' ORDER BY p.conf LIMIT 40"):
            print(f"    {r['conf']:.2f}  p{r['page_no']:<5} {r['title'][:56]}")


def search(term, n=8):
    c = db()
    like = f"%{term}%"
    rows = c.execute(
        "SELECT d.title, ch.page_from, ch.page_to, ch.text FROM chunk ch "
        "JOIN doc d ON d.id=ch.doc_id WHERE ch.text LIKE ? LIMIT ?",
        (like, n)).fetchall()
    if not rows:
        print(f"  nothing in the corpus mentions {term!r}")
        return
    print(f"\n  {len(rows)} passages mentioning {term!r}\n")
    for r in rows:
        i = r["text"].lower().find(term.lower())
        snip = " ".join(r["text"][max(0, i - 90):i + 150].split())
        pages = (f"p{r['page_from']}" if r["page_from"] == r["page_to"]
                 else f"pp{r['page_from']}-{r['page_to']}")
        print(f"  {r['title'][:58]}  {pages}")
        print(f"    …{snip}…\n")


def export(dest):
    """One jsonl line per chunk, provenance attached, ready for the embedder."""
    c = db()
    dest = pathlib.Path(dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "chunks.jsonl"
    n = 0
    with out.open("w", encoding="utf-8") as fh:
        for r in c.execute(
                "SELECT ch.id, ch.text, ch.page_from, ch.page_to, d.title, d.source, "
                "d.path FROM chunk ch JOIN doc d ON d.id=ch.doc_id ORDER BY ch.id"):
            fh.write(json.dumps({
                "id": r["id"], "text": r["text"],
                "cite": {"title": r["title"], "source": r["source"],
                         "pages": [r["page_from"], r["page_to"]],
                         "file": os.path.basename(r["path"])}}) + "\n")
            n += 1
    print(f"  {n:,} chunks -> {out}")


def work(hub, workers=8, engine=None, name=None):
    """Worker mode: take pages from a hub, render and OCR locally, send text back.

    Render and OCR both happen here on purpose. Rendering is ~0.3s and OCR is
    ~1.6s, so a hub that rendered would cap the whole fleet at its own
    single-threaded render rate. The hub only hands out page numbers."""
    import queue
    import socket
    import threading
    import urllib.request

    name = name or socket.gethostname().split(".")[0]
    hub = hub.rstrip("/")
    cache = HOME / "pdfcache"
    cache.mkdir(parents=True, exist_ok=True)
    # A worker never calls db(), which is what normally creates this. Without it
    # every page that fell through to OCR failed with "could not write image"
    # while pages with a text layer sailed through, so the fleet looked healthy
    # and silently skipped exactly the work it exists to do.
    PAGES.mkdir(parents=True, exist_ok=True)

    try:
        eng_name, fn = drivers.pick(engine)
    except drivers.Unavailable as exc:
        sys.exit(f"  {exc}")
    print(f"\n  worker {name}  ->  {hub}")
    print(f"  engine {eng_name}, {workers} threads\n")

    def get_doc(doc_id):
        """Each PDF is fetched once and kept. They do not change."""
        f = cache / f"{doc_id}.pdf"
        if not f.exists():
            with urllib.request.urlopen(f"{hub}/api/doc?id={doc_id}", timeout=300) as r:
                f.write_bytes(r.read())
        return f

    done_total = 0
    t0 = time.time()
    while True:
        try:
            with urllib.request.urlopen(
                    f"{hub}/api/claim?worker={urllib.parse.quote(name)}&n={workers*2}",
                    timeout=60) as r:
                _r = json.load(r)
                pages = _r.get("pages") or []
                outstanding = _r.get("outstanding", 0)
        except Exception as exc:  # noqa: BLE001
            print(f"  hub unreachable ({str(exc)[:50]}), retrying in 15s")
            time.sleep(15)
            continue
        if not pages:
            if outstanding:
                # Someone else holds them and may be dead. Wait for the lease to
                # lapse rather than walking away from an unfinished corpus.
                print(f"  {outstanding} pages still leased elsewhere; waiting 60s")
                time.sleep(60)
                continue
            print("  nothing left to claim; stopping")
            break

        q = queue.Queue()
        for p_ in pages:
            q.put(p_)
        results, lock = [], threading.Lock()

        def run_one():
            while True:
                try:
                    job = q.get_nowait()
                except queue.Empty:
                    return
                t = time.time()
                status, text, conf = "failed", "", None
                try:
                    pdf = get_doc(job["doc_id"])
                    txt = text_layer(pdf, job["page_no"])
                    if len(txt) >= TEXT_LAYER_MIN:
                        status, text, conf, used = "text", txt, 1.0, "pdftotext"
                    else:
                        png, why = render(pdf, job["page_no"],
                                          PAGES / f"w{job['page_id']}.png")
                        used = eng_name
                        if png is None:
                            status, used = "failed", f"render: {why}"
                        else:
                            try:
                                text, conf = fn(str(png))
                                if len(text.strip()) < 25:
                                    status = "blank"
                                elif conf is not None and conf < CONF_FLOOR:
                                    status = "poor"
                                else:
                                    status = "ocr"
                            finally:
                                png.unlink(missing_ok=True)
                except Exception as exc:  # noqa: BLE001
                    status, used = "failed", str(exc)[:80]
                with lock:
                    results.append({"page_id": job["page_id"], "status": status,
                                    "worker": name,
                                    "engine": used, "conf": conf,
                                    "text": clean(text) if text else "",
                                    "ms": int((time.time() - t) * 1000)})

        threads = [threading.Thread(target=run_one, daemon=True)
                   for _ in range(workers)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        try:
            req = urllib.request.Request(
                f"{hub}/api/result", data=json.dumps({"pages": results}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=180).read()
        except Exception as exc:  # noqa: BLE001
            print(f"  could not return {len(results)} pages: {str(exc)[:60]}")
            print("  they will return to the pool when the lease expires")
            continue
        done_total += len(results)
        el = time.time() - t0
        print(f"  {done_total:>7,} pages   {done_total/el*60:6.1f}/min   "
              + "  ".join(f"{k} {sum(1 for r in results if r['status']==k)}"
                          for k in ("text", "ocr", "poor", "blank", "failed")
                          if any(r["status"] == k for r in results)))
    return 0


def engines():
    print()
    for name, ok, why in drivers.probe():
        print(f"  {'ok  ' if ok else '--  '} {name:<10} {why}")
    print()


def main():
    p = argparse.ArgumentParser(prog="archiver")
    sub = p.add_subparsers(dest="cmd")
    a = sub.add_parser("add"); a.add_argument("paths", nargs="+")
    a.add_argument("--source", help="what shelf this came off")
    r = sub.add_parser("run")
    r.add_argument("--limit", type=int)
    r.add_argument("--shard", help="i/n - this machine's share, e.g. 1/3")
    r.add_argument("--engine", choices=list(drivers.DRIVERS))
    r.add_argument("--force-ocr", action="store_true",
                   help="ignore text layers and OCR everything")
    s = sub.add_parser("status"); s.add_argument("--poor", action="store_true")
    f = sub.add_parser("search"); f.add_argument("term"); f.add_argument("-n", type=int, default=8)
    e = sub.add_parser("export"); e.add_argument("dest")
    sub.add_parser("engines")
    sub.add_parser("chunk")
    at = sub.add_parser("add-text")
    at.add_argument("paths", nargs="+")
    at.add_argument("--source")
    rt = sub.add_parser("retitle")
    rt.add_argument("--apply", action="store_true",
                    help="actually rename the documents")
    rt.add_argument("--ask", action="store_true",
                    help="ask the local model where the PDF has no usable title")
    dd = sub.add_parser("dedup")
    dd.add_argument("--apply", action="store_true",
                    help="actually remove the duplicates")
    h = sub.add_parser("hub"); h.add_argument("--port", type=int, default=8430)
    w = sub.add_parser("work")
    w.add_argument("--hub", required=True)
    w.add_argument("--workers", type=int, default=8)
    w.add_argument("--engine", choices=list(drivers.DRIVERS))
    w.add_argument("--name")
    a = p.parse_args()

    if a.cmd == "add":
        return add(a.paths, a.source)
    if a.cmd == "run":
        shard = None
        if a.shard:
            i, n = a.shard.split("/")
            shard = (int(i) - 1, int(n))
        return run(a.limit, shard, a.engine, a.force_ocr)
    if a.cmd == "status":
        return status(a.poor)
    if a.cmd == "search":
        return search(a.term, a.n)
    if a.cmd == "export":
        return export(a.dest)
    if a.cmd == "engines":
        return engines()
    if a.cmd == "chunk":
        return chunk_all()
    if a.cmd == "add-text":
        return add_text(a.paths, a.source)
    if a.cmd == "dedup":
        return dedup(a.apply)
    if a.cmd == "retitle":
        return retitle(a.apply, a.ask)
    if a.cmd == "hub":
        import hub
        return hub.serve(a.port)
    if a.cmd == "work":
        return work(a.hub, a.workers, a.engine, a.name)
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
