#!/usr/bin/env python3
"""
notes: a personal markdown notebook as an MCP server.

Notes are plain .md files in NOTES_DIR, so you can also open and edit them
with any editor. search_notes uses simple relevance scoring (no embeddings),
which works offline. A good upgrade later: embeddings for semantic search.
NEVER print() to stdout (stdio transport). Log to stderr.
"""
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("notes")

NOTES_DIR = Path(os.environ.get("NOTES_DIR", Path.home() / ".agent_platform" / "notes")).expanduser().resolve()
NOTES_DIR.mkdir(parents=True, exist_ok=True)
MAX_READ = 4000

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
ADD = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

mcp = FastMCP("notes")
STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "it", "my", "me", "what",
        "about", "with", "that", "this", "was", "are", "do", "did", "i"}


def slugify(text: str) -> str:
    slug = re.sub(r"[^\w]+", "-", text.strip().lower()).strip("-_")[:60]
    return slug or "untitled"


def note_path(name: str) -> Path:
    """Names are slugified, so '../x' can never leave NOTES_DIR."""
    slug = slugify(name.removesuffix(".md"))
    p = (NOTES_DIR / f"{slug}.md").resolve()
    if p.parent != NOTES_DIR:
        raise ValueError("Invalid note name")
    return p


def existing_note(name: str) -> Path:
    p = note_path(name)
    if not p.exists():
        raise ValueError(f"No note named {name!r}. Use list_notes or search_notes to find the right name.")
    return p


def title_of(p: Path, text: str | None = None) -> str:
    first = (text if text is not None else p.read_text(encoding="utf-8")).split("\n", 1)[0]
    return first.lstrip("# ").strip() or p.stem


def tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"\w+", text.lower()) if len(w) > 1 and w not in STOP]


@mcp.tool(annotations=ADD)
def add_note(title: str, content: str, tags: str = "") -> dict:
    """Create a new note. If a note with the same title exists, a number is added to the name.
    tags is an optional comma-separated string."""
    if not title.strip() or not content.strip():
        raise ValueError("title and content must not be empty")
    p, i = note_path(title), 2
    base = p.stem
    while p.exists():
        p = note_path(f"{base}-{i}")
        i += 1
    tag_line = f"tags: {', '.join(t.strip() for t in tags.split(',') if t.strip())}\n" if tags.strip() else ""
    p.write_text(f"# {title.strip()}\n\n{tag_line}created: {datetime.now():%Y-%m-%d %H:%M}\n\n{content.strip()}\n",
                 encoding="utf-8")
    return {"created": p.stem, "hint": "Use this exact name with read_note or append_to_note."}


@mcp.tool(annotations=ADD)
def append_to_note(name: str, text: str) -> dict:
    """Add text to the end of an existing note (does not overwrite anything)."""
    if not text.strip():
        raise ValueError("text must not be empty")
    p = existing_note(name)
    with p.open("a", encoding="utf-8") as f:
        f.write(f"\n{text.strip()}\n")
    return {"appended_to": p.stem}


@mcp.tool(annotations=READ)
def read_note(name: str) -> dict:
    """Read a note's full text (long notes are cut off at about 4000 characters)."""
    p = existing_note(name)
    text = p.read_text(encoding="utf-8")
    return {"name": p.stem, "truncated": len(text) > MAX_READ, "content": text[:MAX_READ]}


@mcp.tool(annotations=READ)
def list_notes(limit: int = 30) -> dict:
    """List notes, most recently changed first."""
    files = sorted(NOTES_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {"total": len(files),
            "notes": [f"{p.stem}  -  {title_of(p)}" for p in files[:max(1, min(limit, 100))]]}


@mcp.tool(annotations=READ)
def search_notes(query: str, limit: int = 5) -> dict:
    """Search all notes by keywords. Matches in the title and tags count extra.
    Returns the best notes with a short snippet; use read_note for the full text."""
    terms = set(tokens(query))
    if not terms:
        raise ValueError("query has no searchable words")
    scored = []
    for p in NOTES_DIR.glob("*.md"):
        text = p.read_text(encoding="utf-8")
        title = title_of(p, text)
        words = tokens(text)
        title_words = set(tokens(title))
        score = sum(words.count(t) for t in terms) + 5 * len(terms & title_words)
        if p.stem in query.lower().replace(" ", "-"):
            score += 5
        if score:
            snippet = next((ln.strip() for ln in text.splitlines() if terms & set(tokens(ln)) and ln.strip()), "")
            scored.append((score, p.stem, title, snippet[:160]))
    scored.sort(reverse=True)
    return {"matches": len(scored),
            "results": [{"name": n, "title": t, "score": s, "snippet": sn}
                        for s, n, t, sn in scored[:max(1, min(limit, 20))]]}


@mcp.tool(annotations=DELETE)
def delete_note(name: str) -> dict:
    """Permanently delete a note. Cannot be undone; only use when the user clearly asked to delete it."""
    p = existing_note(name)
    p.unlink()
    return {"deleted": p.stem}


if __name__ == "__main__":
    log.info("notes server starting, dir=%s", NOTES_DIR)
    mcp.run(transport="stdio")
