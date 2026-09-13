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

# Generic technical vocabulary. These capitalise like names because they are
# acronyms, and they are everywhere, so no statistic separates them from a real
# product name - "API" sits in 590 notes and "ArgentOS" in 492. A list is the
# honest tool here: it is small, it is obvious, and a classifier for it would be
# a worse version of writing it down.
TECH = set("""
api cli url uri json html css sql sdk ui ux id ide cpu gpu ram ssd nvme ssh dns
tls ssl jwt cors crud rest rpc grpc http https yaml toml csv tsv pdf png jpg svg
gif mp3 mp4 wav get post put patch delete head options ok todo fixme note warning
error info debug trace readme license mit utc am pm env var dir src bin tmp log
logs npm pip git repo repos pr prs ci cd vm vms os io db sqlite postgres redis
gateway phase see status state config settings setup install usage example
examples docs doc test tests build deploy release version versions changelog
lessons learned overview summary architecture roadmap glossary
""".split())

# Shapes that are never a person or a project: file paths, code, versions, IDs.
JUNK = re.compile(r"^(?:[A-Z]{1,3}\d|v?\d|.*[/\\_@#].*|.*\d{3,}.*)$")

# "Evalchemy-style", "Cloudflare DNS-only", "GUI-launched LoRA". These describe a
# thing rather than name one, and they arrive as capitalised runs like any name.
ADJECTIVAL = re.compile(
    r"-(?:style|only|based|driven|aware|safe|ready|first|native|backed|"
    r"launched|facing|side|level|wide|less|like|ish|free|proof)\b", re.I)

# Words that lead a sentence into a name: "What HoLaCe does", "The Titanium Lab".
# Strip them rather than dropping the run, or "What HoLaCe" becomes an entity in
# its own right and splits HoLaCe's mentions across two rows.
LEAD = {"what", "the", "a", "an", "this", "that", "these", "those", "our", "my",
        "your", "his", "her", "its", "their", "all", "some", "any", "each",
        "every", "both", "no", "new", "old", "next", "last", "first", "other",
        "same", "see", "note", "why", "how", "when", "where", "which", "who"}

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


HEADING = re.compile(r"^\s{0,3}#{1,6}\s", re.M)


def _heading_spans(text):
    """Character ranges covered by markdown headings."""
    spans = []
    for m in HEADING.finditer(text):
        end = text.find("\n", m.end())
        spans.append((m.start(), end if end != -1 else len(text)))
    return spans


def candidates(text):
    """Capitalised runs that look like names, and whether each sat in a heading.

    Returns {name: in_heading_count}. Section headings capitalise exactly like
    proper nouns, and they are frequent, so neither casing nor document
    frequency separates "Known Gotchas" from "ArgentOS" - one is in 435 notes
    and the other in 492. Where the name sits does separate them: a heading is
    written with a hash in front of it, and a product name is written in a
    sentence.
    """
    spans = _heading_spans(text)
    def in_heading(i):
        return any(a <= i < b for a, b in spans)
    out = {}
    for m in RUN.finditer(text):
        name = " ".join(m.group(1).split())
        # "Richard's" and "Richard" are the same person; keeping both splits a
        # hundred mentions across two rows and halves everything downstream.
        name = re.sub(r"[\u2019']s$", "", name)
        parts = name.split()
        while len(parts) > 1 and parts[0].lower() in LEAD:
            parts.pop(0)
        name = " ".join(parts)
        low = name.lower()
        if not name or low in STOP or JUNK.match(name) or ADJECTIVAL.search(name):
            continue
        if low in TECH or all(w.lower() in TECH for w in name.split()):
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
        prev = out.get(name, 0)
        out[name] = prev + (1 if in_heading(m.start()) else 0)
    return out


def fold_case(hits, seen, heads):
    """HOLACE and HoLaCe are one name. Merge them and keep the better spelling.

    Two rows for one name is not just untidy: mentions_of() looks the name up
    COLLATE NOCASE, so it matches whichever row SQLite reaches first and silently
    reads half the mentions. Folding here is what makes that lookup honest.

    The surviving spelling is the one that actually appears most often, so an
    acronym that is genuinely upper case (TTS, RLS, IDs) keeps its shape while a
    name that is merely shouted in a heading loses to its ordinary form. Ties go
    to the form that is not all-caps, then the longer one, then alphabetical, so
    two rebuilds of the same corpus agree.
    """
    groups = collections.defaultdict(list)
    for name in hits:
        groups[name.lower()].append(name)
    h, sn, hd = {}, collections.Counter(), collections.Counter()
    for forms in groups.values():
        best = max(forms, key=lambda n: (seen[n], n.upper() != n, len(n), n))
        # a chunk naming both spellings is still one mention of one name
        h[best] = list(dict.fromkeys(m for n in forms for m in hits[n]))
        sn[best] = sum(seen[n] for n in forms)
        hd[best] = sum(heads[n] for n in forms)
    return h, sn, hd


def _fold_selftest():
    C = collections.Counter
    hits = {"HoLaCe": [(1, 1), (2, 2)], "HOLACE": [(3, 3), (1, 1)],
            "TTS": [(4, 4)], "Tts": [(5, 5)]}
    seen = C({"HoLaCe": 168, "HOLACE": 6, "TTS": 40, "Tts": 1})
    h, sn, _ = fold_case(hits, seen, C())
    assert set(h) == {"HoLaCe", "TTS"}, h            # merged, best spelling kept
    assert h["HoLaCe"] == [(1, 1), (2, 2), (3, 3)], h["HoLaCe"]   # (1,1) not doubled
    assert sn["HoLaCe"] == 174, sn
    # an all-caps acronym only loses when the other spelling is genuinely commoner
    h2, _, _ = fold_case({"PATH": [(1, 1)], "Path": [(2, 2)]},
                         C({"PATH": 140, "Path": 93}), C())
    assert set(h2) == {"PATH"}, h2
    # a true tie goes to the mixed-case form, not the shout
    h3, _, _ = fold_case({"WAVE": [(1, 1)], "Wave": [(2, 2)]},
                         C({"WAVE": 9, "Wave": 9}), C())
    assert set(h3) == {"Wave"}, h3
    return "fold: 4/4"


def build(min_docs=3):
    """Index every capitalised name in the archive. No model involved."""
    c = archive.db()
    _schema(c)
    c.execute("DELETE FROM mention")
    c.execute("DELETE FROM entity")
    rows = c.execute("SELECT id, doc_id, text FROM chunk").fetchall()
    print(f"  scanning {len(rows):,} chunks")
    hits = collections.defaultdict(list)          # name -> [(doc_id, chunk_id)]
    heads = collections.Counter()
    seen = collections.Counter()
    for r in rows:
        for name, h in candidates(r["text"]).items():
            hits[name].append((r["doc_id"], r["id"]))
            seen[name] += 1
            heads[name] += 1 if h else 0
    hits, seen, heads = fold_case(hits, seen, heads)
    kept = 0
    # A name that titles notes in many unrelated projects is a section of the
    # author's own note template, not a subject. "Known Gotchas" titles 49 notes
    # across 49 projects; ArgentOS titles 7 and is a real thing. Frequency in the
    # body cannot tell these apart - both are everywhere - but this can.
    template = set()
    for r in c.execute(
            "SELECT title, COUNT(DISTINCT source) srcs, COUNT(*) n FROM doc "
            "GROUP BY title HAVING srcs >= 5 AND n >= 8").fetchall():
        t = re.sub(r"^\s*\d+\s*[-–—.]\s*", "", r["title"]).strip()
        if t:
            template.add(t.lower())
    dropped_template = 0
    dropped_headings = 0
    for name, ms in hits.items():
        docs = len({d for d, _ in ms})
        if docs < min_docs:
            continue
        # Mostly seen under a hash: it is a section of a document, not a thing
        # the documents are about.
        if name.lower() in template:
            dropped_template += 1
            continue
        if seen[name] >= 4 and heads[name] / seen[name] > 0.6:
            dropped_headings += 1
            continue
        cur = c.execute(
            "INSERT INTO entity(name,kind,mentions,docs) VALUES(?,?,?,?)",
            (name, None, len(ms), docs))
        eid = cur.lastrowid
        c.executemany("INSERT INTO mention(entity_id,doc_id,chunk_id) VALUES(?,?,?)",
                      [(eid, d, ch) for d, ch in ms])
        kept += 1
    c.commit()
    print(f"  {len(hits):,} candidates, {kept:,} kept (named in {min_docs}+ notes), "
          f"{dropped_template:,} dropped as note-template sections, "
          f"{dropped_headings:,} as headings")
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


def profile(name, limit=30):
    """Answer from an entity's mentions instead of from nearest neighbours.

    This is the whole point. Retrieval hands the model the passages most like
    the question; for a person that is whichever note happens to name them near
    matching words. This hands it the passages that name them at all, which for
    someone mentioned across a hundred notes is a completely different set - and
    the only one that can say who they are.

    Returns the answer rather than printing it, so the cockpit can use the same
    path the CLI does.
    """
    import archivist
    c = archive.db()
    _schema(c)
    row, rs = mentions_of(c, name, limit)
    if not row:
        return {"error": f"no entity called {name!r} in this archive"}
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
    seen = []
    for r in rs:
        tag = f"{r['title']} · {r['source']}"
        if tag not in seen:
            seen.append(tag)
    return {"name": row["name"], "docs": row["docs"], "mentions": row["mentions"],
            "read": len(rs), "sources": seen,
            "answer": (d["choices"][0]["message"].get("content") or "").strip()}


def who(name, limit=30):
    d = profile(name, limit)
    if d.get("error"):
        print("  " + d["error"])
        return 1
    print(f"  {d['name']}: named in {d['docs']} notes, {d['mentions']} times")
    print(f"  reading {d['read']} of them\n")
    print("  " + d["answer"].replace("\n", "\n  "))
    print("\n  drawn from")
    for t in d["sources"][:8]:
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
