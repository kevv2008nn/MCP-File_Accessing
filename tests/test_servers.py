"""Tests tasks + notes servers directly over MCP stdio (no host, no LLM)."""
import asyncio, json, os, shutil, sys, tempfile
from datetime import date, timedelta
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="srv_test_"))
fails = []

def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   {extra}" if not cond and extra else ""))
    if not cond: fails.append(name)

def data(res): return json.loads(res.content[0].text)

async def with_server(script, env, fn):
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "servers" / script / "server.py")],
                                   env={**os.environ, **env})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            await fn(s)

async def tasks(s):
    tools = {t.name: t for t in (await s.list_tools()).tools}
    check("tasks: 6 tools", len(tools) == 6, str(list(tools)))
    check("tasks: delete is destructive", tools["delete_task"].annotations.destructiveHint is True)
    check("tasks: list is read-only", tools["list_tasks"].annotations.readOnlyHint is True)
    check("tasks: priority has enum in schema", "enum" in tools["add_task"].inputSchema["properties"]["priority"])
    today, tomorrow, yday = date.today(), date.today() + timedelta(days=1), date.today() - timedelta(days=1)
    call = lambda n, a=None: s.call_tool(n, a or {})
    await call("add_task", {"title": "Pay rent", "due": yday.isoformat(), "priority": "high", "tags": "Home, money"})
    await call("add_task", {"title": "Lab record", "due": today.isoformat()})
    await call("add_task", {"title": "Call mom", "due": tomorrow.isoformat(), "priority": "low"})
    await call("add_task", {"title": "Someday idea"})
    ag = data(await call("agenda", {"days": 1}))
    check("agenda: 1 overdue", len(ag["overdue"]) == 1 and "Pay rent" in ag["overdue"][0])
    check("agenda days=1 shows only today", len(ag[f"due_through_{today.isoformat()}"]) == 1)
    check("agenda days=2 includes tomorrow", len(data(await call("agenda", {"days": 2}))[f"due_through_{tomorrow.isoformat()}"]) == 2)
    check("tags normalised + filterable", data(await call("list_tasks", {"tag": "#MONEY"}))["count"] == 1)
    check("bad date rejected", (await call("add_task", {"title": "x", "due": "tomorrow"})).isError)
    check("bad priority rejected (schema enum)", (await call("add_task", {"title": "x", "priority": "urgent"})).isError)
    check("empty title rejected", (await call("add_task", {"title": "  "})).isError)
    check("complete works", "DONE" in data(await call("complete_task", {"task_id": 1}))["completed"])
    check("open list excludes done", data(await call("list_tasks"))["count"] == 3)
    check("reschedule works", "2030-01-01" in data(await call("reschedule_task", {"task_id": 2, "due": "2030-01-01"}))["rescheduled"])
    check("unknown id -> isError", (await call("complete_task", {"task_id": 999})).isError)
    check("delete works", "deleted" in data(await call("delete_task", {"task_id": 4})))
    check("SQL-injection-ish title stored literally", "Robert" in data(await call("add_task", {"title": "Robert'); DROP TABLE tasks;--"}))["added"])
    check("table survived", data(await call("list_tasks", {"status": "all"}))["count"] >= 4)

async def notes(s):
    tools = {t.name: t for t in (await s.list_tools()).tools}
    check("notes: 6 tools", len(tools) == 6, str(list(tools)))
    check("notes: delete is destructive", tools["delete_note"].annotations.destructiveHint is True)
    call = lambda n, a=None: s.call_tool(n, a or {})
    r1 = data(await call("add_note", {"title": "Operating Systems Exp 6", "content": "Banker's algorithm avoids deadlock by checking safe states.", "tags": "os, lab"}))
    await call("add_note", {"title": "Grocery list", "content": "milk, eggs, rice"})
    r3 = data(await call("add_note", {"title": "Grocery list", "content": "second one"}))
    check("duplicate title gets suffix", r3["created"] == "grocery-list-2", r3["created"])
    hits = data(await call("search_notes", {"query": "how does deadlock avoidance work"}))
    check("search finds the OS note first", hits["results"] and hits["results"][0]["name"] == r1["created"], str(hits))
    check("search snippet present", "deadlock" in hits["results"][0]["snippet"].lower())
    check("search with only stopwords -> isError", (await call("search_notes", {"query": "the of a"})).isError)
    check("no match -> empty results", data(await call("search_notes", {"query": "quantum"}))["matches"] == 0)
    await call("append_to_note", {"name": "grocery-list", "text": "bananas"})
    check("append works", "bananas" in data(await call("read_note", {"name": "Grocery list"}))["content"])
    check("path traversal impossible", (await call("read_note", {"name": "../../etc/passwd"})).isError)
    esc = await call("add_note", {"title": "../../evil", "content": "x"})
    check("traversal title stays inside dir", (Path(TMP / "notes" / "evil.md")).exists() and not (TMP.parent / "evil.md").exists())
    check("list_notes counts", data(await call("list_notes"))["total"] == 4)
    check("delete works", "deleted" in data(await call("delete_note", {"name": "grocery-list-2"})))

async def main():
    await with_server("tasks", {"TASKS_DB": str(TMP / "t.db")}, tasks)
    await with_server("notes", {"NOTES_DIR": str(TMP / "notes")}, notes)
    shutil.rmtree(TMP, ignore_errors=True)
    print("\nRESULT:", f"{len(fails)} FAILED" if fails else "ALL TESTS PASSED")
    sys.exit(1 if fails else 0)
asyncio.run(main())
