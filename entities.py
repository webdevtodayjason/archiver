#!/usr/bin/env python3
"""An entity index over an archive, so questions about people survive retrieval.

Vector search answers questions about topics, because a chunk that is *about*
something embeds near a question about that thing. It cannot answer questions
about entities that own no document. "Richard" appears in 409 chunks of my vault
and not one of them means "who Richard is" - he is always mentioned while the
note is about something else, so the nearest neighbour to "who is Richard" is
whatever passage happens to name him in passing.

The fix is not a better embedding. It is a different index: who is named, where,
and near what. That is cheap to build and it is the half that retrieval is
structurally unable to do.

Extraction is deliberately dumb and fast - capitalised runs, filtered hard. No
model runs over the corpus. Inference is spent only on the question actually
asked, over the mentions this index already found.
"""
import collections
import os
import re
import sqlite3
import sys

import archive

# Words that start sentences, head sections, or otherwise capitalise without
# naming anything. The list is long because precision matters more than recall:
# a junk entity is visible in every answer, a missed one is merely absent.
STOP = set("""
the this that these those there their they them then than a an and or but if so
of in on at to for from with without into onto over under about after before
is are was were be been being do does did done has have had having
i you he she it we us our your my me his her its
what when where which who whom whose why how all any both each few more most
other some such no nor not only own same too very can will just should now
one two three four five six seven eight nine ten first second third next last
new old good great best better big small high low long short same different
note notes todo done next step steps plan plans goal goals task tasks
monday tuesday wednesday thursday friday saturday sunday
january february march april may june july august september october november
december mon tue wed thu fri sat sun jan feb mar apr jun jul aug sep oct nov dec
yes ok okay also still even much many well back down out up off here
readme overview summary status update updates changelog architecture
""".split())

# Shapes that are never a person or a project: file paths, code, versions, IDs.
JUNK = re.compile(r"^(?:[A-Z]{1,3}\d|v?\d|.*[/\\_@#].*|.*\d{3,}.*)$")

# A capitalised run: "Richard", "Richard Avery", "Forward Observer".
RUN = re.compile(r"\b([A-Z][a-zA-Z'’-]{1,20}(?:\s+[A-Z][a-zA-Z'’-]{1,20}){0,2})\b")

SCHEMA = """
CREATE TABLE IF NOT EXISTS entity (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  kind TEXT,                    -- person|project|org|place|thing|unknown
  mentions INTEGER NOT NULL,
  docs INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mention (
  entity_id INTEGER NOT NULL REFERENCES entity(id),
  doc_id INTEGER NOT NULL,
  chunk_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS mention_entity ON mention(entity_id);
CREATE INDEX IF NOT EXISTS mention_doc ON mention(doc_id);
"""


def _schema(c):
    c.executescript(SCHEMA)
    c.commit()


def candidates(text):
    """Capitalised runs that look like names, from one chunk."""
    out = set()
    for m in RUN.finditer(text):
        name = " ".join(m.group(1).split())
        low = name.lower()
        if low in STOP or JUNK.match(name):
            continue
        # A single word that is only capitalised because it starts a sentence is
        # noise. Require either two words, or a word seen mid-sentence.
        if " " not in name:
            i = m.start()
            before = text[max(0, i - 2):i]
            if i == 0 or before.strip().endswith((".", "!", "?", "\n", "-", "*")):
                continue
            if len(name) < 3:
                continue
        if all(w.lower() in STOP for w in name.split()):
            continue
        out.add(name)
    return out


def build(min_docs=3):
    """Index every capitalised name in the archive. No model involved."""
    c = archive.db()
    _schema(c)
    c.execute("DELETE FROM mention")
    c.execute("DELETE FROM entity")
    rows = c.execute("SELECT id, doc_id, text FROM chunk").fetchall()
    print(f"  scanning {len(rows):,} chunks")
    hits = collections.defaultdict(list)          # name -> [(doc_id, chunk_id)]
    for r in rows:
        for name in candidates(r["text"]):
            hits[name].append((r["doc_id"], r["id"]))
    kept = 0
    for name, ms in hits.items():
        docs = len({d for d, _ in ms})
        if docs < min_docs:
            continue
        cur = c.execute(
            "INSERT INTO entity(name,kind,mentions,docs) VALUES(?,?,?,?)",
            (name, None, len(ms), docs))
        eid = cur.lastrowid
        c.executemany("INSERT INTO mention(entity_id,doc_id,chunk_id) VALUES(?,?,?)",
                      [(eid, d, ch) for d, ch in ms])
        kept += 1
    c.commit()
    print(f"  {len(hits):,} candidates, {kept:,} kept (named in {min_docs}+ notes)")
    return 0


def top(n=25):
    c = archive.db()
    _schema(c)
    for r in c.execute("SELECT name,mentions,docs FROM entity "
                       "ORDER BY docs DESC, mentions DESC LIMIT ?", (n,)):
        print(f"  {r['docs']:>4} notes  {r['mentions']:>5} mentions   {r['name']}")
    return 0


def mentions_of(c, name, limit=40):
    """Every chunk naming this entity, best-connected notes first."""
    row = c.execute("SELECT id,name,mentions,docs FROM entity WHERE name=? COLLATE NOCASE",
                    (name,)).fetchone()
    if not row:
        return None, []
    rs = c.execute(
        "SELECT ch.text, d.title, d.source, ch.id "
        "FROM mention m JOIN chunk ch ON ch.id=m.chunk_id "
        "JOIN doc d ON d.id=ch.doc_id WHERE m.entity_id=? LIMIT ?",
        (row["id"], limit)).fetchall()
    return row, rs


def who(name, limit=30):
    """Answer from an entity's mentions instead of from nearest neighbours.

    This is the whole point. Retrieval hands the model the passages most like
    the question; for a person that is whichever note happens to name them near
    matching words. This hands it the passages that name them at all, which for
    someone mentioned across a hundred notes is a completely different set - and
    the only one that can say who they are.
    """
    import archivist
    c = archive.db()
    _schema(c)
    row, rs = mentions_of(c, name, limit)
    if not row:
        print(f"  no entity called {name!r} in this archive")
        return 1
    print(f"  {row['name']}: named in {row['docs']} notes, {row['mentions']} times")
    print(f"  reading {len(rs)} of them\n")
    passages = "\n\n".join(
        f"[{i+1}] ({r['title']} · {r['source']})\n{r['text'][:900]}"
        for i, r in enumerate(rs))
    model, base, key = archivist.pick_chat()
    body = {"model": model, "max_tokens": 700, "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {"role": "system", "content":
                 "You are reading someone's own notes to answer a question about a "
                 "recurring name in them. Every passage below mentions that name.\n\n"
                 "Say who or what it is, and what role it plays, using ONLY these "
                 "passages. Cite like [2] for every claim. Be concrete: if the "
                 "passages show a person doing particular things, say what. If the "
                 "name is a project or a tool rather than a person, say so.\n\n"
                 "If the passages genuinely do not establish it, say that plainly. "
                 "Do not guess, and do not pad."},
                {"role": "user", "content":
                 f"PASSAGES, all mentioning \"{row['name']}\":\n\n{passages}\n\n"
                 f"QUESTION: who or what is {row['name']}?"}]}
    d = archivist.api("/v1/chat/completions", body, timeout=420, base=base, key=key)
    print("  " + (d["choices"][0]["message"].get("content") or "").strip().replace("\n", "\n  "))
    print("\n  drawn from")
    seen = []
    for r in rs:
        tag = f"{r['title']} · {r['source']}"
        if tag not in seen:
            seen.append(tag)
    for t in seen[:8]:
        print(f"    {t}")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "top"
    if cmd == "build":
        sys.exit(build(int(sys.argv[2]) if len(sys.argv) > 2 else 3))
    if cmd == "who":
        sys.exit(who(" ".join(sys.argv[2:])))
    if cmd == "top":
        sys.exit(top(int(sys.argv[2]) if len(sys.argv) > 2 else 25))
    sys.exit("usage: entities.py build [min_docs] | top [n] | who <name>")
