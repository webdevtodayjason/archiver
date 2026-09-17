#!/usr/bin/env python3
"""Turn a folder of markdown into a JSONL shelf the archiver can ingest.

Same output shape as unzim.py --html, so `archive.py add-text` reads it without
knowing where it came from: one object per line, {title, path, text}.

Written for an Obsidian vault, where the folder a note sits in is most of what
tells you what it is about, and the filename is how its author refers to it. So
the folder becomes the source and the filename becomes the title, which makes a
citation read "Pricing Discussion - Titanium" rather than a path nobody says
out loud.
"""
import json
import pathlib
import re
import sys

SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", ".smart-env",
             ".space", "__pycache__"}
FRONTMATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)


def clean(text):
    """Body text, without the parts that are addressing the editor."""
    text = FRONTMATTER.sub("", text)
    # Obsidian embeds and wiki links: keep the words, drop the plumbing.
    text = re.sub(r"!\[\[([^\]|]+)(\|[^\]]*)?\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)  # code fences
    # Inline code too: `DocumentEvent` and `GeneratedDocument` are identifiers,
    # and left in they read as capitalised names and become entities.
    text = re.sub(r"`[^`\n]{1,80}`", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def shelf_of(rel):
    """Top folder, or "(root)" for loose notes at the top level.

    Takes a relative PurePath so the same note gives the same project name
    whichever machine walked the folder. A Windows walk hands back
    ``Projects\\note.md`` and a Mac walk ``Projects/note.md``; both are
    ``Projects``.
    """
    return rel.parts[0] if len(rel.parts) > 1 else "(root)"


def records(root, min_chars=200):
    """Walk a folder of markdown and yield one record per note.

    The one place the walk lives. md2jsonl writes these to a JSONL file for the
    command line; the cockpit's loader reads the same generator straight into
    the archive, so the button and the command see the same notes, the same
    projects and the same titles.

    Yields (record, rel) where rel is the note's path under root. A record that
    is too short to be worth embedding is counted rather than yielded, and comes
    back as the second half of the ``too_short`` pair.
    """
    root = pathlib.Path(root).expanduser()
    for md in sorted(root.rglob("*.md")):
        if any(part in SKIP_DIRS for part in md.parts):
            continue
        try:
            body = clean(md.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        rel = md.relative_to(root)
        if len(body) < min_chars:
            yield None, rel
            continue
        yield {
            "title": md.stem,
            # as_posix, so the key a note is filed under does not depend on
            # which operating system read the folder.
            "path": rel.as_posix(),
            "shelf": shelf_of(rel),
            "text": body,
        }, rel


def convert(root, out_path, min_chars=200):
    out = pathlib.Path(out_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    kept = short = 0
    with out.open("w", encoding="utf-8") as fh:
        for rec, _rel in records(root, min_chars):
            if rec is None:
                short += 1
                continue
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            kept += 1
    return kept, short, out


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: md2jsonl.py <vault-dir> <out.jsonl>")
    n, s, where = convert(sys.argv[1], sys.argv[2])
    print(f"  {n:,} notes  ({s:,} too short)  ->  {where}")
