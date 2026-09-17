#!/usr/bin/env python3
"""Build the release tarball the Tiiny App Farm installs.

    python3 scripts/build-release.py 0.1.3

0.1.0 and 0.1.1 were packed by hand and the file list lived in somebody's shell
history, which is not a thing a second person can repeat. It is written down
here now, and this is the only place it is written down.

What ships is less than the repo. The farm's archive scanner refuses a tree that
can start other programs, so the two OCR modules that shell out to tesseract and
poppler stay behind; so do the changelog, the contributor notes and the banner,
which are things you read on GitHub rather than things the app runs.

Python's tarfile is used rather than the tar command on purpose. On macOS, tar
writes an AppleDouble ._ sidecar for every file carrying an extended attribute
unless COPYFILE_DISABLE is set, and remembering an environment variable is not a
guarantee. tarfile cannot write one at all.
"""
import argparse
import hashlib
import pathlib
import sys
import tarfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Everything the app needs at runtime, and nothing that can run another program.
SHIPPED = (
    "LICENSE",
    "README.md",
    "archiver",
    "archive.py",
    "archivist.py",
    "cockpit.py",
    "entities.py",
    "hub.py",
    "ingest.py",
    "md2jsonl.py",
    "overview.py",
    "static/cockpit.html",
)

# Deliberately absent, so a later reader knows it was a decision and not a slip.
WITHHELD = {
    "drivers.py": "shells out to tesseract; the scanner refuses subprocess",
    "pdfsource.py": "shells out to poppler; same reason",
    "corpus/": "the person's own archive, written at first run",
    "CHANGELOG.md": "read on GitHub, not by the app",
    "CONTRIBUTING.md": "read on GitHub, not by the app",
    "FOLLOWUPS.md": "read on GitHub, not by the app",
    "INSTALL.md": "read on GitHub, not by the app",
    "assets/": "the banner is for the README",
    "tiiny-app.json": "the farm keeps its own manifest",
}


def build(version, out_dir=None):
    """Write tiiny-brain-<version>.tar.gz and return (path, sha256, size)."""
    out_dir = pathlib.Path(out_dir or ROOT / "dist")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"tiiny-brain-{version}"
    out = out_dir / f"{stem}.tar.gz"
    if out.exists():
        out.unlink()

    def scrub(info):
        # Nobody's username belongs in a public tarball, and the farm reads
        # neither, so the same tree packs to the same bytes on any machine.
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mode = 0o755 if info.mode & 0o111 else 0o644
        return info

    with tarfile.open(out, "w:gz") as tar:
        for name in SHIPPED:
            source = ROOT / name
            if not source.is_file():
                raise SystemExit(f"  missing from the tree: {name}")
            tar.add(source, arcname=f"{stem}/{name}", filter=scrub)

    blob = out.read_bytes()
    return out, hashlib.sha256(blob).hexdigest(), len(blob)


def main():
    parser = argparse.ArgumentParser(description="Build the farm release tarball.")
    parser.add_argument("version", help="for example 0.1.3")
    parser.add_argument("--out", default=None, help="where to write it")
    args = parser.parse_args()
    out, digest, size = build(args.version, args.out)
    print(f"  {out}")
    print(f"  sha256  {digest}")
    print(f"  size    {size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
