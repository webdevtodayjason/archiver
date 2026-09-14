# Changelog

Dates are when the work landed, not when it was tagged.

## Unreleased

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
