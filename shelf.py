#!/usr/bin/env python3
"""Fetching the prebuilt shelf, for the LAST LIGHT app on the Tiiny App Farm.

The desk tool builds its own corpus out of PDFs. The farm app cannot: ingestion
needs poppler, poppler is reached by starting another program, and the farm's
archive scanner refuses a tree that can start another program. So the farm build
ships no ingestion at all and pulls a shelf that was already built.

That makes this module the whole install, which is why it is careful:

  * Resumable. The shelf is hundreds of megabytes over whatever connection the
    person has. A download that cannot resume is a download that never finishes
    on a bad line, so the partial file is kept and continued with a Range
    request. A server that ignores Range is not an error; it just starts over.
  * Verified twice, by length and then by hash, before anything is unpacked. A
    truncated tarball that happens to still open would put a half a shelf on the
    disk and nothing would ever say so.
  * Unpacked through a filter that refuses absolute paths and parent traversal.
    The archive is ours, but "the archive is ours" is not a security model, and
    Python only made that check the default in 3.12.

Nothing here starts a program. urllib, hashlib and tarfile are the whole toolkit.
"""
import hashlib
import json
import os
import pathlib
import tarfile
import urllib.error
import urllib.request

import archive

# ---------------------------------------------------------------- the shelf
# PLACEHOLDER. The corpus is not published yet: Jason decides what goes in the
# shelf from artifacts-src/lastlight-shelf-titles.md, and only then does it get
# a bucket, a URL and a hash. Until all three are real, download() refuses and
# says so rather than fetching something that is not there. Replace all three
# together, and keep the version in the filename so an old one stays fetchable.
SHELF_URL_PLACEHOLDER = "https://REPLACE-ME-WITH-R2-PUBLIC-URL.invalid/last-light/shelf-0.1.0.tar.gz"
SHELF_URL = os.environ.get("LAST_LIGHT_SHELF_URL", SHELF_URL_PLACEHOLDER)
SHELF_SHA256 = os.environ.get("LAST_LIGHT_SHELF_SHA256", "")
SHELF_BYTES = int(os.environ.get("LAST_LIGHT_SHELF_BYTES", "0") or 0)

CHUNK = 1 << 20

# A shelf built from Vikidia carries CC BY-SA 3.0 obligations: credit the authors
# and state the licence with a link, and for a complete copy those are mandatory
# rather than optional. So the bundle has to ship the notice, and this is where
# that is enforced instead of remembered: unpack refuses a bundle without it.
# The text below is what belongs in that file, for whoever builds the bundle.
ATTRIBUTION = "ATTRIBUTION.txt"
ATTRIBUTION_TEMPLATE = """This shelf contains text from Vikidia (https://en.vikidia.org),
used under the Creative Commons Attribution-ShareAlike 3.0 Unported licence,
https://creativecommons.org/licenses/by-sa/3.0/ , or the GNU Free Documentation
License, https://www.gnu.org/copyleft/fdl.html .

Authors are credited by the article history on Vikidia; each document records the
article it came from. Text reused from this shelf stays under the same licence.

Other documents on this shelf carry their own terms, listed in SOURCES.txt.
"""


def part_file():
    return archive.HOME / "shelf.tar.gz.part"


def configured():
    """Whether there is a real shelf to fetch yet.

    A placeholder URL, a missing hash or a zero length all mean the same thing:
    nobody has published a shelf, so there is nothing honest to download.
    """
    return bool(SHELF_URL
                and ".invalid" not in SHELF_URL
                and "REPLACE-ME" not in SHELF_URL
                and len(SHELF_SHA256) == 64
                and SHELF_BYTES > 0)


def installed():
    """Whether a corpus with documents in it is already on disk."""
    if not archive.DB.exists():
        return False
    try:
        c = archive.db()
        return c.execute("SELECT COUNT(*) n FROM doc").fetchone()["n"] > 0
    except Exception:
        return False


def state():
    """What the first-run screen needs to know, in one call."""
    have = part_file().stat().st_size if part_file().exists() else 0
    documents = 0
    if installed():
        documents = archive.db().execute(
            "SELECT COUNT(*) n FROM doc").fetchone()["n"]
    return {"installed": installed(),
            "documents": documents,
            "configured": configured(),
            "url": SHELF_URL if configured() else None,
            "bytes_expected": SHELF_BYTES,
            "bytes_have": have,
            "home": str(archive.HOME)}


def _open(url, offset):
    req = urllib.request.Request(url)
    if offset:
        req.add_header("Range", "bytes=%d-" % offset)
    return urllib.request.urlopen(req, timeout=60)


def fetch(on_progress=None):
    """Download the shelf to part_file(), resuming if there is something there.

    Returns the path. Raises RuntimeError with a sentence a person can act on.
    """
    if not configured():
        raise RuntimeError(
            "No shelf has been published yet. LAST_LIGHT_SHELF_URL, "
            "LAST_LIGHT_SHELF_SHA256 and LAST_LIGHT_SHELF_BYTES all have to be "
            "set, or the constants in shelf.py filled in, before there is "
            "anything to fetch.")
    archive.HOME.mkdir(parents=True, exist_ok=True)
    out = part_file()
    have = out.stat().st_size if out.exists() else 0
    if have > SHELF_BYTES:
        out.unlink()
        have = 0
    if have == SHELF_BYTES:
        return out

    try:
        resp = _open(SHELF_URL, have)
    except urllib.error.HTTPError as exc:
        if have and exc.code == 416:
            out.unlink()
            have = 0
            resp = _open(SHELF_URL, 0)
        else:
            raise RuntimeError("the shelf server answered HTTP %d" % exc.code)
    except Exception as exc:
        raise RuntimeError("could not reach the shelf: %s" % str(exc)[:160])

    # A server that ignores Range sends 200 and the whole file. Starting over is
    # correct then; appending would corrupt what is already on disk.
    mode = "ab" if (have and resp.status == 206) else "wb"
    if mode == "wb":
        have = 0
    with resp, open(out, mode) as fh:
        while True:
            block = resp.read(CHUNK)
            if not block:
                break
            fh.write(block)
            have += len(block)
            if on_progress:
                on_progress(have, SHELF_BYTES)
    return out


def verify(path):
    """Length then hash. Both, in that order, before anything is unpacked."""
    size = path.stat().st_size
    if size != SHELF_BYTES:
        raise RuntimeError(
            "the shelf is %d bytes and should be %d; the download is short or "
            "the published size is wrong" % (size, SHELF_BYTES))
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    got = h.hexdigest()
    if got != SHELF_SHA256:
        raise RuntimeError(
            "the shelf hashes to %s and should be %s; it is not the file that "
            "was published" % (got[:16], SHELF_SHA256[:16]))
    return got


def _safe(members, root):
    """Refuse anything that would land outside the corpus directory."""
    root = root.resolve()
    for m in members:
        target = (root / m.name).resolve()
        if not str(target).startswith(str(root) + os.sep):
            raise RuntimeError("the shelf contains a path outside the corpus: %s"
                               % m.name)
        if m.issym() or m.islnk():
            raise RuntimeError("the shelf contains a link: %s" % m.name)
        yield m


def unpack(path):
    """Extract the verified tarball into the corpus directory.

    Refuses a bundle with no attribution file. A share-alike obligation that
    depends on somebody remembering to add a file is an obligation that gets
    missed, so it is checked here, where a missing file is still fixable.
    """
    archive.HOME.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "r:gz") as tf:
        names = tf.getnames()
        if not any(pathlib.PurePath(n).name == ATTRIBUTION for n in names):
            raise RuntimeError(
                "this shelf carries no %s. The Vikidia text in it is CC BY-SA, "
                "which requires the authors to be credited and the licence "
                "named, so the bundle is not complete without it." % ATTRIBUTION)
        tf.extractall(archive.HOME, members=_safe(tf, archive.HOME))
    return archive.HOME


def install(on_progress=None):
    """Fetch, verify, unpack, and drop the part file. The whole install."""
    path = fetch(on_progress)
    verify(path)
    unpack(path)
    path.unlink(missing_ok=True)
    return state()


if __name__ == "__main__":
    print(json.dumps(state(), indent=2))
