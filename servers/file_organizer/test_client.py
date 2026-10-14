"""Acts as a tiny MCP host: launches server.py over stdio and exercises every tool.
Usage:  python test_client.py [optional_test_folder]"""
import asyncio, json, os, shutil, sys, tempfile
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / "org_test"

def setup():
    shutil.rmtree(ROOT, ignore_errors=True)
    d = ROOT / "messy"; d.mkdir(parents=True)
    (d / "Holiday Photo.JPG").write_bytes(b"img1")
    (d / "Holiday Photo copy.JPG").write_bytes(b"img1")      # duplicate
    (d / "My Report (final).pdf").write_bytes(b"pdfdata")
    (d / "budget 2026.xlsx").write_bytes(b"xls")
    (d / "song.mp3").write_bytes(b"mp3")
    (d / "weird.xyz").write_bytes(b"???")
    (d / "big.bin").write_bytes(b"0" * 2_000_000)

def data(res):
    return json.loads(res.content[0].text)

def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        check.failed = True
check.failed = False

async def main():
    setup()
    params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name("server.py"))],
                                   env={**os.environ, "ORGANIZER_ROOT": str(ROOT)})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            print("TOOLS:", tools, "\n")
            call = lambda n, a=None: s.call_tool(n, a or {})

            check("organizer_info works", "workflow" in data(await call("organizer_info")))
            check("summarize counts 7 files", data(await call("summarize_folder", {"subdir": "messy"}))["total_files"] == 7)
            check("finds 1 duplicate group", data(await call("find_duplicates", {"subdir": "messy"}))["duplicate_groups"] == 1)
            check("finds 1 large file", data(await call("find_large_files", {"subdir": "messy", "min_mb": 1}))["found"] == 1)

            # --- sort by type with full safety flow ---
            plan = data(await call("plan_sort_by_type", {"subdir": "messy"}))
            pid = plan["plan_id"]
            check("plan created, nothing moved yet", (ROOT / "messy" / "song.mp3").exists())
            r1 = data(await call("apply_plan", {"plan_id": pid, "confirm_code": "APPLY-0000"}))
            check("apply WITHOUT preview refused", r1["applied"] is False)
            prev = data(await call("preview_plan", {"plan_id": pid}))
            check("preview is safe and gives code", prev["safe_to_apply"] and prev["confirm_code"].startswith("APPLY-"))
            r2 = data(await call("apply_plan", {"plan_id": pid, "confirm_code": "APPLY-0000"}))
            check("apply with WRONG code refused", r2["applied"] is False)
            check("files still untouched after refusals", (ROOT / "messy" / "song.mp3").exists())
            r3 = data(await call("apply_plan", {"plan_id": pid, "confirm_code": prev["confirm_code"]}))
            check("apply with right code moves 7 files", r3["applied"] and r3["moved"] == 7)
            check("song now in Audio/", (ROOT / "messy" / "Audio" / "song.mp3").exists())
            twice = await call("apply_plan", {"plan_id": pid, "confirm_code": prev["confirm_code"]})
            check("cannot apply the same plan twice (isError)", twice.isError)
            und = data(await call("undo_plan"))
            check("undo restores 7 files", und["restored"] == 7 and (ROOT / "messy" / "song.mp3").exists())
            check("empty Audio/ folder cleaned up", not (ROOT / "messy" / "Audio").exists())

            # --- rename + dedupe plans ---
            rn = data(await call("plan_rename", {"subdir": "messy", "mode": "snake_case"}))
            rp = data(await call("preview_plan", {"plan_id": rn["plan_id"]}))
            check("rename plans snake_case", any("my_report_final.pdf" in c for c in rp["changes"]))
            dd = data(await call("plan_dedupe", {"subdir": "messy"}))
            dp = data(await call("preview_plan", {"plan_id": dd["plan_id"]}))
            check("dedupe quarantines exactly 1 file", dp["operations"] == 1 and "_duplicates" in dp["changes"][0])
            dres = data(await call("apply_plan", {"plan_id": dd["plan_id"], "confirm_code": dp["confirm_code"]}))
            check("dedupe applied, original kept, nothing deleted",
                  dres["applied"] and (ROOT / "messy" / "Holiday Photo.JPG").exists()
                  and len(list((ROOT / "messy" / "_duplicates").iterdir())) == 1)
            data(await call("undo_plan"))

            # --- error handling / sandbox ---
            bad = await call("list_files", {"subdir": "../../etc"})
            check("path escape rejected (isError)", bad.isError)
            bad2 = await call("plan_rename", {"mode": "nonsense"})
            check("bad mode rejected (isError)", bad2.isError)

    print("\nRESULT:", "SOME TESTS FAILED" if check.failed else "ALL TESTS PASSED")

asyncio.run(main())
