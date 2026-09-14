# Contributing

Patches welcome. A few things are load-bearing, so they are written down rather than left
for you to discover in review.

## Run the check first

```sh
python3 cockpit.py --selfcheck
```

It builds a corpus from nothing in a temp directory and runs it through ingest, chunking,
entity extraction and the graph reduction, then checks the ask-routing pattern and the
page. No network, no device, no third-party package, and it has to stay that way: it is
the only part that can be checked somewhere other than the machine it will live on. It
runs in CI on the app farm in `python:3.11-slim` with a read-only app directory, so write
temporary files to `/tmp`.

If you change behaviour, the check should fail before you fix it. It caught two of my own
mistakes while I was writing it, which is the only evidence a check is worth having.

## Standard library only

No dependencies. Not a purity thing: this ends up on machines where installing a package
is not an option, and a vault that needs a package index to start is not a vault. numpy is
used if it happens to be there and there is a working path without it.

If you genuinely need a dependency, open an issue first and say what breaks without it.

## Nothing that runs another program

`pdfsource.py` and `drivers.py` are the only files allowed to shell out, and they are
reached through `_poppler()` and `_drivers()` so the rest of the tool can be packaged
without them. Do not add `subprocess`, `os.system`, `ctypes`, `eval`, `exec` or
`__import__` anywhere else. The app farm scanner rejects all of them outright and there is
no permission you can add to get around it.

## Bad text is worse than no text

This ends up in something people consult when they cannot look anything up, and a page
that reads "50 mg" where the paper said "5 mg" gets quoted with total confidence. Every
page carries a confidence score, anything under the floor is quarantined rather than
indexed, and an answer that cites nothing is withheld no matter how well it reads.

If you are touching retrieval or answering, read `archivist.ask()` first. It has two
refusal gates and both exist because the failure they prevent looks exactly like success.

## Comments explain why

The code says what it does. A comment earns its place by saying why it is like that,
usually because something else was tried and broke. Real examples in here:

- `rfind` returns `-1`, which is truthy, so `or` does not catch it and the chunker spins.
- Accelerate raises spurious FP flags from inside a vectorised matmul on finite,
  unit-norm input, so the flags are suppressed and the output is checked instead.
- Reaping leases only on claim meant that once every worker exited, nothing ran the reaper
  again and a run sat at 99.7% forever.

Comments that restate the line above them get deleted in review.

## Touching the entity index

The filters there are ordered and each one exists because something specific got through:
the stop list, junk patterns, adjectival endings, possessive folding, case folding,
template-title detection, the heading rule, and the capitalisation ratio. Adding a filter
is fine. Before you do, check what it drops against a real corpus with
`python3 entities.py top 40`, and say in the commit message which real names survived.

Changing the entity extractor means rebuilding: `python3 entities.py build`. Changing the
chunker means re-embedding everything, which costs real time on a real corpus, so the
chunker drops stale vectors and tells you rather than leaving them pointing at text that
no longer exists.

## Commits

Say what broke and why the fix is the fix. Long is fine. The git log here is the design
document, and it is more use than one would be.

MIT, so contributions are under that.
