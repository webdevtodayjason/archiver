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

No coordinator, no queue server. Each machine takes a slice by page id:

```bash
archiver run --shard 1/3     # on the first box
archiver run --shard 2/3     # on the second
archiver run --shard 3/3     # on the third
```

They share one SQLite corpus over the network; WAL and a busy timeout are the whole
coordination story. Every page is committed as it finishes, so an interrupted run loses
at most the page in flight.

## Status

Working: add, text-layer triage, OCR through any driver, cleaning, chunking with
provenance, quarantine, search, export.

Not yet: ZIM extraction (needs libzim), parallel workers inside one machine, embedding.
Chunks come out as jsonl and something else embeds them.

MIT.
