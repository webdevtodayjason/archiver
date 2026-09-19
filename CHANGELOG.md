# Changelog

Dates are when the work landed, not when it was tagged.

## Unreleased

## LAST LIGHT 0.1.1 - 18 September 2026

### The shelf it needed did not exist yet

LAST LIGHT reached the farm as 0.1.0 with nowhere to get a corpus from. The farm
build cannot ingest: turning a PDF into text needs poppler, reaching poppler
means starting another program, and the farm's archive scanner refuses a tree
that can do that. So the app was always going to download a shelf somebody else
had built, and until one was published it did the only honest thing available,
which was to say so and refuse.

The shelf is published now, as its own release on this repo: 5,955 documents,
5,934 Vikidia articles under CC BY-SA 3.0 and 21 survival and reference PDFs
that are free to pass on. It was cut from a copy of the working corpus, with the
88 documents whose terms could not be established in writing deleted along with
their pages, chunks, vectors and entities, and the file vacuumed so it carries
no trace of them. ATTRIBUTION.txt and SOURCES.txt travel inside it, and the app
refuses to unpack a bundle that has no attribution file, because a share-alike
obligation that depends on somebody remembering is one that gets missed.

The shelf has its own tag, so the app can be released again without republishing
73 MB of data, and all three of its constants read the environment first, which
is how a mirror gets pointed at.

## 0.1.3

### There was no way to load notes without a terminal

Started from the launcher on a machine that had never run it, Tiiny Brain came
up with an empty board and no way to point it anywhere. Loading notes was five
commands in one order: `md2jsonl.py`, `add-text`, `chunk`, `index`, then
`entities.py build`. Every one of them lives behind a shell prompt, and the
people this is built for do not open one. An empty board with nothing on it that
says how to fill it reads as a broken tool rather than an empty one.

The five commands are unchanged and still do what they did. What is new is that
all of them are reachable from the page.

The empty board now carries the door: "No notes yet. Point Tiiny Brain at a
folder of markdown", and a button. The same button is in the top bar, and the
same door is under Settings beside the vault folder. It takes a folder path,
with a folder browser served from this machine because the notes are already on
this machine and a browser upload would only make copies of them. `~/Documents`,
`~/Desktop`, an Obsidian vault and the iCloud Obsidian folder are offered as
shortcuts when they exist. The folder is remembered as the vault, so the folder
you load from is the folder a new note lands in.

One press runs the whole pipeline in a background thread: the walk, the filing,
the chunking, the embedding on the Tiiny, the name index. The page polls
`/api/ingest` and shows which of the five steps it is on and how far through.
The board redraws itself when it finishes.

Loading the same folder again is cheap. A note is filed under a hash of its
text, so an unchanged note is skipped before it is read any further and only new
or changed notes are chunked and embedded. A note edited on disk is re-read in
place, keeping its id, and its old vectors go with its old chunks.
`archive.chunk_all()` is deliberately not used: it empties the chunk table and
every vector with it, which is right for a rebuild and would cost a full
re-embed for one new note.

### Every step that cannot run says which step, and keeps what came before

A Tiiny with no embedding model loaded is a normal Tuesday, not a fault, and it
used to be indistinguishable from a Tiiny that was not there. The loader now
separates four answers, and in all of them the reading is kept: no Tiiny set up
yet, a Tiiny that did not answer, a Tiiny answering and serving no embedding
model (it names what it is serving instead), and a folder with no markdown in
it. The name index is built either way, because the names come out of the chunks
and need no device, so a person whose Tiiny is asleep still gets their notes on
the board and loses only the ability to ask. "Try again" picks up where it
stopped.

An empty board is never left without words on it. It says either "No notes yet"
or, once notes are in but no name yet recurs across enough of them to draw,
"Nothing to draw yet" with the count and where to read them.

PDFs in the folder are counted and reported as not loaded in this build. The two
OCR modules shell out to poppler and tesseract, and the farm's archive scanner
refuses a shipped tree that can start another program, so they stay behind.

### Three faults found while building it

`archivist.die()` leaves by `sys.exit`, which raises SystemExit, which is not an
Exception. A load started before the device was ever configured killed the
worker thread on the first embedding call and left the job marked running, so
the page reported "loading" for as long as it was open. The worker now catches
BaseException, and an unconfigured device is one of the four states above.

The cockpit caches its entity graph for the life of the process and a load is
the one thing that invalidates it. Clearing it through `import cockpit` reached
a second copy of the module: the app starts as `python3 cockpit.py`, so the
module answering requests is `__main__`. The board stayed empty after a load
while the vitals under it counted the new notes, which is the failure this
release exists to fix, reintroduced by an import.

`/api/ingest` and `/api/folders` are answered before a database connection is
opened. Rebuilding the name index holds a write transaction and opening a
connection runs the schema script, so during the last and slowest step of a load
every route that opens one waited on it, including the two the page polls.

### Two vaults no longer overwrite each other

A note is filed under the folder it came from as well as its path inside it. A
work vault and a personal one both have an Inbox at the top; filed on the
relative path alone, the second one loaded replaced the first and a note the
person could still see on disk left their board.

## 0.1.2

### A fresh install could not answer its own first two questions

Installed through the farm onto a Mac that had never run it, the page came up
and then sat there: every number in the top strip a dash, and in the console two
requests, to `/api/vitals` and `/api/settings`, that had returned nothing at
all. ERR_EMPTY_RESPONSE, then `TypeError: Failed to fetch`.

Nothing was wrong with the network. Both routes count rows in `entity`, and on a
database nobody had ever built an entity index in, that table did not exist.
sqlite raised, the exception went past the request handler into socketserver,
and socketserver's answer to a handler that raises is to print it and close the
connection having written nothing. A browser has no way to show that except as a
fetch that failed, so the page said nothing and the reason sat in farm.log.

Two faults, so two fixes.

The schema was spread across three modules, each creating its own tables the
first time that module did any work. archive.py made `doc`, `page` and `chunk`,
archivist.py made `vec` the first time anything was embedded, entities.py made
`entity` and `mention` the first time an extraction ran. A database was complete
only once all three had run, and the cockpit runs none of them, it only reads.
`archive.ensure_schema()` now creates the lot on every connection and the other
two modules call it instead of carrying their own half, so a fresh database and
a database left half built by 0.1.1 both come out complete.

And every route answers now. One try/except around the GET and POST dispatch
turns an exception into a 500 carrying its type and message, while the traceback
still goes to farm.log. A dropped socket tells nobody anything; a 500 in the
network tab names the route and the reason, which is the difference between this
taking two releases to notice and taking one page load.

### The corpus moves to the folder the farm keeps

The archive lived next to the code, in `corpus/` inside the install folder. The
farm unpacks each version into a folder of its own and throws the old one away
when it updates, so an index built under 0.1.1 would not have survived the
upgrade to 0.1.2. The farm hands every app a data directory that sits beside
those version folders for exactly this reason, so `FARM_DATA_DIR` is used when
it is set. `ARCHIVER_HOME` still wins over it, and a plain checkout with neither
set keeps its corpus next to the code, as it always did.

The notes were never at risk. They are markdown files in your own folder and
always have been, and the vault setting lives in `~/.config/tiiny-brain.json`.
What was at risk is the index over them, which is minutes of rebuilding rather
than anything lost.

### Also

- `/favicon.ico` answers 204, and the page carries its own mark inline, so the
  console no longer opens with a 404 that means nothing.
- `tests/test_fresh_install.py` opens a fresh database, starts the cockpit in a
  thread against an empty data directory, and asks it the two questions that
  failed. Standard library only and no subprocess, the same rule the shipped
  tree keeps.
- `ci.yml` runs those tests and the selfcheck on Linux, macOS and Windows.
  Windows had never been tested at all before this.
- `scripts/build-release.py` writes the release tarball from a file list that is
  written down now rather than remembered.

## 0.1.1

- The cockpit reads TIINYAPP_PORT when no port is given, so `farm start tiiny-brain --port N` moves it.

### The cockpit opens on a library of books

Pointed at LAST LIGHT, 6,043 documents over 18,539 pages and 27,746 chunks, the
cockpit drew an empty black canvas and stayed there. Every number in the top
strip was a dash and the badge said READY. Only the shelf rail filled, because
it is the one panel that does not touch the graph.

The graph query was still running. It takes **834 seconds** on that corpus, on
an M5 Max, and the page fires three requests that each want it, so three copies
ran at once. The join is over `mention`, which carries a row per chunk, and the
largest document here has 19,750 of them: 1.9 billion intermediate rows to
produce 1.5 million answers. `COUNT(DISTINCT a.doc_id)` was already collapsing
those duplicates, it was just paying for them first. Collapsing them before the
join instead gives the same rows in **3 seconds**, and the vault's output is
unchanged to the row.

That made the map appear, and the map was nonsense: `urethral / thrombosis`,
`Epilobium / Sagittaria`, Pokémon. Every edge sat exactly on the MIN_SHARED
floor and the ten highest-degree names were each in three documents. Two things
were wrong, and they are asked separately now, in `_shape()`.

Co-occurrence needs a unit that is about one thing. A note is. A 676-page
survival manual is not, so at document scale every term in it co-occurs with
every other, and three medical books that each contain both words somewhere
made a maximum-PMI edge. Where most of a corpus's pages sit inside multi-page
documents, co-occurrence moves to the chunk, which is the note-sized unit a
library already has. On LAST LIGHT that drops the candidate pairs from 1,512,160
to 336,609 and the top of the degree list stops being flukes: water, injury,
surgery, shelter, treatment.

The second thing was the population. `entities.py` keeps names and subjects in
one table, and a PMI taken across both compares two different measurements. A
subject averages 143 mentions here and a name 15, so the bigger population
wins on volume. Drawn together at chunk scale the map came out as Vikidia
geography: Bangladesh, Anne Boleyn, Thomas Astruc. Drawn as subjects alone it
comes out as the library: suture, fractures, tissue, closure, chlorination,
pickling. So where a subject index exists it is the one drawn, because running
`entities.py subjects` over a corpus says its proper nouns were not the answer.

Both switches are measured off the corpus and both are inert on a note vault:
it has no multi-page document and no subject row. Every cockpit endpoint was
diffed against the shipped build on the 2,378-note vault and is identical.

`MIN_SHARED` is 3 shared documents and stays 3. The chunk unit gets its own
floor of 8, because there are 4.6 times as many chunks as documents here and 3
of them is not the same claim. Measured at 3, 5, 8 and 12: at 3 the seeds are
still docs=3 flukes, at 8 they are water, plate, surgery, injuries, shelter.

### A book is not a note, and the cockpit stops pretending otherwise

The reader handed back `text[:60000]` with no page numbers. On *Nuclear War
Survival Skills* that is 5.5% of the book, 28 pages of 510, presented as the
book. 59 documents in this corpus were over that cap. It now reads whole pages,
marks each one, says "pages 1-22 of 510" in the header, and pages forward and
back. Copy and download carry the range too, so a slice does not come back later
looking like the whole thing.

Asking a question about a document had the same hole. The first 24,000
characters went to the model under a prompt swearing the document was
reproduced below, so the question was really being asked of the front matter.
Asked how much drinking water to store per person in a fallout shelter, the old
path answered "The provided note does not say" while the book answers it across
pages 107 to 313. The refusal gate was working; it was being fed the wrong 2% of
the book. When a document does not fit, its own chunks are ranked against the
question now and the best are sent in page order with their pages, and the
prompt says that is what they are. The same question comes back with the
quantity, the container and a page for each. Lexical rather than vector: the
field is already one document, and the device runs one inference at a time. A
note still goes over whole, under the original wording, unchanged.

The shelf list was `LIMIT 200`. That was the whole of a 90-note project and 3%
of a 5,934-article encyclopedia, in alphabetical order, with nothing saying so.
It now reports how many there are and takes a title filter, and it orders by
length, which is title order when every document is one page and puts the books
first when they are not. The rail says Shelves and documents rather than
Projects and notes when the corpus is a library.

### Audio overviews: two hosts, talking about your notes

`overview.py`, and a button next to Ask. Point it at a project, a name or the whole
archive and it writes a two host conversation about it and speaks it on the device. The
citation for every line sits under it in the rail, and clicking a line jumps the audio
there, so the thing it drew on is one click away rather than a claim.

The grounding had to survive the format and the format fights it. A cited answer shows
its working and the reader can look; speech goes past once and sounds equally confident
whatever it says. So the filter is mechanical, in the same shape as `archivist.ask()`:
every line must end with the passage it came from, a line citing nothing or citing a
passage that was not sent is deleted before it is synthesised, and the count of what was
deleted is reported rather than quietly shortening the show.

No new retrieval. An entity's passages come from `entities.mentions_of()`, a project's
topics from the shelf panel, and the archive's from the co-occurrence graph the cockpit
already reduces.

Two device facts shape the rest. It runs one inference at a time and caps a request near
220 seconds, so the script is written one section per call and every spoken line is its
own synthesis request, capped at 600 characters. Measured on a Tiiny Pocket the TTS runs
at 1.64x realtime, so that cap is about 25 seconds of work against a 220 second ceiling,
and the margin is there because the device is shared. Concatenation is the `wave` module
rather than ffmpeg, because nothing here is allowed to run another program.

Measured end to end on that device with the writing model on a separate box: five minutes
of audio from forty lines in about 220 seconds, roughly 20 seconds of that writing and
190 speaking.

### The entity index can tell a name from a word

`Use`, `Related`, `Tech Details`, `Full`, `Run`, `Core`, `Live` and `Path` were all
entities. Every structural filter passed them: hundreds of notes each, capitalised, not
mostly under a hash.

What separates them is how often a word is capitalised when it is used at all. A name
gets a capital every time because that is its name. An ordinary word gets one when it
opens a sentence and is lowercase the other eight hundred times. The two populations
barely touch, 0.72 to 0.99 against 0.06 to 0.57.

Counted over prose only, with URLs, paths, identifiers and backtick spans removed first.
A name that also lives in `github.com` and `node_modules` would otherwise be punished for
it: GitHub reads 0.57 raw and 0.99 on prose, and the second number is the true one.

On a 2,378 note vault: 4,889 entities down to 1,762, and the top twenty is now entirely
real.

### Names fold on case

`HOLACE` and `HoLaCe` were two rows for one name, and 261 names in that vault were split
the same way. Worse than untidy: `mentions_of()` matches `COLLATE NOCASE`, so it took
whichever row SQLite reached first and read half the mentions without saying so. HoLaCe
had been answering from 168 of its 174.

The surviving spelling is the one that actually appears most often, so `TTS` and `IDs`
keep their shape. Ties go to the form that is not all caps, then the longer, then
alphabetical, so two rebuilds of one corpus agree.

### Documentation

`archivist.py` 3 of 13 functions to 13 of 13, `hub.py` 2 of 12 to 12 of 12. Repo-wide 45%
to 64%. `INSTALL.md` added, and it splits at the top by which half of the tool you want,
because sending someone to `brew install poppler` when they only want to read their own
notes is how you lose them.

## 0.1.0 — 13 September 2026

First release. The archive published as **Tiiny Brain** is the notes half.

### The cockpit

A map of the names in your notes and the ones that keep turning up together. Click a name
and its neighbourhood pulls forward. Click a note and read it, copy it as markdown, or ask
about that note alone. Write a new note and it lands in your vault as markdown, gets
chunked and embedded, and is answerable a few seconds later.

### Two ways of answering

A question about a topic goes to nearest-neighbour search. A question about a name the
archive knows reads every passage that names it instead, which is a different and much
better set, and the answer says which path it took. Same corpus and same model: one
hedged sentence against a cited profile drawn from 136 notes.

### Setup, help, and knowing it works

A light in the top bar that is green only when the device answered, embeddings ran and the
answering model is reachable. Behind it, six live checks that name the broken link, and
fields for the host, port, key, an alternate chat endpoint and the vault folder. Settings
persist to `~/.config/tiiny-brain.json` at mode 600, and environment variables win over
that file so scripts keep working. Help is six tabs. An empty corpus opens it rather than
showing an empty room.

### poppler behind one import

`pdfsource.py` and `drivers.py` hold everything that runs another program, reached through
late imports. `archive.py` does not import `subprocess` at all. That is what lets the notes
half be packaged for somewhere that forbids it, and a missing module prints a sentence
rather than a traceback.

### `--selfcheck`

`cockpit.py --selfcheck` builds a corpus from nothing in a temp directory and runs it
through ingest, chunking, entity extraction and the graph reduction, then checks the
ask-routing pattern and the page. No network, no device, no third-party package.

### Fixed

- The rail sent its scope label with every question and `ask()` treated `everything` as an
  entity name, so unscoped questions were embedded as `everything: what is Keelpin` and a
  project with 56 notes could not be found.
- `set_vault()` overwrote the whole config file, which would have thrown away the endpoint
  the moment someone moved their vault.
- The chunker could hang. `buf.rfind(" ", 0, CHUNK_CHARS) or CHUNK_CHARS` does not catch
  `-1`, because `-1` is truthy, and two articles with no usable break in 35,000 characters
  spun it at full CPU for 25 minutes.
- Rebuilding chunks left the old vectors in place, still pointing at text that no longer
  existed. They are dropped now, with a line telling you to re-index.
- An entity query selected `title, source` but not `d.id`, so some notes in the inspector
  were unopenable and others were fine.
- The default graph was seeded from the highest-PMI edges. PMI peaks on rarity, so the
  first thing anyone saw was thirteen nodes of nothing. Seeded on degree now.
- Refusals claimed the collection lacked something, when all the tool can see is what it
  retrieved.
