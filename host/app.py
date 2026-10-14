"""
Web backend: serves the UI and runs one agent session per WebSocket connection.

Security notes (a local agent that can touch your files is a juicy target):
  * binds to 127.0.0.1 only (see run.py)
  * the WebSocket checks the Host and Origin headers. Without this, ANY website open in your
    browser could connect to ws://localhost:PORT/ws and make your agent run tools.
  * the API key is never sent to the browser or to MCP servers
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .agent import Agent
from .llm import LLMError, make_llm, provider_info
from .mcp_manager import MCPManager

log = logging.getLogger("host.app")
ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DATA_DIR = Path(os.environ.get("AGENT_DATA_DIR", ROOT / "data"))
PORT = int(os.environ.get("PORT", "8000"))
APPROVAL_TIMEOUT = float(os.environ.get("APPROVAL_TIMEOUT", "300"))
MAX_MESSAGE_CHARS = 8000

ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"} | \
    {o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()}

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       f"connect-src 'self' ws://127.0.0.1:{PORT} ws://localhost:{PORT}; frame-ancestors 'none'")

manager: MCPManager | None = None
agent: Agent | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager, agent
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    manager = MCPManager(ROOT / "config.json", ROOT, DATA_DIR)
    await manager.start()
    agent = Agent(manager, max_steps=int(os.environ.get("MAX_STEPS", "8")))
    yield
    await manager.stop()


app = FastAPI(lifespan=lifespan, title="MCP Agent Platform")
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html", headers={"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff"})


@app.get("/api/status")
async def status():
    return {"servers": manager.status(), "provider": provider_info(), "approval": manager.approval_mode}


def origin_ok(ws: WebSocket) -> bool:
    if (ws.headers.get("host") or "").lower() not in ALLOWED_HOSTS:
        return False                      # blocks DNS-rebinding style access
    origin = ws.headers.get("origin")
    return origin is None or origin.lower() in ALLOWED_ORIGINS   # browsers always send Origin; other sites are rejected


@app.websocket("/ws")
async def chat_socket(ws: WebSocket):
    if not origin_ok(ws):
        log.warning("rejected websocket: host=%r origin=%r", ws.headers.get("host"), ws.headers.get("origin"))
        await ws.close(code=1008)
        return
    await ws.accept()

    history: list[dict] = []
    approvals: dict[str, asyncio.Future] = {}
    run_task: asyncio.Task | None = None
    llm = None

    async def emit(event: dict) -> None:
        try:
            await ws.send_json(event)
        except Exception:      # noqa: BLE001 - client went away; the run will be cancelled in `finally`
            pass

    async def ask_approval(call_id: str, server: str, tool: str, args: dict) -> bool:
        fut = asyncio.get_running_loop().create_future()
        approvals[call_id] = fut
        await emit({"type": "approval_request", "id": call_id, "server": server, "tool": tool, "args": args})
        try:
            return await asyncio.wait_for(fut, APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            return False
        finally:
            approvals.pop(call_id, None)

    async def run(text: str, enabled: set[str] | None) -> None:
        nonlocal llm
        try:
            if llm is None:
                llm = make_llm()
            await agent.run(llm, history, text, enabled, emit, ask_approval)
        except LLMError as e:
            await emit({"type": "error", "text": str(e)})
        except asyncio.CancelledError:
            await emit({"type": "error", "text": "Stopped."})
            raise
        except Exception as e:     # noqa: BLE001
            log.exception("agent run failed")
            await emit({"type": "error", "text": f"Unexpected error: {type(e).__name__}: {e}"})
        finally:
            await emit({"type": "done"})

    await emit({"type": "hello", "servers": manager.status(), "provider": provider_info(), "approval": manager.approval_mode})
    try:
        while True:
            frame = await ws.receive()
            if frame["type"] == "websocket.disconnect":
                break
            try:
                msg = json.loads(frame["text"]) if frame.get("text") is not None else None
            except ValueError:
                msg = None
            if not isinstance(msg, dict):      # malformed or binary frame: answer politely, keep the session
                await emit({"type": "error", "text": "Invalid message (expected a JSON object)."})
                continue
            kind = msg.get("type")
            if kind == "chat":
                text = str(msg.get("text", "")).strip()
                if not text or len(text) > MAX_MESSAGE_CHARS:
                    await emit({"type": "error", "text": f"Message must be 1 to {MAX_MESSAGE_CHARS} characters."})
                    await emit({"type": "done"})
                elif run_task and not run_task.done():
                    await emit({"type": "error", "text": "Still working on the previous message."})
                else:
                    raw = msg.get("enabled")
                    enabled = {s for s in raw if isinstance(s, str)} if isinstance(raw, list) else None
                    run_task = asyncio.create_task(run(text, enabled))
            elif kind == "approval":
                fut = approvals.get(str(msg.get("id")))
                if fut and not fut.done():
                    fut.set_result(bool(msg.get("approved")))
            elif kind == "stop":
                if run_task and not run_task.done():
                    run_task.cancel()
            elif kind == "reset":
                if not (run_task and not run_task.done()):
                    history.clear()
                    await emit({"type": "reset_ok"})
    except WebSocketDisconnect:
        pass
    finally:
        for fut in approvals.values():          # nothing may stay half-approved
            if not fut.done():
                fut.set_result(False)
        if run_task and not run_task.done():
            run_task.cancel()
