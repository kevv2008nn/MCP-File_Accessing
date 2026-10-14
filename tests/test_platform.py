"""
End-to-end tests of the whole platform. Starts the REAL host (python run.py) as a subprocess.

Phase 1 uses the scripted model (LLM_MODE=scripted): tests MCP plumbing, agent loop, approvals, security.
Phase 2 runs a MOCK OpenAI-compatible server and points the real LLM client at it: tests the wire
format and the error messages. (Neither phase talks to a real AI model.)

Run:  python tests/test_platform.py
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
import websockets
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="platform_test_"))
fails: list[str] = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   -> {extra}" if not cond and extra else ""))
    if not cond:
        fails.append(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Host:
    """Context manager that runs the real platform in a subprocess."""

    def __init__(self, env: dict, tag: str):
        self.port = free_port()
        self.data = TMP / f"data_{tag}"
        self.files = TMP / f"files_{tag}"
        self.log = TMP / f"host_{tag}.log"
        base = {k: v for k, v in os.environ.items() if not k.startswith("LLM_")}
        self.env = {**base, "PORT": str(self.port), "AGENT_DATA_DIR": str(self.data), "ORGANIZER_ROOT": str(self.files),
                    "APPROVAL_TIMEOUT": "3", "PYTHONUNBUFFERED": "1", **env}
        self.files.mkdir(parents=True, exist_ok=True)

    def __enter__(self):
        self.proc = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=self.env,
                                     stdout=open(self.log, "w"), stderr=subprocess.STDOUT)
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/status", timeout=1).read()
                return self
            except Exception:
                time.sleep(0.5)
                if self.proc.poll() is not None:
                    break
        print(self.log.read_text()[-2000:])
        raise RuntimeError("host did not start")

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    @property
    def ws_url(self):
        return f"ws://127.0.0.1:{self.port}/ws"

    def get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5)


async def chat(ws, text, enabled=None, answer=None, timeout=30):
    await ws.send(json.dumps({"type": "chat", "text": text, "enabled": enabled}))
    events = []
    while True:
        ev = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        events.append(ev)
        if ev["type"] == "approval_request" and answer is not None:
            await ws.send(json.dumps({"type": "approval", "id": ev["id"], "approved": answer}))
        if ev["type"] == "done":
            return events


def of(events, kind):
    return [e for e in events if e["type"] == kind]


def said(events):
    return " ".join(e["text"] for e in of(events, "assistant"))


async def connect(host: Host, **kw):
    ws = await websockets.connect(host.ws_url, **kw)
    hello = json.loads(await ws.recv())
    assert hello["type"] == "hello"
    return ws, hello


# ---------------------------------------------------------------- phase 1
async def phase1():
    print("\n=== PHASE 1: scripted model ===")
    host = Host({"LLM_MODE": "scripted", "LLM_API_KEY": "sk-PLANTED-SECRET"}, "p1")
    (host.files / "song.mp3").write_bytes(b"m")
    (host.files / "notes.pdf").write_bytes(b"p")
    (host.files / "pic.jpg").write_bytes(b"i")
    with host:
        st = json.loads(host.get("/api/status").read())
        counts = {s["name"]: len(s["tools"]) for s in st["servers"]}
        check("all 3 servers connected", all(s["ok"] for s in st["servers"]) and set(counts) == {"files", "tasks", "notes"}, str(st))
        check("tool counts 14/6/6", counts == {"files": 14, "tasks": 6, "notes": 6}, str(counts))
        risks = {t["name"]: t["risk"] for s in st["servers"] for t in s["tools"]}
        check("risk labels: apply/undo/delete destructive",
              all(risks[n] == "destructive" for n in ("apply_plan", "undo_plan", "delete_task", "delete_note")), str(risks))
        check("risk labels: reads and plans not destructive",
              risks["list_tasks"] == "read" and risks["plan_dedupe"] == "write" and risks["add_task"] == "write")
        check("API key never appears in /api/status", "PLANTED-SECRET" not in json.dumps(st))
        check("API key never appears in the page or hello event", "PLANTED-SECRET" not in host.get("/").read().decode())
        r = host.get("/")
        csp = r.headers.get("Content-Security-Policy", "")
        check("index served with strict CSP (no inline script)", "script-src 'self'" in csp and "unsafe-inline" not in csp.split("style-src")[0], csp)
        check("static js/css served", host.get("/static/app.js").status == 200 and host.get("/static/style.css").status == 200)

        # ---- origin protection
        try:
            bad = await websockets.connect(host.ws_url, additional_headers={"Origin": "http://evil.example"})
            await bad.close()
            check("websocket from foreign Origin is rejected", False)
        except Exception as e:
            check("websocket from foreign Origin is rejected", "403" in str(e) or "reject" in str(e).lower(), repr(e))
        ws, hello = await connect(host, additional_headers={"Origin": f"http://localhost:{host.port}"})
        check("websocket from same-site Origin works", hello["provider"]["mode"] == "scripted")

        # ---- tool visibility
        ev = await chat(ws, "how many tools")
        check("model sees all 26 tools", "26 tools" in said(ev), said(ev))
        check("API key never sent to the browser (hello)", "PLANTED-SECRET" not in json.dumps(hello))
        ev = await chat(ws, "how many tools", enabled=["tasks"])
        check("server toggle hides other servers' tools", "6 tools" in said(ev) and "files" not in said(ev), said(ev))

        # ---- tasks: no approval needed
        ev = await chat(ws, "add task Buy milk due 2030-01-01")
        check("add_task runs without approval", not of(ev, "approval_request") and of(ev, "tool_result") and not of(ev, "tool_result")[0]["is_error"])
        check("add_task result reaches the answer", "Buy milk" in said(ev), said(ev))
        ev = await chat(ws, "add task Bad date due 2030-13-45")
        check("tool error is shown, not crashed", any(e["is_error"] for e in of(ev, "tool_result")) and of(ev, "done"))

        # ---- disabled server enforced by host (not just hidden)
        ev = await chat(ws, "add task Sneaky", enabled=["notes"])
        tr = of(ev, "tool_result")
        check("calling a switched-off server is blocked", tr and tr[0]["is_error"] and "switched off" in tr[0]["text"], str(tr))

        # ---- notes
        await chat(ws, "save note Banker :: banker algorithm avoids deadlock")
        ev = await chat(ws, "find note deadlock banker")
        check("notes: save then search finds it", "banker" in said(ev).lower(), said(ev))

        # ---- file organizer: preview -> approval -> apply
        ev = await chat(ws, "tidy", answer=False)
        ap = of(ev, "approval_request")
        check("apply_plan asks for approval", len(ap) == 1 and ap[0]["tool"] == "apply_plan", str(ap))
        check("denied: files untouched", (host.files / "song.mp3").exists() and not (host.files / "Audio").exists())
        check("denied: model told it was not done", "did not apply" in said(ev).lower(), said(ev))
        check("read/plan tools needed no approval", [a["tool"] for a in ap] == ["apply_plan"])
        ev = await chat(ws, "tidy", answer=True)
        check("approved: files really moved", (host.files / "Audio" / "song.mp3").exists() and (host.files / "Documents" / "notes.pdf").exists()
              and (host.files / "Images" / "pic.jpg").exists() and not (host.files / "song.mp3").exists(), said(ev))
        ev = await chat(ws, "undo", answer=True)
        check("undo needs approval and restores files", len(of(ev, "approval_request")) == 1 and (host.files / "song.mp3").exists()
              and not (host.files / "Audio").exists(), said(ev))

        # ---- delete task: deny, timeout, then approve
        ev = await chat(ws, "delete task 1", answer=False)
        check("delete denied -> task kept", "did not delete" in said(ev).lower(), said(ev))
        t0 = time.time()
        ev = await chat(ws, "delete task 1", answer=None, timeout=15)
        check("unanswered approval times out as DENIED", "did not delete" in said(ev).lower() and time.time() - t0 < 12, said(ev))
        ev = await chat(ws, "delete task 1", answer=True)
        check("delete approved -> task gone (so earlier denials really kept it)", "deleted" in said(ev).lower() and "Buy milk" in said(ev), said(ev))

        # ---- bad input
        ev = await chat(ws, "   ")
        check("empty message rejected politely", of(ev, "error") and of(ev, "done"))
        ev = await chat(ws, "x" * 9000)
        check("oversized message rejected", of(ev, "error") and of(ev, "done"))
        for label, frame in (("non-JSON text", "this is not json"), ("JSON but not an object", "[1,2,3]"), ("binary frame", b"\x00\x01")):
            await ws.send(frame)
            reply = json.loads(await asyncio.wait_for(ws.recv(), 5))
            check(f"malformed frame ({label}) -> polite error", reply["type"] == "error")
        ev = await chat(ws, "how many tools")
        check("session still works after malformed frames", "26 tools" in said(ev), said(ev))
        await ws.send(json.dumps({"type": "approval", "id": "does-not-exist", "approved": True}))
        await ws.send(json.dumps({"type": "nonsense"}))
        ev = await chat(ws, "how many tools")
        check("unknown approval id / unknown message type are ignored safely", "26 tools" in said(ev))
        await ws.close()

        # ---- disconnect in the middle of an approval
        ws2, _ = await connect(host)
        await ws2.send(json.dumps({"type": "chat", "text": "add task Keep me", "enabled": None}))
        while json.loads(await ws2.recv())["type"] != "done":
            pass
        await ws2.send(json.dumps({"type": "chat", "text": "delete task 2", "enabled": None}))
        while json.loads(await ws2.recv())["type"] != "approval_request":
            pass
        await ws2.close()                      # user closes the tab while the dialog is open
        await asyncio.sleep(1)
        ws3, _ = await connect(host)
        ev = await chat(ws3, "agenda")
        check("disconnect during approval: host alive and nothing was deleted", host.proc.poll() is None and of(ev, "done"))
        ev = await chat(ws3, "delete task 2", answer=True)
        check("...task 2 was still there to delete", "Keep me" in said(ev), said(ev))
        await ws3.close()

        # ---- API key never reaches tool servers
        from mcp.client.stdio import get_default_environment
        os.environ["LLM_API_KEY"] = "sk-secret"
        check("child-process env has no LLM_API_KEY", "LLM_API_KEY" not in get_default_environment())
    log = host.log.read_text()
    check("no Python tracebacks in host log", "Traceback" not in log, log[-800:] if "Traceback" in log else "")


# ---------------------------------------------------------------- phase 2
MOCK = FastAPI()
LAST: dict = {"bodies": []}


@MOCK.post("/v1/chat/completions")
async def mock_chat(req: Request):
    body = await req.json()
    LAST["bodies"].append(body)
    LAST["auth"] = req.headers.get("authorization")
    if LAST["auth"] == "Bearer bad":
        return JSONResponse({"error": {"message": "Invalid API key", "type": "auth"}}, status_code=401)
    if body["model"] == "ghost":
        return JSONResponse({"error": {"message": "model not found"}}, status_code=404)
    if body["model"] == "limited":
        return JSONResponse({"error": {"message": "quota"}}, status_code=429)
    if body["model"] == "picky":
        return JSONResponse({"error": {"message": "tools not supported for this model"}}, status_code=400)
    msgs, last = body["messages"], body["messages"][-1]

    def reply(message, finish):
        return {"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": finish}]}

    if last["role"] == "user" and "agenda" in last["content"]:
        return reply({"role": "assistant", "content": None, "tool_calls": [
            {"id": "", "type": "function", "function": {"name": "tasks__agenda", "arguments": "{\"days\": 1}"}}]}, "tool_calls")
    if last["role"] == "user" and "garbage" in last["content"]:
        return reply({"role": "assistant", "content": None, "tool_calls": [
            {"id": "c9", "type": "function", "function": {"name": "tasks__add_task", "arguments": "{not json"}}]}, "tool_calls")
    if last["role"] == "tool":
        return reply({"role": "assistant", "content": "Your agenda is: " + last["content"][:60].replace("\n", " ")}, "stop")
    return reply({"role": "assistant", "content": "hello from the mock"}, "stop")


def start_mock() -> tuple[int, uvicorn.Server]:
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(MOCK, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    return port, server


async def phase2():
    print("\n=== PHASE 2: real LLM client against a mock OpenAI-compatible server ===")
    port, server = start_mock()
    base = f"http://127.0.0.1:{port}/v1"
    with Host({"LLM_BASE_URL": base, "LLM_MODEL": "good", "LLM_API_KEY": "key123"}, "p2a") as host:
        ws, hello = await connect(host)
        check("provider label shown, key hidden", "good" in hello["provider"]["label"] and "key123" not in json.dumps(hello))
        ev = await chat(ws, "what's my agenda?")
        check("tool call round trip through the real LLM client", of(ev, "tool_call") and "Your agenda is" in said(ev), str(ev)[-600:])
        first, second = LAST["bodies"][0], LAST["bodies"][1]
        names = {t["function"]["name"] for t in first["tools"]}
        check("provider received namespaced tools", {"files__plan_rename", "tasks__add_task", "notes__search_notes"} <= names and len(names) == 26)
        add = next(t for t in first["tools"] if t["function"]["name"] == "tasks__add_task")["function"]["parameters"]
        check("schema cleaned but param 'title' kept", "title" in add["properties"] and "title" not in add and add["properties"]["priority"].get("enum"), str(add))
        rename = next(t for t in first["tools"] if t["function"]["name"] == "files__plan_rename")["function"]["parameters"]
        check("Literal enum reaches the model", "enum" in rename["properties"]["mode"])
        check("auth header sent", LAST["auth"] == "Bearer key123")
        check("system prompt has date + servers", first["messages"][0]["role"] == "system" and "files, tasks, notes" in first["messages"][0]["content"])
        asst = next(m for m in second["messages"] if m["role"] == "assistant" and m.get("tool_calls"))
        tool = next(m for m in second["messages"] if m["role"] == "tool")
        check("empty tool-call id replaced; assistant/tool ids pair up", asst["tool_calls"][0]["id"].startswith("call_") and tool["tool_call_id"] == asst["tool_calls"][0]["id"], str(asst))
        ev = await chat(ws, "please garbage")
        tr = of(ev, "tool_result")
        check("invalid JSON args -> error fed back to model, no crash", tr and tr[0]["is_error"] and "valid JSON" in tr[0]["text"], str(tr))
        # history reaches the provider on a later turn
        await chat(ws, "hi")
        check("conversation history is resent", len(LAST["bodies"][-1]["messages"]) > 6)
        await ws.close()

    cases = [("bad key -> friendly 401", {"LLM_API_KEY": "bad", "LLM_MODEL": "good"}, "API key"),
             ("unknown model -> friendly 404", {"LLM_MODEL": "ghost"}, "was not found"),
             ("quota -> friendly 429", {"LLM_MODEL": "limited"}, "quota"),
             ("400 about tools -> hint", {"LLM_MODEL": "picky"}, "function calling"),
             ("server down -> friendly connection error", {"LLM_MODEL": "good", "LLM_BASE_URL": f"http://127.0.0.1:{free_port()}/v1"}, "Cannot reach"),
             ("not configured -> setup hint", {"LLM_BASE_URL": "", "LLM_MODEL": ""}, "No model configured")]
    for i, (name, env, needle) in enumerate(cases):
        full = {"LLM_BASE_URL": base, "LLM_API_KEY": "key123", **env}
        with Host(full, f"p2e{i}") as host:
            ws, _ = await connect(host)
            ev = await chat(ws, "hello", timeout=60)
            err = " ".join(e["text"] for e in of(ev, "error"))
            check(name, needle.lower() in err.lower() and of(ev, "done"), err)
            ev2 = await chat(ws, "hello again", timeout=60)
            check(f"   ...and the session is still usable after: {name.split(' ->')[0]}", of(ev2, "done"))
            await ws.close()
    server.should_exit = True


def unit_origin():
    print("\n=== PHASE 0: origin/host check (unit) ===")
    os.environ["PORT"] = "8000"
    from host.app import origin_ok

    class W:
        def __init__(self, host, origin=None):
            self.headers = {"host": host, **({"origin": origin} if origin else {})}

    cases = [("same-site localhost", W("localhost:8000", "http://localhost:8000"), True),
             ("same-site 127.0.0.1", W("127.0.0.1:8000", "http://127.0.0.1:8000"), True),
             ("no Origin (non-browser client)", W("127.0.0.1:8000"), True),
             ("foreign site", W("127.0.0.1:8000", "http://evil.example"), False),
             ("lookalike host", W("127.0.0.1:8000", "http://localhost:8000.evil.example"), False),
             ("wrong port", W("127.0.0.1:8000", "http://127.0.0.1:9999"), False),
             ("literal null origin (sandboxed iframe)", W("127.0.0.1:8000", "null"), False),
             ("DNS-rebinding Host header", W("evil.example:8000", "http://evil.example:8000"), False)]
    for name, ws, want in cases:
        check(f"origin_ok: {name}", origin_ok(ws) is want)


async def main():
    try:
        unit_origin()
        await phase1()
        await phase2()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("\nRESULT:", f"{len(fails)} FAILED: {fails}" if fails else "ALL TESTS PASSED")
    sys.exit(1 if fails else 0)


asyncio.run(main())
