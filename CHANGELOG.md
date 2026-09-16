# Changelog

Dates are when the work landed, not when it was tagged.

## Unreleased

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
