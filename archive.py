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
def chunk_all():
    """Group consecutive good pages into overlapping chunks, carrying the page
    range so a citation can point at something a human can open."""
    c = db()
    c.execute("DELETE FROM chunk")
    made = 0
    for doc in c.execute("SELECT * FROM doc ORDER BY id"):
        pages = c.execute(
            "SELECT page_no,text FROM page WHERE doc_id=? AND status IN ('text','ocr') "
            "AND chars>0 ORDER BY page_no", (doc["id"],)).fetchall()
        buf, first, last = "", None, None
        for p in pages:
            if first is None:
                first = p["page_no"]
            last = p["page_no"]
            buf += ("\n\n" if buf else "") + p["text"]
            while len(buf) >= CHUNK_CHARS:
                cut = buf.rfind(" ", 0, CHUNK_CHARS) or CHUNK_CHARS
                c.execute("INSERT INTO chunk(doc_id,page_from,page_to,text,chars) "
                          "VALUES(?,?,?,?,?)",
                          (doc["id"], first, last, buf[:cut], cut))
                made += 1
                buf = buf[max(0, cut - CHUNK_OVERLAP):]
                first = last
        if buf.strip():
            c.execute("INSERT INTO chunk(doc_id,page_from,page_to,text,chars) "
                      "VALUES(?,?,?,?,?)",
                      (doc["id"], first or 1, last or 1, buf.strip(), len(buf)))
            made += 1
    c.commit()
    print(f"  {made:,} chunks, each carrying its page range")


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
                pages = json.load(r).get("pages") or []
        except Exception as exc:  # noqa: BLE001
            print(f"  hub unreachable ({str(exc)[:50]}), retrying in 15s")
            time.sleep(15)
            continue
        if not pages:
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
    if a.cmd == "hub":
        import hub
        return hub.serve(a.port)
    if a.cmd == "work":
        return work(a.hub, a.workers, a.engine, a.name)
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
