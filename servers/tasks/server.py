#!/usr/bin/env python3
"""
tasks: a personal to-do list as an MCP server (SQLite storage).

Dates are always YYYY-MM-DD. The host tells the model today's date in its
system prompt, so the model converts "tomorrow" or "next Friday" itself.
NEVER print() to stdout (stdio transport). Log to stderr.
"""
import logging
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tasks")

DB_PATH = Path(os.environ.get("TASKS_DB", Path.home() / ".agent_platform" / "tasks.db")).expanduser()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
ADD = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
EDIT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

mcp = FastMCP("tasks")
PRIORITY_ORDER = "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END"


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        due TEXT,
        priority TEXT NOT NULL DEFAULT 'medium',
        tags TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open',
        created TEXT NOT NULL,
        completed TEXT)""")
    return con


def check_date(value: str) -> str:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Date must be YYYY-MM-DD, got {value!r}")


def fmt(r: sqlite3.Row) -> str:
    bits = [f"#{r['id']}", f"[{r['priority']}]", r["title"]]
    if r["due"]:
        bits.append(f"(due {r['due']})")
    if r["tags"]:
        bits.append(" ".join("#" + t for t in r["tags"].split(",") if t))
    if r["status"] == "done":
        bits.append("DONE")
    return " ".join(bits)


def clean_tags(tags: str) -> str:
    return ",".join(sorted({t.strip().lstrip("#").lower() for t in tags.split(",") if t.strip()}))


def get_row(con: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise ValueError(f"No task with id {task_id}. Use list_tasks to see valid ids.")
    return row


@mcp.tool(annotations=ADD)
def add_task(title: str, due: str = "", priority: Literal["low", "medium", "high"] = "medium",
             tags: str = "") -> dict:
    """Add a new task. due is optional and must be YYYY-MM-DD. tags is a comma-separated string."""
    if not title.strip():
        raise ValueError("title must not be empty")
    due = check_date(due) if due else None
    with db() as con:
        cur = con.execute("INSERT INTO tasks (title, due, priority, tags, created) VALUES (?,?,?,?,?)",
                          (title.strip(), due, priority, clean_tags(tags), datetime.now().isoformat(timespec="seconds")))
        row = get_row(con, cur.lastrowid)
    return {"added": fmt(row)}


@mcp.tool(annotations=READ)
def list_tasks(status: Literal["open", "done", "all"] = "open", tag: str = "", limit: int = 30) -> dict:
    """List tasks, soonest due date first. Filter by status and optionally by one tag."""
    q, args = "SELECT * FROM tasks WHERE 1=1", []
    if status != "all":
        q += " AND status=?"
        args.append(status)
    if tag:
        q += " AND (',' || tags || ',') LIKE ?"
        args.append(f"%,{tag.strip().lstrip('#').lower()},%")
    q += f" ORDER BY (due IS NULL), due, {PRIORITY_ORDER}, id LIMIT ?"
    args.append(max(1, min(limit, 100)))
    with db() as con:
        rows = con.execute(q, args).fetchall()
    return {"count": len(rows), "tasks": [fmt(r) for r in rows]}


@mcp.tool(annotations=READ)
def agenda(days: int = 1) -> dict:
    """What needs attention: overdue open tasks plus tasks due from today through the next `days` days
    (days=1 means today only; days=7 means the coming week)."""
    today = date.today()
    end = today + timedelta(days=max(1, min(days, 60)) - 1)
    with db() as con:
        overdue = con.execute(f"SELECT * FROM tasks WHERE status='open' AND due < ? ORDER BY due, {PRIORITY_ORDER}",
                              (today.isoformat(),)).fetchall()
        upcoming = con.execute(f"SELECT * FROM tasks WHERE status='open' AND due BETWEEN ? AND ? "
                               f"ORDER BY due, {PRIORITY_ORDER}", (today.isoformat(), end.isoformat())).fetchall()
        undated = con.execute("SELECT COUNT(*) FROM tasks WHERE status='open' AND due IS NULL").fetchone()[0]
    return {"today": today.isoformat(), "overdue": [fmt(r) for r in overdue],
            f"due_through_{end.isoformat()}": [fmt(r) for r in upcoming],
            "open_tasks_without_due_date": undated}


@mcp.tool(annotations=EDIT)
def complete_task(task_id: int) -> dict:
    """Mark a task as done."""
    with db() as con:
        row = get_row(con, task_id)
        if row["status"] == "done":
            return {"message": f"Task #{task_id} was already done."}
        con.execute("UPDATE tasks SET status='done', completed=? WHERE id=?",
                    (datetime.now().isoformat(timespec="seconds"), task_id))
        return {"completed": fmt(get_row(con, task_id))}


@mcp.tool(annotations=EDIT)
def reschedule_task(task_id: int, due: str) -> dict:
    """Change a task's due date. due must be YYYY-MM-DD."""
    due = check_date(due)
    with db() as con:
        get_row(con, task_id)
        con.execute("UPDATE tasks SET due=? WHERE id=?", (due, task_id))
        return {"rescheduled": fmt(get_row(con, task_id))}


@mcp.tool(annotations=DELETE)
def delete_task(task_id: int) -> dict:
    """Permanently delete a task. This cannot be undone; prefer complete_task unless the user asked to delete."""
    with db() as con:
        row = get_row(con, task_id)
        con.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return {"deleted": fmt(row)}


if __name__ == "__main__":
    log.info("tasks server starting, db=%s", DB_PATH)
    mcp.run(transport="stdio")
