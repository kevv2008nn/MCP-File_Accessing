#!/usr/bin/env python3
"""
file-organizer v2: a safe, local file organizer exposed as an MCP server.
Tuned for LOCAL models (LM Studio) on Windows.

Safety design:
  1. SANDBOX     - every path must stay inside ORGANIZER_ROOT.
  2. PLAN FIRST  - plan_* tools never touch files.
  3. DRY RUN     - preview_plan shows what would happen AND issues a confirm code.
  4. CONFIRM     - apply_plan only runs with that exact code (so the preview can't be skipped).
  5. NO DELETE   - duplicates go to a quarantine folder, never deleted.
  6. UNDO        - applied plans are journaled to disk and reversible.

NEVER print() to stdout: stdio transport uses stdout for JSON-RPC. Log to stderr.
"""
import hashlib
import json
import logging
import os
import random
import re
import shutil
import sys
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("file-organizer")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
ROOT = Path(os.environ.get("ORGANIZER_ROOT",
                           Path.home() / "organizer_sandbox")).expanduser().resolve()
ROOT.mkdir(parents=True, exist_ok=True)
STATE_DIR = ROOT / ".organizer"
JOURNAL = STATE_DIR / "journal.json"
QUARANTINE = "_duplicates"

# Small local models choke on huge outputs, so everything is capped.
MAX_LIST = 30
MAX_PREVIEW = 25
MAX_GROUPS = 10

CATEGORIES = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".heic"},
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt"},
    "Spreadsheets": {".xls", ".xlsx", ".csv", ".ods"},
    "Presentations": {".ppt", ".pptx", ".key", ".odp"},
    "Audio": {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg"},
    "Video": {".mp4", ".mkv", ".mov", ".avi", ".webm"},
    "Archives": {".zip", ".tar", ".gz", ".rar", ".7z", ".bz2"},
    "Code": {".py", ".js", ".ts", ".java", ".c", ".cpp", ".go", ".rs", ".html", ".css", ".json"},
}

mcp = FastMCP("file-organizer")

# Tool annotations are HINTS the host uses to decide how much approval to ask for.
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)                      # never changes anything
PLAN = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)  # changes no files
MOVES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)  # moves/renames real files
PLANS: dict[str, dict] = {}   # in-memory; the journal of APPLIED plans is on disk


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def safe_path(rel_path: str) -> Path:
    """Resolve inside ROOT or raise. Blocks '..' and symlink escapes."""
    p = (ROOT / rel_path).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise ValueError(f"Path escapes the allowed root: {rel_path!r}")
    return p


def rel(p: Path) -> str:
    """Root-relative path with forward slashes (same on Windows and Linux)."""
    return p.relative_to(ROOT).as_posix()


def iter_files(base: Path, recursive: bool = False, include_quarantine: bool = False):
    it = base.rglob("*") if recursive else base.iterdir()
    for p in it:
        parts = p.relative_to(ROOT).parts
        if any(part.startswith(".") for part in parts):
            continue
        if not include_quarantine and QUARANTINE in parts:
            continue
        if p.is_file() and not p.is_symlink():
            yield p


def category_of(p: Path) -> str:
    ext = p.suffix.lower()
    for name, exts in CATEGORIES.items():
        if ext in exts:
            return name
    return "Other"


def sha256(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def is_occupied(dst: Path, src: Path | None = None) -> bool:
    """True if dst exists AND is a different file than src.
    Needed on Windows: 'Photo.JPG' -> 'photo.jpg' is the SAME file (case-insensitive FS)."""
    if not dst.exists():
        return False
    if src is not None and src.exists():
        try:
            return not os.path.samefile(src, dst)
        except OSError:
            return True
    return True


def unique_dest(dst: Path, taken: set, src: Path | None = None) -> Path:
    """Avoid overwriting: add _1, _2... if dst exists or is already planned."""
    cand, i = dst, 1
    while is_occupied(cand, src) or str(cand).lower() in taken:
        cand = dst.with_name(f"{dst.stem}_{i}{dst.suffix}")
        i += 1
    taken.add(str(cand).lower())
    return cand


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def new_plan(kind: str, ops: list[dict], base: Path) -> dict:
    if not ops:
        return {"plan_id": None, "operations": 0, "message": "Nothing to do; no plan created."}
    pid = uuid.uuid4().hex[:6]
    PLANS[pid] = {"id": pid, "kind": kind, "created": datetime.now().isoformat(timespec="seconds"),
                  "status": "pending", "confirm_code": None, "ops": ops}
    log.info("created plan %s (%s, %d ops)", pid, kind, len(ops))
    return {"plan_id": pid, "kind": kind, "operations": len(ops),
            "next_step": f"Call preview_plan with plan_id='{pid}' and show the result to the user."}


def load_journal() -> list:
    return json.loads(JOURNAL.read_text()) if JOURNAL.exists() else []


def save_journal(entries: list) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    JOURNAL.write_text(json.dumps(entries, indent=2))


def get_plan(plan_id: str) -> dict:
    plan = PLANS.get(plan_id)
    if not plan:
        raise ValueError(f"No such plan '{plan_id}'. Call list_plans to see valid ids.")
    return plan


# --------------------------------------------------------------------------
# TOOLS: info + read-only inspection
# --------------------------------------------------------------------------
@mcp.tool(annotations=READ)
def organizer_info() -> dict:
    """START HERE. Explains the workflow and shows the root folder being managed."""
    return {"root_folder": str(ROOT),
            "paths": "All paths are relative to the root folder. Use '.' for the root itself.",
            "workflow": [
                "1. Inspect: summarize_folder, list_files, find_duplicates, find_large_files.",
                "2. Plan: plan_sort_by_type, plan_sort_by_date, plan_rename or plan_dedupe. Plans change nothing.",
                "3. Preview: preview_plan returns what will happen plus a confirm_code.",
                "4. Ask the user for approval, showing the preview.",
                "5. Only after the user says yes: apply_plan with plan_id and confirm_code.",
                "6. undo_plan reverses the last applied plan."],
            "rules": ["Never apply a plan without showing the preview to the user first.",
                      "Never invent a confirm_code. It only comes from preview_plan.",
                      "Files are never deleted; duplicates are moved to a _duplicates folder."]}


@mcp.tool(annotations=READ)
def list_files(subdir: str = ".", recursive: bool = False) -> dict:
    """List files in a folder (read-only). subdir is relative to the root, e.g. 'messy'."""
    base = safe_path(subdir)
    files = sorted(iter_files(base, recursive))
    return {"total": len(files), "showing": min(MAX_LIST, len(files)),
            "files": [f"{rel(p)} ({human(p.stat().st_size)})" for p in files[:MAX_LIST]]}


@mcp.tool(annotations=READ)
def summarize_folder(subdir: str = ".", recursive: bool = True) -> dict:
    """Summarize a folder: file count and size per category (read-only)."""
    base = safe_path(subdir)
    by_cat = defaultdict(lambda: [0, 0])
    total = 0
    for p in iter_files(base, recursive):
        size = p.stat().st_size
        by_cat[category_of(p)][0] += 1
        by_cat[category_of(p)][1] += size
        total += size
    return {"total_files": sum(v[0] for v in by_cat.values()), "total_size": human(total),
            "by_category": {k: f"{v[0]} files, {human(v[1])}" for k, v in sorted(by_cat.items())}}


@mcp.tool(annotations=READ)
def find_large_files(subdir: str = ".", min_mb: float = 10.0) -> dict:
    """Find files bigger than min_mb megabytes, largest first (read-only)."""
    base = safe_path(subdir)
    limit = int(min_mb * 1024 * 1024)
    big = sorted((p for p in iter_files(base, True) if p.stat().st_size >= limit),
                 key=lambda p: p.stat().st_size, reverse=True)
    return {"found": len(big),
            "files": [f"{rel(p)} ({human(p.stat().st_size)})" for p in big[:MAX_LIST]]}


@mcp.tool(annotations=READ)
def find_duplicates(subdir: str = ".") -> dict:
    """Find files with identical content, using size then SHA-256 (read-only)."""
    base = safe_path(subdir)
    by_size = defaultdict(list)
    for p in iter_files(base, True):
        by_size[p.stat().st_size].append(p)
    groups, wasted = [], 0
    for size, paths in by_size.items():
        if len(paths) < 2 or size == 0:
            continue
        by_hash = defaultdict(list)
        for p in paths:
            by_hash[sha256(p)].append(p)
        for same in by_hash.values():
            if len(same) > 1:
                wasted += size * (len(same) - 1)
                groups.append({"size": human(size), "files": sorted(rel(p) for p in same)})
    return {"duplicate_groups": len(groups), "wasted_space": human(wasted),
            "groups": groups[:MAX_GROUPS],
            "note": "Use plan_dedupe to quarantine the extras." if groups else "No duplicates."}


# --------------------------------------------------------------------------
# TOOLS: planning (NEVER modify files)
# --------------------------------------------------------------------------
@mcp.tool(annotations=PLAN)
def plan_sort_by_type(subdir: str = ".", recursive: bool = False) -> dict:
    """Plan moving files into folders like Images, Documents, Audio. Changes nothing yet."""
    base = safe_path(subdir)
    taken, ops = set(), []
    for p in sorted(iter_files(base, recursive)):
        target_dir = base / category_of(p)
        if p.parent == target_dir:
            continue
        ops.append({"src": rel(p), "dst": rel(unique_dest(target_dir / p.name, taken, p))})
    return new_plan("sort_by_type", ops, base)


@mcp.tool(annotations=PLAN)
def plan_sort_by_date(subdir: str = ".", date_format: str = "%Y-%m", recursive: bool = False) -> dict:
    """Plan moving files into folders named by modified date, e.g. 2026-10. Changes nothing yet.
    date_format is a strftime pattern: '%Y-%m' gives 2026-10, '%Y' gives 2026."""
    base = safe_path(subdir)
    taken, ops = set(), []
    for p in sorted(iter_files(base, recursive)):
        target_dir = base / datetime.fromtimestamp(p.stat().st_mtime).strftime(date_format)
        if p.parent == target_dir:
            continue
        ops.append({"src": rel(p), "dst": rel(unique_dest(target_dir / p.name, taken, p))})
    return new_plan("sort_by_date", ops, base)


@mcp.tool(annotations=PLAN)
def plan_rename(subdir: str = ".",
                mode: Literal["snake_case", "kebab_case", "lowercase", "date_prefix", "replace"] = "snake_case", find: str = "",
                replace: str = "", recursive: bool = False) -> dict:
    """Plan renaming files. Changes nothing yet.
    mode is one of: snake_case, kebab_case, lowercase, date_prefix, replace.
    date_prefix adds the modified date (YYYY-MM-DD_) to the name.
    replace swaps the text in `find` with `replace` inside each filename."""
    if mode not in {"snake_case", "kebab_case", "lowercase", "date_prefix", "replace"}:
        raise ValueError("mode must be snake_case, kebab_case, lowercase, date_prefix or replace")
    if mode == "replace" and not find:
        raise ValueError("mode='replace' needs a non-empty `find` value")
    base = safe_path(subdir)
    taken, ops = set(), []
    for p in sorted(iter_files(base, recursive)):
        stem, ext = p.stem, p.suffix.lower()
        if mode == "snake_case":
            new_stem = re.sub(r"[^\w]+", "_", stem.strip()).strip("_").lower()
        elif mode == "kebab_case":
            new_stem = re.sub(r"[^\w]+", "-", stem.strip()).strip("-").lower()
        elif mode == "lowercase":
            new_stem = stem.lower()
        elif mode == "date_prefix":
            d = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
            new_stem = stem if stem.startswith(d) else f"{d}_{stem}"
        else:
            new_stem = stem.replace(find, replace)
        new_name = (new_stem or "unnamed") + ext
        if new_name == p.name:
            continue
        ops.append({"src": rel(p), "dst": rel(unique_dest(p.with_name(new_name), taken, p))})
    return new_plan(f"rename:{mode}", ops, base)


@mcp.tool(annotations=PLAN)
def plan_dedupe(subdir: str = ".") -> dict:
    """Plan moving duplicate files into a _duplicates folder. Never deletes. Changes nothing yet.
    In each group of identical files, the oldest one stays where it is."""
    base = safe_path(subdir)
    by_size = defaultdict(list)
    for p in iter_files(base, True):
        by_size[p.stat().st_size].append(p)
    taken, ops = set(), []
    qdir = base / QUARANTINE
    for size, paths in by_size.items():
        if len(paths) < 2 or size == 0:
            continue
        by_hash = defaultdict(list)
        for p in paths:
            by_hash[sha256(p)].append(p)
        for same in by_hash.values():
            if len(same) < 2:
                continue
            same.sort(key=lambda p: (p.stat().st_mtime, len(str(p))))
            for dup in same[1:]:
                ops.append({"src": rel(dup), "dst": rel(unique_dest(qdir / dup.name, taken, dup)),
                            "note": f"duplicate of {rel(same[0])}"})
    return new_plan("dedupe", ops, base)


# --------------------------------------------------------------------------
# TOOLS: review, apply, undo
# --------------------------------------------------------------------------
@mcp.tool(annotations=READ)
def list_plans() -> dict:
    """List plans created in this session and their status."""
    return {"plans": [f"{p['id']}: {p['kind']}, {len(p['ops'])} ops, {p['status']}"
                      for p in PLANS.values()] or ["No plans yet."]}


@mcp.tool(annotations=READ)
def preview_plan(plan_id: str) -> dict:
    """DRY RUN. Shows what apply_plan would do and returns the confirm_code needed to apply.
    Show this preview to the user and get their approval before applying."""
    plan = get_plan(plan_id)
    problems = []
    for op in plan["ops"]:
        src, dst = safe_path(op["src"]), safe_path(op["dst"])
        if not src.exists():
            problems.append(f"source missing: {op['src']}")
        if is_occupied(dst, src):
            problems.append(f"destination exists: {op['dst']}")
    safe = not problems and plan["status"] == "pending"
    if safe and not plan["confirm_code"]:
        plan["confirm_code"] = f"APPLY-{random.randint(1000, 9999)}"
    out = {"plan_id": plan_id, "kind": plan["kind"], "status": plan["status"],
           "operations": len(plan["ops"]),
           "changes": [f"{o['src']}  ->  {o['dst']}" + (f"  [{o['note']}]" if "note" in o else "")
                       for o in plan["ops"][:MAX_PREVIEW]],
           "not_shown": max(0, len(plan["ops"]) - MAX_PREVIEW),
           "problems": problems[:10], "safe_to_apply": safe}
    if safe:
        out["confirm_code"] = plan["confirm_code"]
        out["instruction"] = ("Show these changes to the user and ask for approval. If they approve, "
                              "call apply_plan with this plan_id and confirm_code.")
    return out


@mcp.tool(annotations=MOVES)
def apply_plan(plan_id: str, confirm_code: str = "") -> dict:
    """Execute a previewed plan (moves/renames files). Needs the confirm_code from preview_plan.
    Only call this AFTER the user has approved the preview."""
    plan = get_plan(plan_id)
    if plan["status"] != "pending":
        raise ValueError(f"Plan {plan_id} is already {plan['status']}")
    if not plan["confirm_code"]:
        return {"applied": False, "message": "Refused: call preview_plan first to get a confirm_code."}
    if confirm_code.strip() != plan["confirm_code"]:
        return {"applied": False, "message": "Refused: wrong or missing confirm_code. "
                                             "It comes from preview_plan; do not guess it."}
    done, skipped = [], []
    for op in plan["ops"]:
        src, dst = safe_path(op["src"]), safe_path(op["dst"])
        if not src.exists() or is_occupied(dst, src):
            skipped.append(op["src"])
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        done.append({"src": op["src"], "dst": op["dst"]})
    plan["status"] = "applied"
    journal = load_journal()
    journal.append({"plan_id": plan_id, "kind": plan["kind"],
                    "applied_at": datetime.now().isoformat(timespec="seconds"),
                    "moves": done, "undone": False})
    save_journal(journal)
    log.info("applied plan %s: %d moved, %d skipped", plan_id, len(done), len(skipped))
    return {"applied": True, "moved": len(done), "skipped": skipped[:10],
            "undo_hint": f"undo_plan(plan_id='{plan_id}') reverses this."}


@mcp.tool(annotations=MOVES)
def undo_plan(plan_id: str = "") -> dict:
    """Reverse an applied plan. With no plan_id, reverses the most recent applied plan."""
    journal = load_journal()
    candidates = [e for e in journal if not e["undone"] and (not plan_id or e["plan_id"] == plan_id)]
    if not candidates:
        raise ValueError("Nothing to undo.")
    entry = candidates[-1]
    restored, skipped = 0, []
    for mv in reversed(entry["moves"]):
        src, dst = safe_path(mv["src"]), safe_path(mv["dst"])
        if dst.exists() and not is_occupied(src, dst):
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
            restored += 1
        else:
            skipped.append(mv["src"])
    for mv in entry["moves"]:   # tidy up empty folders the plan created
        d = safe_path(mv["dst"]).parent
        if d != ROOT and d.exists() and not any(d.iterdir()):
            d.rmdir()
    entry["undone"] = True
    save_journal(journal)
    if entry["plan_id"] in PLANS:
        PLANS[entry["plan_id"]]["status"] = "undone"
    return {"undone_plan": entry["plan_id"], "restored": restored, "skipped": skipped[:10]}


@mcp.tool(annotations=PLAN)
def discard_plan(plan_id: str) -> dict:
    """Throw away a pending plan without applying it."""
    get_plan(plan_id)
    PLANS.pop(plan_id)
    return {"discarded": plan_id}


# --------------------------------------------------------------------------
# RESOURCES + PROMPTS (kept for learning; LM Studio only uses tools, but
# hosts like Claude Desktop and MCP Inspector show these)
# --------------------------------------------------------------------------
@mcp.resource("organizer://config")
def config_resource() -> str:
    """Server configuration and category rules."""
    return json.dumps({"root": str(ROOT), "quarantine_folder": QUARANTINE,
                       "categories": {k: sorted(v) for k, v in CATEGORIES.items()}}, indent=2)


@mcp.resource("organizer://plans/{plan_id}")
def plan_resource(plan_id: str) -> str:
    """Full contents of one plan (a resource template)."""
    plan = dict(PLANS.get(plan_id, {"error": "not found"}))
    plan.pop("confirm_code", None)   # never leak the code through a side channel
    return json.dumps(plan, indent=2)


@mcp.resource("organizer://journal")
def journal_resource() -> str:
    """History of every applied plan."""
    return json.dumps(load_journal(), indent=2)


@mcp.prompt()
def organize_folder(subdir: str = ".") -> str:
    """Guided, safe workflow for tidying a folder."""
    return (f'Tidy the folder "{subdir}" with the file-organizer tools. Call organizer_info first. '
            "Summarize the folder, check for duplicates, propose a strategy and wait for my choice. "
            "Create a plan, call preview_plan, show me the changes, and only apply after I say yes.")


if __name__ == "__main__":
    log.info("file-organizer v2 starting, root=%s", ROOT)
    mcp.run(transport="stdio")
