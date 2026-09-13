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


def convert(root, out_path, min_chars=200):
    root = pathlib.Path(root).expanduser()
    out = pathlib.Path(out_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    kept = short = 0
    with out.open("w", encoding="utf-8") as fh:
        for md in sorted(root.rglob("*.md")):
            if any(part in SKIP_DIRS for part in md.parts):
                continue
            try:
                body = clean(md.read_text(encoding="utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                continue
            if len(body) < min_chars:
                short += 1
                continue
            rel = md.relative_to(root)
            # Top folder, or "(root)" for loose notes at the top level.
            shelf = rel.parts[0] if len(rel.parts) > 1 else "(root)"
            fh.write(json.dumps({
                "title": md.stem,
                "path": str(rel),
                "shelf": shelf,
                "text": body,
            }, ensure_ascii=False) + "\n")
            kept += 1
    return kept, short, out


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: md2jsonl.py <vault-dir> <out.jsonl>")
    n, s, where = convert(sys.argv[1], sys.argv[2])
    print(f"  {n:,} notes  ({s:,} too short)  ->  {where}")
