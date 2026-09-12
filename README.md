# Archiver

Turns a shelf of PDFs into text an AI can **cite**.

Built for [LAST LIGHT](https://lastlight.cc), the off-grid knowledge vault, where the
Archivist answers out of a corpus and never out of its own head. But it has an ordinary
use too: point it at a box of scanned documents and get searchable, quotable text back.

```bash
archiver engines                 # what OCR is usable here, and why not
archiver add ~/shelf --source "Survivor Library"
archiver run                     # safe to stop and restart
archiver status
archiver search "tanning"
archiver export ./out            # chunks.jsonl, ready to embed
```

Python standard library, plus poppler for PDFs and whichever OCR engine you have.

## Three rules it is built around

**Never OCR what you can already read.** Most "scans" carry a text layer. `pdftotext`
runs first and OCR only touches pages that come back empty. Measured on this machine:
**3,700 pages/min** through the text-layer path against **45 pages/min** through OCR. On a
mixed corpus that is the difference between an afternoon and a week.

**Provenance or it did not happen.** Every chunk keeps its book, its page range and its
source file, because the Archivist has to cite a page a human can go and open. A chunk
without a page number is useless no matter how clean the text is.

**Bad text is worse than no text.** This ends up in a vault someone consults when they
cannot look anything up, and a page that reads "50 mg" where the paper said "5 mg" gets
quoted with total confidence. Every page carries a confidence score, anything under the
floor is quarantined rather than indexed, and the Archivist is expected to say *the scan
is poor here, read the original*.

## OCR engines

Interchangeable, tried in order, first one that actually answers wins:

| engine | notes |
|---|---|
| `tiiny` | GLM-OCR on a Tiiny Pocket. Checks the gateway really serves it, because firmware 0.1.29 reports the model running while the gateway answers "not loaded" |
| `tesseract` | `brew install tesseract`. Reports real per-word confidence |
| `paddle` | `pip install paddleocr`. Baidu's, strong on difficult scans |

Adding one is a `probe()` and an `ocr(png) -> (text, confidence)`.

## Across several machines

One machine holds the corpus and runs the hub; everything else asks it for work.

```bash
archiver hub                                        # on the box with the corpus
archiver work --hub http://hub-host:8430 --workers 16
```

Open the hub in a browser for a live dashboard: progress, fleet rate, ETA, and
per-machine pages, milliseconds and confidence.

**Not a shared SQLite over NFS.** Sharding with `--shard i/n` is correct between
processes on one machine, but SQLite's locking over a network filesystem is unreliable
and the failure mode is silent corruption of a corpus that took days to build. So the
database stays on one machine and the rest talk HTTP.

**Workers render and OCR locally.** Rendering is ~0.3s and OCR ~1.6s, so a hub that
rendered would cap the fleet at its own single-threaded render rate. The hub hands out
page numbers; each worker fetches the PDF once, caches it, and does the work.

**Leases, because machines die.** A claimed page not returned inside the lease goes back
in the pool. A worker losing power costs a few pages, not the run.

Measured: **480 pages/min** OCR on one M3 Ultra at 16 workers (near-linear from 37 at one
worker), and 96 pages of text-layer extraction across Tailscale at **5,024 pages/min**.

## Status

Working: add, text-layer triage, OCR through any driver, cleaning, chunking with
provenance, quarantine, search, export.

Not yet: ZIM extraction (needs libzim), parallel workers inside one machine, embedding.
Chunks come out as jsonl and something else embeds them.

MIT.
