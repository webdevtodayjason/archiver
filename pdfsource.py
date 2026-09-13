#!/usr/bin/env python3
"""Everything that shells out to poppler.

It is one import away from the rest of the tool on purpose. Reading a PDF means
running pdfinfo, pdftotext and pdftoppm, and the notes half of this tool never
touches a PDF at all. Keeping the four calls in their own module means the notes
half can be packaged for a sandbox that forbids subprocess without pretending
the PDF half does not exist: you ship everything except this file, and
`archiver add` says so in a sentence instead of dying in a traceback.

poppler does the work because it is the one thing that reliably reads a PDF the
way the person who made it meant it. Nothing here parses PDF structure itself.
"""
import os
import re
import subprocess
import time

TIMEOUT_INFO = 120
TIMEOUT_TEXT = 120
TIMEOUT_RENDER = 300


def page_count(pdf):
    """How many pages, or 0 if poppler cannot make sense of the file."""
    try:
        out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True,
                             text=True, errors="replace", timeout=TIMEOUT_INFO).stdout
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        return int(m.group(1)) if m else 0
    except Exception:  # noqa: BLE001
        return 0


def pdf_title(path):
    """Whatever the PDF claims its title is, junk included.

    Junk included because the caller decides. A title that is really a filename
    is worthless as a title and still useful as a hint to a model being asked to
    guess a better one, so the filtering belongs where the intent is, not here.
    """
    if not os.path.exists(path):
        return None
    try:
        out = subprocess.run(["pdfinfo", path], capture_output=True, text=True,
                             errors="replace", timeout=30).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        k, _, v = line.partition(":")
        if k.strip() == "Title":
            return " ".join(v.split())[:120] or None
    return None


def text_layer(pdf, page_no):
    """What poppler can already read off the page, if anything."""
    try:
        out = subprocess.run(
            ["pdftotext", "-f", str(page_no), "-l", str(page_no), "-layout",
             str(pdf), "-"], capture_output=True, text=True, errors="replace",
            timeout=TIMEOUT_TEXT)
        return (out.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def render(pdf, page_no, dest, tries=2):
    """One page to PNG at 300dpi, which is what OCR engines want.

    Retried once and the reason kept. Under heavy concurrency pdftoppm
    occasionally comes back with nothing, and an early version recorded that as
    a bare 'failed' with no explanation - which on a million-page run is the
    difference between a fixable problem and a mystery."""
    stem = str(dest.with_suffix(""))
    why = ""
    for attempt in range(tries):
        try:
            r = subprocess.run(
                ["pdftoppm", "-f", str(page_no), "-l", str(page_no),
                 "-r", "300", "-png", "-singlefile", str(pdf), stem],
                capture_output=True, timeout=TIMEOUT_RENDER)
            if dest.exists() and dest.stat().st_size > 0:
                return dest, ""
            why = (r.stderr or b"").decode("utf-8", "replace").strip()[:70] \
                or f"pdftoppm produced nothing (rc {r.returncode})"
        except Exception as exc:  # noqa: BLE001
            why = str(exc)[:70]
        if attempt + 1 < tries:
            time.sleep(0.4)
    return None, why or "render failed"
