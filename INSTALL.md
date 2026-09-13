# Install

There are two ways to use this and they need different things. Read the one you want.

- **A shelf of PDFs into citable text.** Needs poppler and an OCR engine. Start at
  [Scanning documents](#scanning-documents).
- **Your own notes, as a second brain you can ask.** Needs neither. Start at
  [Your own notes](#your-own-notes).

Both need Python 3.9 or newer and nothing from PyPI. The whole thing is standard
library. That is deliberate: a tool you reach for when you cannot look anything up
should not need a package index to start.

If numpy happens to be installed it will be used for the similarity search, which is
worth having once a corpus gets past a few thousand chunks. It is not required and
nothing asks you to install it.

```sh
git clone https://github.com/webdevtodayjason/archiver
cd archiver
python3 archiver status
```

If that prints a corpus summary you are installed. There is no build step.

## Where things get written

Everything lands in one folder, and you choose it:

```sh
export ARCHIVER_HOME=~/brain/corpus      # default: ./corpus next to the code
```

Set it in your shell profile. Every command below reads it, and pointing two
machines at two different folders is how you keep two corpora apart.

## Connecting a Tiiny

Embedding and answering happen on the device. Three variables:

```sh
export TIINY_HOST=tiiny                  # hostname or IP
export TIINY_KEY=...                     # from the Tiiny app
export TIINY_PORT=80                     # the default, and correct for firmware 1.0.0
```

**Port 80 is not a typo.** Firmware 1.0.0 moved the gateway there. Before that it was
8800, and on 1.0.0 that port still answers but only on the docker bridge, so a TCP check
passes while every request fails. If you are on older firmware, set `TIINY_PORT=8800`.

To check it without running anything else:

```sh
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TIINY_KEY" \
  http://$TIINY_HOST:$TIINY_PORT/v1/models
```

`200` is good. `401` means the device is there and the key is wrong. `404` means
something is answering on that port but it is not the gateway. Anything else means you
have not reached the device at all.

If you want the answering model somewhere other than the Tiiny, point it there and keep
embeddings on the device:

```sh
export LASTLIGHT_CHAT_URL=http://some-host:8104
export LASTLIGHT_CHAT_KEY=...            # defaults to TIINY_KEY
```

You can set all of this in the Setup panel instead, once the cockpit is running. It
writes `~/.config/tiiny-brain.json` at mode 600. Environment variables always win over
that file, so a script you wrote last month keeps working.

## Your own notes

No poppler, no OCR, nothing to install.

```sh
python3 md2jsonl.py ~/Obsidian\ Vault notes.jsonl
python3 archiver add-text notes.jsonl
python3 archiver chunk
python3 archivist.py index
python3 entities.py build
python3 cockpit.py 8500
```

Then open <http://127.0.0.1:8500>.

What each step is for:

| step | what it does |
|---|---|
| `md2jsonl.py` | Reads a folder of markdown. Folder becomes the project, filename becomes the title. Strips frontmatter, wikilinks and code fences. |
| `add-text` | Files the notes in the corpus. Re-running only adds what is new. |
| `chunk` | Cuts them into passages that keep their source and page. |
| `archivist.py index` | Embeds the chunks on the device. This is the slow one. |
| `entities.py build` | Finds the recurring names. No model involved, about a second for ten thousand chunks. |
| `cockpit.py` | The map, the reader and the ask box. |

Write a note from inside the cockpit and it goes into your vault as markdown, gets
chunked and embedded, and is answerable a few seconds later. You do not re-run any of
the above.

**Re-running after you add notes.** `add-text`, `chunk`, `index` and `entities.py build`
again. The first three only do what is new. `entities.py build` rebuilds the whole name
index every time, which is fine because it is fast.

## Scanning documents

This path shells out to poppler, so it needs it:

```sh
brew install poppler                     # macOS
sudo apt install poppler-utils           # Debian, Ubuntu
```

Then pick an OCR engine. Ask the tool which ones it can actually use:

```sh
python3 archiver engines
```

| engine | how to get it |
|---|---|
| `tiiny` | Load `zai-org/GLM-OCR` on the device. Nothing to install here. |
| `tesseract` | `brew install tesseract` or `apt install tesseract-ocr` |
| `paddle` | `pip install paddleocr` |

You do not need all three, or any of them, if your PDFs already carry a text layer.
Most do, and the tool checks before it reaches for OCR.

```sh
python3 archiver add ~/shelf --source "Survivor Library"
python3 archiver run                     # safe to stop and restart
python3 archiver status
python3 archiver chunk
python3 archivist.py index
python3 archivist.py ask "how do you tan a hide"
```

`run` is resumable. Stop it with Ctrl-C, start it again, and it picks up the pages it
has not done.

### Several machines

One machine holds the corpus and hands out work:

```sh
python3 archiver hub                                       # on the box with the corpus
python3 archiver work --hub http://hub-host:8430 --workers 16
```

Open the hub in a browser for progress, fleet rate and per-machine confidence. The
database stays on one machine on purpose, because SQLite over a network filesystem
fails silently and takes the corpus with it.

## Checking it works

```sh
python3 archiver status                  # notes, chunks, quarantine
python3 archivist.py refusal-test        # does it refuse what it cannot support
python3 entities.py top 25               # the names it found
```

Or open the cockpit and click the light in the top bar. It probes the device, the key,
the embedding model, the answering model, whether every chunk is embedded and whether
names have been extracted, and names the one that is broken.

## When it does not work

**`Set TIINY_HOST and TIINY_KEY.`** Exactly what it says. They are not set in the shell
you are in.

**Nothing I retrieved covers that.** Working as intended, most of the time. It means the
passages that came back do not support an answer, and the tool will not invent one.
Check that chunks and vectors match in Setup. If vectors are short, run
`python3 archivist.py index`.

**Empty graph in the cockpit.** Names have not been extracted yet. `python3 entities.py
build`.

**A request that takes forever and then fails.** The device caps a single request at
around 220 seconds. Long answers have to be asked for in smaller pieces.

**Homebrew Python cannot reach the device.** On some setups the Homebrew build resolves
the Tiiny's hostname differently from the system one. Try `/usr/bin/python3` before you
debug anything else.
