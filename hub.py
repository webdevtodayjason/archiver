#!/usr/bin/env python3
"""The hub: hands out pages, collects text, shows you what the fleet is doing.

    archiver hub                       serve the corpus and the dashboard
    archiver work --hub http://host:8430 --workers 16

Why a hub rather than the shard flag. Sharding is correct on one machine, where
several processes share one SQLite over a local disk. Across machines it would
mean SQLite on a network filesystem, and SQLite's locking over NFS or SMB is
famously unreliable - the failure is silent corruption of the corpus you spent
five days building, which is the one outcome worth real effort to avoid. So the
database stays on exactly one machine and everything else talks HTTP to it.

The split of work follows from where the time goes. Rendering a page is about
0.3s and OCR is about 1.6s, so the hub must not render: it would cap the whole
fleet at its own single-threaded render rate. Instead a worker fetches the PDF
once, caches it, and does render and OCR locally. The hub only ever hands out
page numbers and takes back text.

Leases, because machines die. A claimed page not returned inside the lease goes
back in the pool, so a worker that loses power costs you a few pages rather than
stalling the run.
"""
import json
import os
import pathlib
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import archive

LEASE_S = int(os.environ.get("ARCHIVER_LEASE_S", "900"))
_LOCK = threading.Lock()

# One connection for the whole hub, serialised by _LOCK.
#
# The first version opened a fresh connection per request. archive.db() runs
# executescript() to create tables if missing, and executescript opens a write
# transaction every time - so a reader holding a WAL snapshot blocked every
# writer, /api/claim hung past the worker's timeout while /api/stats kept
# answering, and the fleet sat idle looking like a network fault. One
# connection, one lock, no contention.
_CONN = None


def _c():
    """The hub's single connection.

    check_same_thread=False because ThreadingHTTPServer answers every request on
    a fresh thread, and sqlite3 binds a connection to its creating thread. That
    is only safe because every caller goes through _LOCK first, so there is
    never more than one statement in flight."""
    global _CONN
    if _CONN is None:
        archive.HOME.mkdir(parents=True, exist_ok=True)
        archive.PAGES.mkdir(parents=True, exist_ok=True)
        _CONN = sqlite3.connect(archive.DB, timeout=60, check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _CONN.executescript(archive.SCHEMA)
        _CONN.execute("PRAGMA journal_mode=WAL")
        _CONN.execute("PRAGMA busy_timeout=30000")
        _ensure_lease_columns(_CONN)
    return _CONN


def _ensure_lease_columns(c):
    cols = {r[1] for r in c.execute("PRAGMA table_info(page)")}
    if "claimed_by" not in cols:
        c.execute("ALTER TABLE page ADD COLUMN claimed_by TEXT")
        c.execute("ALTER TABLE page ADD COLUMN claimed_at REAL")
        c.commit()
    if "done_by" not in cols:
        # Separate from claimed_by, which is cleared on submit so the page is no
        # longer leased. Without this the dashboard could never attribute
        # finished work and always said no worker had reported.
        c.execute("ALTER TABLE page ADD COLUMN done_by TEXT")
        c.commit()


def _reap(c):
    """Expired claims go back in the pool."""
    cutoff = time.time() - LEASE_S
    n = c.execute("UPDATE page SET claimed_by=NULL, claimed_at=NULL "
                  "WHERE status='todo' AND claimed_at IS NOT NULL AND claimed_at < ?",
                  (cutoff,)).rowcount
    if n:
        c.commit()
    return n


_CHUNKED_AT = [0]


def claim(worker, n):
    with _LOCK:
        c = _c()
        _reap(c)
        # When the queue drains and nothing is still in flight, chunk once so a
        # finished corpus is immediately searchable. Otherwise someone has to
        # remember a step, and the run looks complete while the text sits
        # unusable - which is exactly what happened the first time this ran.
        pending = c.execute(
            "SELECT COUNT(*) n FROM page WHERE status='todo'").fetchone()["n"]
        if pending == 0:
            done = c.execute("SELECT COUNT(*) n FROM page "
                             "WHERE status IN ('text','ocr')").fetchone()["n"]
            if done and _CHUNKED_AT[0] != done:
                _CHUNKED_AT[0] = done
                archive.chunk_all()
        rows = c.execute(
            "SELECT p.id, p.doc_id, p.page_no, d.path FROM page p "
            "JOIN doc d ON d.id=p.doc_id "
            "WHERE p.status='todo' AND p.claimed_by IS NULL "
            "ORDER BY p.doc_id, p.page_no LIMIT ?", (n,)).fetchall()
        now = time.time()
        for r in rows:
            c.execute("UPDATE page SET claimed_by=?, claimed_at=? WHERE id=?",
                      (worker, now, r["id"]))
        c.commit()
        return [{"page_id": r["id"], "doc_id": r["doc_id"],
                 "page_no": r["page_no"],
                 "doc": os.path.basename(r["path"])} for r in rows]


def submit(rows):
    with _LOCK:
        c = _c()
        for r in rows:
            c.execute(
                "UPDATE page SET status=?, engine=?, conf=?, chars=?, text=?, ms=?, "
                "done_at=?, done_by=COALESCE(?,claimed_by), "
                "claimed_by=NULL, claimed_at=NULL WHERE id=?",
                (r["status"], r.get("engine"), r.get("conf"),
                 len(r.get("text") or ""), r.get("text") or "",
                 r.get("ms"), archive.now(), r.get("worker"), r["page_id"]))
        c.commit()
    return len(rows)


def stats():
    with _LOCK:
        return _stats(_c())


def _stats(c):
    by = {r["status"]: r["n"] for r in
          c.execute("SELECT status, COUNT(*) n FROM page GROUP BY status")}
    total = sum(by.values()) or 1
    done = total - by.get("todo", 0)
    recent = c.execute(
        "SELECT done_by w, COUNT(*) n, AVG(ms) ms, AVG(conf) cf FROM page "
        "WHERE done_at IS NOT NULL AND done_at > ? GROUP BY done_by",
        (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 300)),)
    ).fetchall()
    # Throughput over the last five minutes, which is what you want when you are
    # deciding whether a box has fallen over.
    win = c.execute(
        "SELECT COUNT(*) n FROM page WHERE done_at > ?",
        (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 300)),)
    ).fetchone()["n"]
    rate = win / 5.0
    left = by.get("todo", 0)
    workers = [{"name": r["w"] or "?", "pages": r["n"],
                "ms": round(r["ms"] or 0), "conf": round(r["cf"] or 0, 3)}
               for r in recent if r["w"]]
    active = c.execute(
        "SELECT claimed_by w, COUNT(*) n FROM page WHERE claimed_by IS NOT NULL "
        "AND status='todo' GROUP BY claimed_by").fetchall()
    docs = c.execute("SELECT COUNT(*) n FROM doc").fetchone()["n"]
    ch = c.execute("SELECT COUNT(*) n FROM chunk").fetchone()["n"]
    return {
        "docs": docs, "total": total, "done": done, "left": left,
        "by_status": by, "pages_per_min": round(rate, 1),
        "eta_hours": round(left / rate / 60, 2) if rate else None,
        "workers": workers,
        "in_flight": [{"name": r["w"], "pages": r["n"]} for r in active],
        "chunks": ch,
    }


DASH = """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Archiver · the fleet</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#101214;--s1:#16191c;--s2:#1d2126;--line:#2a2f36;--ink:#F5F3EE;
 --steel:#8b929c;--dim:#616873;--orange:#EF7D22;--hot:#ffb968;--good:#7fb069;--bad:#d1564a;
 --mono:ui-monospace,"SF Mono",Menlo,monospace}
html{background:var(--bg);color-scheme:dark}
body{font-family:var(--mono);color:var(--ink);font-size:14px;line-height:1.55;
 padding:0 clamp(16px,4vw,48px) 60px;letter-spacing:-.01em}
.w{max-width:1100px;margin:0 auto}
header{padding:28px 0 20px;border-bottom:1px solid var(--line);display:flex;
 align-items:baseline;gap:16px;flex-wrap:wrap}
h1{font-size:24px;font-weight:600;letter-spacing:-.04em}
h1 em{font-style:normal;color:var(--orange)}
.sub{color:var(--dim);font-size:12px}
.pill{margin-left:auto;font-size:11px;letter-spacing:.1em;text-transform:uppercase;
 color:var(--good)}
.grid{display:grid;gap:1px;background:var(--line);border:1px solid var(--line);
 grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin:24px 0;border-radius:7px;
 overflow:hidden}
.c{background:var(--s1);padding:16px}
.v{font-size:26px;font-weight:600;letter-spacing:-.04em;color:var(--hot);
 font-variant-numeric:tabular-nums;line-height:1}
.v small{font-size:.42em;color:var(--dim);margin-left:.3em;font-weight:400}
.k{margin-top:8px;font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--steel)}
.bar{height:8px;background:var(--s2);border-radius:4px;overflow:hidden;margin:6px 0 22px;
 border:1px solid var(--line)}
.bar i{display:block;height:100%;background:var(--orange);width:0;transition:width .8s ease}
h2{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--steel);
 margin:26px 0 10px}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font-weight:400;font-size:10px;letter-spacing:.13em;text-transform:uppercase;
 color:var(--steel);padding:9px 12px;background:var(--s2);border-bottom:1px solid var(--line)}
td{padding:9px 12px;border-bottom:1px solid var(--line)}
.num{text-align:right;font-variant-numeric:tabular-nums}
.tw{border:1px solid var(--line);border-radius:7px;overflow:hidden;background:var(--s1)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--good);
 margin-right:7px;animation:p 1.6s infinite}
@keyframes p{0%,100%{opacity:.35}50%{opacity:1}}
.empty{color:var(--dim);padding:18px 12px;text-align:center;font-size:12px}
.st{display:flex;gap:18px;flex-wrap:wrap;font-size:12px;color:var(--steel);margin-top:4px}
.st b{color:var(--ink);font-weight:500}
.bad{color:var(--bad)}
</style><div class=w>
<header><h1>Archiver<em>·</em>the fleet</h1>
<span class=sub id=corpus></span><span class=pill id=live></span></header>
<div class=bar><i id=prog></i></div>
<div class=st id=statuses></div>
<div class=grid id=stats></div>
<h2>Workers, last five minutes</h2>
<div class=tw><table><thead><tr><th>machine</th><th class=num>pages</th>
<th class=num>ms/page</th><th class=num>confidence</th><th class=num>in flight</th>
</tr></thead><tbody id=workers></tbody></table></div>
</div>
<script>
function n(x){return (x||0).toLocaleString()}
async function tick(){
  let d; try{ d = await (await fetch('/api/stats')).json(); }catch(e){ return; }
  document.getElementById('corpus').textContent =
    n(d.docs)+' documents · '+n(d.total)+' pages';
  document.getElementById('live').innerHTML = d.pages_per_min > 0
    ? '<span class=dot></span>running' : 'idle';
  const pct = d.total ? d.done/d.total*100 : 0;
  document.getElementById('prog').style.width = pct.toFixed(1)+'%';
  document.getElementById('statuses').innerHTML =
    Object.entries(d.by_status).map(([k,v])=>
      `<span>${k} <b>${n(v)}</b></span>`).join('');
  const eta = d.eta_hours==null ? '—'
    : (d.eta_hours < 1 ? Math.round(d.eta_hours*60)+'<small>min</small>'
                       : d.eta_hours.toFixed(1)+'<small>h</small>');
  document.getElementById('stats').innerHTML = [
    [pct.toFixed(1)+'<small>%</small>','complete'],
    [n(d.done),'pages done'],
    [n(d.left),'pages left'],
    [d.pages_per_min+'<small>/min</small>','fleet rate'],
    [eta,'eta'],
    [n(d.chunks),'chunks'],
  ].map(([v,k])=>`<div class=c><div class=v>${v}</div><div class=k>${k}</div></div>`).join('');
  const inflight = Object.fromEntries((d.in_flight||[]).map(w=>[w.name,w.pages]));
  const names = new Set([...(d.workers||[]).map(w=>w.name), ...Object.keys(inflight)]);
  const rows = [...names].map(nm=>{
    const w = (d.workers||[]).find(x=>x.name===nm) || {pages:0,ms:0,conf:0};
    const cf = w.conf ? (w.conf<0.55?`<span class=bad>${w.conf.toFixed(2)}</span>`
                                   :w.conf.toFixed(2)) : '—';
    return `<tr><td>${nm}</td><td class=num>${n(w.pages)}</td>
      <td class=num>${w.ms||'—'}</td><td class=num>${cf}</td>
      <td class=num>${inflight[nm]||0}</td></tr>`;
  });
  document.getElementById('workers').innerHTML = rows.length ? rows.join('')
    : '<tr><td colspan=5 class=empty>no workers have reported yet</td></tr>';
}
tick(); setInterval(tick, 3000);
</script></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(200, DASH, "text/html; charset=utf-8")
        if u.path == "/api/stats":
            return self._send(200, json.dumps(stats()))
        if u.path == "/api/claim":
            w = (q.get("worker") or ["?"])[0][:60]
            n = min(64, max(1, int((q.get("n") or ["8"])[0])))
            return self._send(200, json.dumps({"pages": claim(w, n),
                                               "lease_s": LEASE_S}))
        if u.path == "/api/doc":
            # The worker fetches each PDF once and caches it, so this is served
            # rarely and simply.
            try:
                doc_id = int((q.get("id") or ["0"])[0])
            except ValueError:
                return self._send(400, json.dumps({"error": "bad id"}))
            with _LOCK:
                r = _c().execute("SELECT path FROM doc WHERE id=?", (doc_id,)).fetchone()
            if not r or not os.path.exists(r["path"]):
                return self._send(404, json.dumps({"error": "no such document"}))
            with open(r["path"], "rb") as fh:
                return self._send(200, fh.read(), "application/pdf")
        return self._send(404, json.dumps({"error": "no such path"}))

    def do_POST(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or "{}")
        except ValueError:
            return self._send(400, json.dumps({"error": "bad json"}))
        if u.path == "/api/result":
            got = submit(body.get("pages") or [])
            return self._send(200, json.dumps({"ok": True, "stored": got}))
        if u.path == "/api/chunk":
            archive.chunk_all()
            return self._send(200, json.dumps({"ok": True}))
        return self._send(404, json.dumps({"error": "no such path"}))


def serve(port=8430):
    with _LOCK:
        back = _reap(_c())
    s = stats()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print(f"\n  Archiver hub   http://0.0.0.0:{port}/")
    print(f"  corpus         {archive.DB}")
    print(f"  {s['docs']} documents, {s['total']:,} pages, {s['left']:,} to do")
    if back:
        print(f"  {back} expired claims returned to the pool")
    print(f"\n  point workers at it:\n"
          f"    archiver work --hub http://<this-host>:{port} --workers 16\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  hub stopped")
    return 0
