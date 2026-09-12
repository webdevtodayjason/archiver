#!/usr/bin/env python3
"""The Archivist - answers out of the vault, or says the vault does not cover it.

    archivist index                    embed every chunk that is not embedded yet
    archivist ask "how do I purify water"
    archivist refusal-test             the test that matters most

This is the half of LAST LIGHT that talks to a person. The Archiver did the
grunt work; this reads what it produced and nothing else.

The one rule everything else serves: the model is never the source of truth.
A wrong answer here is not an inconvenience, it is someone drinking bad water
or taking ten times a dose. So the model's job is to find the right passage and
explain it, never to know things, and that is enforced in three places rather
than trusted to a prompt:

  Retrieval happens first and the answer is built only from what came back.
  The prompt forbids outside knowledge and requires a [n] citation per claim.
  An answer that cites nothing, or cites a passage that was not retrieved, is
  rejected mechanically before a human ever sees it.

The third one is the point. A prompt is a request; a validator is a rule. An
instruction to cite can be ignored silently, and the failure looks exactly like
a good answer.
"""
import argparse
import json
import math
import os
import sqlite3
import struct
import sys
import time
import urllib.request

import archive

HOST = os.environ.get("TIINY_HOST", "")
KEY = os.environ.get("TIINY_KEY", "")
PORT = os.environ.get("TIINY_PORT", "8800")
EMBED_MODEL = os.environ.get("LASTLIGHT_EMBED", "Qwen/Qwen3-Embedding-0.6B")
RERANK_MODEL = os.environ.get("LASTLIGHT_RERANK", "Qwen/Qwen3-Reranker-0.6B")
CHAT_MODEL = os.environ.get("LASTLIGHT_CHAT", "")

TOP_RETRIEVE = 30
TOP_ANSWER = 6
# Below this the best passage is not really about the question, and answering
# from it is how a vault starts inventing things.
MIN_SIM = float(os.environ.get("LASTLIGHT_MIN_SIM", "0.28"))

REFUSAL = "The vault does not cover that."

SYSTEM = """You are the Archivist of a knowledge vault. Someone is asking you a \
question because they cannot look anything up any other way.

You answer ONLY from the numbered passages given to you. You have no other \
knowledge. If the passages do not answer the question, you say exactly: \
"{refusal}" and nothing else.

Every factual claim carries a citation like [2] naming the passage it came from. \
Never write a claim without one. Never say "I know" - say "the archive says".

Be plain and practical. The person may be frightened, cold, or hurt. Short \
sentences. No preamble."""


def die(msg):
    sys.exit(f"  {msg}")


def api(path, body, timeout=300):
    if not (HOST and KEY):
        die("Set TIINY_HOST and TIINY_KEY.")
    req = urllib.request.Request(
        f"http://{HOST}:{PORT}{path}", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ------------------------------------------------------------------ index
def _schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS vec (
                   chunk_id INTEGER PRIMARY KEY REFERENCES chunk(id),
                   dim INTEGER NOT NULL,
                   v BLOB NOT NULL)""")
    c.commit()


def embed(texts):
    d = api("/v1/embeddings", {"model": EMBED_MODEL, "input": texts})
    if "data" not in d:
        raise RuntimeError(str(d)[:160])
    return [row["embedding"] for row in d["data"]]


def norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def index(batch=16, limit=None):
    """Embed chunks that have no vector yet. Resumable: it only ever does the
    ones that are missing, so an interrupted index costs one batch."""
    c = archive.db()
    _schema(c)
    q = ("SELECT ch.id, ch.text FROM chunk ch LEFT JOIN vec v ON v.chunk_id=ch.id "
         "WHERE v.chunk_id IS NULL ORDER BY ch.id")
    if limit:
        q += f" LIMIT {int(limit)}"
    todo = c.execute(q).fetchall()
    if not todo:
        n = c.execute("SELECT COUNT(*) n FROM vec").fetchone()["n"]
        print(f"  nothing to embed; {n:,} chunks already indexed")
        return 0
    print(f"  embedding {len(todo):,} chunks\n")
    t0 = time.time()
    done = 0
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        try:
            vecs = embed([r["text"][:2000] for r in part])
        except Exception as exc:  # noqa: BLE001
            print(f"  batch failed ({str(exc)[:70]}), stopping; rerun to continue")
            break
        for r, v in zip(part, vecs):
            vn = norm(v)
            c.execute("INSERT OR REPLACE INTO vec(chunk_id,dim,v) VALUES(?,?,?)",
                      (r["id"], len(vn), struct.pack(f"{len(vn)}f", *vn)))
        c.commit()
        done += len(part)
        el = time.time() - t0
        print(f"  {done:>7,}/{len(todo):,}  {done/el:6.1f}/s  "
              f"{(len(todo)-done)/(done/el)/60:5.1f}min left", end="\r", flush=True)
    print(f"\n\n  {done:,} chunks embedded in {(time.time()-t0)/60:.1f} min")
    return 0


# ------------------------------------------------------------------ search
def _load(c):
    rows = c.execute("SELECT chunk_id, dim, v FROM vec").fetchall()
    if not rows:
        die("Nothing is indexed. Run: archivist index")
    ids = [r["chunk_id"] for r in rows]
    dim = rows[0]["dim"]
    try:
        import numpy as np
        m = np.frombuffer(b"".join(r["v"] for r in rows), dtype="<f4").reshape(len(rows), dim)
        return ids, m, True
    except ImportError:
        # Works without numpy so this can run on a bare Pi. Slower, but a vault
        # that only runs on machines with a scientific stack is not a vault.
        return ids, [struct.unpack(f"{dim}f", r["v"]) for r in rows], False


def search(c, question, k=TOP_RETRIEVE):
    ids, mat, fast = _load(c)
    qv = norm(embed([question])[0])
    if fast:
        import numpy as np
        sims = mat @ np.asarray(qv, dtype="<f4")
        order = sims.argsort()[::-1][:k]
        hits = [(ids[i], float(sims[i])) for i in order]
    else:
        sims = [(cid, sum(a * b for a, b in zip(row, qv)))
                for cid, row in zip(ids, mat)]
        sims.sort(key=lambda x: -x[1])
        hits = sims[:k]
    out = []
    for cid, s in hits:
        r = c.execute(
            "SELECT ch.text, ch.page_from, ch.page_to, d.title, d.source "
            "FROM chunk ch JOIN doc d ON d.id=ch.doc_id WHERE ch.id=?", (cid,)).fetchone()
        if r:
            out.append({"id": cid, "sim": round(s, 4), "text": r["text"],
                        "title": r["title"], "source": r["source"],
                        "pages": [r["page_from"], r["page_to"]]})
    return out


def cite(h):
    p = (f"p{h['pages'][0]}" if h["pages"][0] == h["pages"][1]
         else f"pp{h['pages'][0]}-{h['pages'][1]}")
    return f"{h['title']} · {p}"


def pick_chat():
    if CHAT_MODEL:
        return CHAT_MODEL
    req = urllib.request.Request(f"http://{HOST}:{PORT}/v1/models",
                                 headers={"Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        ids = [m["id"] for m in json.load(r).get("data", [])]
    for m in ids:
        if any(t in m for t in ("Ornith", "Qwen3.6", "Qwen3-30B", "Qwen3-8B", "gpt-oss")):
            return m
    die("No chat model is loaded. Load one, or set LASTLIGHT_CHAT.")


def ask(question, show_sources=True, quiet=False):
    c = archive.db()
    _schema(c)
    hits = search(c, question)
    if not hits or hits[0]["sim"] < MIN_SIM:
        # Nothing retrieved is close enough to be about this. Refuse here, before
        # a model gets the chance to be helpful about it.
        if not quiet:
            print(f"\n  {REFUSAL}")
            if hits:
                print(f"  (closest passage scored {hits[0]['sim']:.2f}, "
                      f"below the {MIN_SIM} floor)")
        return {"answer": REFUSAL, "refused": True, "hits": hits[:3]}

    top = hits[:TOP_ANSWER]
    passages = "\n\n".join(
        f"[{i+1}] ({cite(h)})\n{h['text'][:1200]}" for i, h in enumerate(top))
    model = pick_chat()
    body = {"model": model, "max_tokens": 700, "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {"role": "system", "content": SYSTEM.format(refusal=REFUSAL)},
                {"role": "user",
                 "content": f"PASSAGES FROM THE VAULT:\n\n{passages}\n\n"
                            f"QUESTION: {question}"}]}
    d = api("/v1/chat/completions", body, timeout=420)
    answer = (d["choices"][0]["message"].get("content") or "").strip()

    # The mechanical guard. An instruction to cite can be ignored silently and
    # the result looks exactly like a good answer, so the citations are checked
    # rather than trusted.
    import re
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
    valid = cited & set(range(1, len(top) + 1))
    refused = answer.startswith(REFUSAL[:20])
    if not refused and not valid:
        answer = (REFUSAL + "\n  (an answer was produced but cited no passage, "
                  "so it was withheld)")
        refused = True
    bad = cited - set(range(1, len(top) + 1))

    if not quiet:
        print(f"\n  {answer}\n")
        if bad:
            print(f"  warning: cited passages that do not exist: {sorted(bad)}")
        if show_sources and not refused:
            print("  sources")
            for i, h in enumerate(top, 1):
                mark = "*" if i in valid else " "
                print(f"   {mark}[{i}] {cite(h)}   sim {h['sim']:.2f}")
    return {"answer": answer, "refused": refused, "cited": sorted(valid),
            "sources": [cite(h) for h in top], "model": model}


# ---------------------------------------------------------------- the test
OUT_OF_SCOPE = [
    "what is the capital of France",
    "who won the 2022 world cup",
    "write me a python function to sort a list",
    "what is the airspeed velocity of an unladen swallow",
    "how do I reset my iPhone password",
]


def refusal_test():
    """The test that decides whether this is a tool or a liability.

    Every question here is something a capable model certainly knows and the
    vault certainly does not contain. Each one must be refused. A single
    confident answer means the Archivist is drawing on the model rather than
    the archive, and everything it says becomes untrustworthy."""
    print("\n  Asking things the vault does not contain.")
    print("  Every one must be refused.\n")
    passed = failed = 0
    for q in OUT_OF_SCOPE:
        r = ask(q, quiet=True)
        ok = r["refused"]
        passed += ok
        failed += not ok
        print(f"  {'refused ' if ok else 'ANSWERED'}  {q}")
        if not ok:
            print(f"            -> {r['answer'][:110]}")
    print(f"\n  {passed}/{len(OUT_OF_SCOPE)} refused")
    if failed:
        print("\n  The Archivist answered from its own knowledge. That is the one")
        print("  failure that makes the vault unusable, because a reader cannot")
        print("  tell which answers came from the archive.")
    return 0 if not failed else 1


def main():
    p = argparse.ArgumentParser(prog="archivist")
    sub = p.add_subparsers(dest="cmd")
    i = sub.add_parser("index")
    i.add_argument("--batch", type=int, default=16)
    i.add_argument("--limit", type=int)
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--no-sources", action="store_true")
    s = sub.add_parser("search"); s.add_argument("question"); s.add_argument("-k", type=int, default=6)
    sub.add_parser("refusal-test")
    n = p.parse_args()

    if n.cmd == "index":
        return index(n.batch, n.limit)
    if n.cmd == "ask":
        ask(n.question, show_sources=not n.no_sources)
        return 0
    if n.cmd == "search":
        c = archive.db(); _schema(c)
        for h in search(c, n.question, n.k):
            print(f"\n  {h['sim']:.3f}  {cite(h)}")
            print(f"    {' '.join(h['text'][:200].split())}…")
        return 0
    if n.cmd == "refusal-test":
        return refusal_test()
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
