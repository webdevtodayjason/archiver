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
import math
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

wikipedia wikimedia vikidia commons reflist infobox template templates category
categories citation cite portal redirect stub disambiguation namespace thumb
authors contents references external links see also further reading
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


# A hash is not the only way people write a heading. "**Related:**" and
# "Tech Details:" at the start of a line are the same gesture in markdown, and
# the names that sit in them are section labels, not subjects.
HEADING = re.compile(r"^\s{0,3}(?:#{1,6}\s|\*\*[^*\n]{1,40}\*\*\s*:?\s*$"
                     r"|[A-Z][\w .'-]{0,38}:\s*$)", re.M)


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


# A name written inside a URL, a path, an identifier or a backtick span is not
# the English word. github.com and node_modules say nothing about how anyone
# uses the word, and counting them makes a real name look like a common one.
NOISE = re.compile(r"""https?://\S+ | www\.\S+
                     | \b\S+\.(?:com|org|net|io|dev|app|ai|cc|sh|me)\b
                     | [\w.-]*[/\\][\w./\\-]*
                     | \b\w+_\w+\b | `[^`]*` | \$\w+""", re.X)
WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
CAP_FLOOR = 0.65


def case_ratios(rows):
    """How often each word is capitalised when it is used at all.

    This is the signal that separates a name from a word. "Keelpin" is written
    with a capital every time anyone mentions it, because that is its name.
    "run" is written with a capital when it happens to open a sentence or a
    bullet, and lowercase the other eight hundred times. Nothing else available
    without a model tells those two apart: both are capitalised often, both are
    in hundreds of notes, and both survive every structural filter.

    Counted over prose only, because a name that also appears in URLs, paths and
    identifiers would otherwise be punished for it. GitHub reads 0.57 against
    raw text and 0.99 against prose, and the second number is the true one.
    """
    form = collections.Counter()
    for r in rows:
        for w in WORD.findall(NOISE.sub(" ", r["text"])):
            form[w] += 1
    anycase = collections.Counter()
    for w, n in form.items():
        anycase[w.lower()] += n
    return form, anycase


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
    form, anycase = case_ratios(rows)

    def always_capitalised(name):
        """Multi-word names are judged on their least name-like word."""
        parts = name.split()
        rs = []
        for w in parts:
            t = anycase[w.lower()]
            rs.append(form[w] / t if t else 1.0)
        return min(rs) if rs else 1.0

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
    dropped_common = 0
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
        if always_capitalised(name) < CAP_FLOOR:
            dropped_common += 1
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
          f"{dropped_headings:,} as headings, "
          f"{dropped_common:,} as ordinary words that are merely capitalised")
    return 0


TERM = re.compile(r"[a-z][a-z-]{3,}")
DF_CEILING = 0.12


def subjects(min_docs=3, keep=2500):
    """Index what a reference library is *about*, which is not its proper nouns.

    The capitalised-run extractor is right for a personal vault, where the things
    worth asking about are named: Richard, Keelpin, ArgentOS. It is useless on a
    survival library, where the things worth asking about are ordinary lowercase
    nouns. "deadfall" appears in 180 chunks here and was never an entity, and
    neither were tinder, snare or tourniquet. What the capitalised pass gives you
    instead is United States, French and English, which nobody is going to ask.

    A subject is a word a few documents are heavily about and the rest never use.
    That is two signals multiplied: how many times it turns up in a document that
    mentions it at all, and how few documents those are. Generic words fail the
    first (people: 5 uses per document), common words fail the second (water: 547
    documents). deadfall gets 18 uses across 10 documents and scores 117.

    Stored alongside the names with kind='subject', so one lookup answers either
    kind and the mention path works for both.
    """
    c = archive.db()
    _schema(c)
    rows = c.execute("SELECT id, doc_id, text FROM chunk").fetchall()
    ndocs = c.execute("SELECT COUNT(*) n FROM doc").fetchone()["n"] or 1
    print(f"  scanning {len(rows):,} chunks for subjects")

    df = collections.defaultdict(set)        # term -> docs
    tot = collections.Counter()              # term -> occurrences
    where = collections.defaultdict(set)     # term -> chunks
    for r in rows:
        for w in set(TERM.findall(r["text"].lower())):
            where[w].add(r["id"])
        for w in TERM.findall(r["text"].lower()):
            if w in STOP:
                continue
            df[w].add(r["doc_id"])
            tot[w] += 1

    ceiling = max(min_docs, int(ndocs * DF_CEILING))
    scored = []
    for w, docs in df.items():
        n = len(docs)
        if n < min_docs or n > ceiling:
            continue
        # "somethin" and "goin" are a dropped g, not a subject. Transcribed
        # speech is bursty in exactly the way a real subject is, and the only
        # honest tell is that the spelled-out word is right there in the corpus
        # and commoner.
        if w[-1] != "g" and len(df.get(w + "g", ())) > n:
            continue
        scored.append((tot[w] / n * math.log(ndocs / n), w, n, tot[w]))
    scored.sort(reverse=True)
    scored = scored[:keep]

    c.execute("DELETE FROM mention WHERE entity_id IN "
              "(SELECT id FROM entity WHERE kind='subject')")
    c.execute("DELETE FROM entity WHERE kind='subject'")
    for _, w, n, occ in scored:
        cur = c.execute("INSERT INTO entity(name,kind,mentions,docs) VALUES(?,?,?,?)",
                        (w, "subject", occ, n))
        eid = cur.lastrowid
        ch = sorted(where[w])
        bydoc = {}
        for cid in ch:
            bydoc[cid] = None
        c.executemany("INSERT INTO mention(entity_id,doc_id,chunk_id) "
                      "SELECT ?, doc_id, id FROM chunk WHERE id=?",
                      [(eid, cid) for cid in ch])
    c.commit()
    print(f"  {len(df):,} candidate terms, {len(scored):,} kept as subjects")
    if scored:
        print("  top: " + ", ".join(w for _, w, _, _ in scored[:12]))
    return 0


def top(n=25):
    c = archive.db()
    _schema(c)
    for r in c.execute("SELECT name,mentions,docs FROM entity "
                       "ORDER BY docs DESC, mentions DESC LIMIT ?", (n,)):
        print(f"  {r['docs']:>4} notes  {r['mentions']:>5} mentions   {r['name']}")
    return 0


def mentions_of(c, name, limit=40):
    """A sample of the passages naming this entity, spread across documents.

    Spread matters more than it looks. A LIMIT with no ordering takes whichever
    rows the join reaches first, which clusters by document: asking about a
    deadfall returned twelve passages from one survival handbook and seven from
    the book actually called Deadfalls and Snares. Taking a few from each source
    in turn reads the same number of passages and covers six books instead of
    leaning on one, which is the whole reason this path beats search.

    Documents with more to say still get more of the budget, because the
    round-robin keeps going while they still have passages left.
    """
    row = c.execute("SELECT id,name,mentions,docs FROM entity WHERE name=? COLLATE NOCASE",
                    (name,)).fetchone()
    if not row:
        return None, []
    rs = c.execute(
        "SELECT ch.text, d.title, d.source, ch.id, ch.doc_id "
        "FROM mention m JOIN chunk ch ON ch.id=m.chunk_id "
        "JOIN doc d ON d.id=ch.doc_id WHERE m.entity_id=? ORDER BY ch.doc_id, ch.id",
        (row["id"],)).fetchall()
    bydoc = collections.OrderedDict()
    for r in rs:
        bydoc.setdefault(r["doc_id"], []).append(r)
    out = []
    while len(out) < limit and bydoc:
        for k in list(bydoc):
            if not bydoc[k]:
                del bydoc[k]
                continue
            out.append(bydoc[k].pop(0))
            if len(out) >= limit:
                break
    return row, out


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


ASKING_ABOUT = re.compile(
    r"^\s*(?:who|what)(?:'s|\s+is|\s+are|\s+was|\s+were)\s+(.{2,60}?)\s*\??$", re.I)


def asked_about(question, min_docs=3):
    """The indexed name a question is about, if it is about one at all.

    "who is Richard" and "what is a deadfall" are not retrieval questions.
    Nearest-neighbour search hands back the passages most like the question; for
    a name or a subject, what answers it is every passage that mentions it, which
    is a completely different set. This is the test for which kind of question
    just arrived.

    Deliberately narrow. "what did we decide about Turnstile" stays on retrieval,
    because it is a question about an event and not a request for a definition.
    """
    m = ASKING_ABOUT.match(question or "")
    if not m:
        return None
    name = re.sub(r"^(?:the|a|an)\s+", "", m.group(1).strip(), flags=re.I)
    c = archive.db()
    _schema(c)
    row = c.execute("SELECT name FROM entity WHERE name=? COLLATE NOCASE AND docs >= ?",
                    (name, min_docs)).fetchone()
    return row["name"] if row else None


def _routing_selftest():
    ok = lambda q, want: ASKING_ABOUT.match(q) and \
        re.sub(r"^(?:the|a|an)\s+", "", ASKING_ABOUT.match(q).group(1).strip(), flags=re.I) == want
    assert ok("who is Richard", "Richard")
    assert ok("What is a deadfall?", "deadfall")
    assert ok("what's HoLaCe", "HoLaCe")
    assert ASKING_ABOUT.match("what did we decide about Turnstile") is None
    assert ASKING_ABOUT.match("who should own the deploy step") is None
    return "routing: 5/5"


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
    if cmd == "subjects":
        sys.exit(subjects(int(sys.argv[2]) if len(sys.argv) > 2 else 3))
    if cmd == "top":
        sys.exit(top(int(sys.argv[2]) if len(sys.argv) > 2 else 25))
    sys.exit("usage: entities.py build [min_docs] | subjects [min_docs] "
             "| top [n] | who <name>")
