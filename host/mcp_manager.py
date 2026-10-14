"""
MCPManager: connects to several MCP servers and presents them as ONE tool catalogue.

Key ideas (this is the "host" side of MCP):
  * each server runs as a child process, spoken to over stdio
  * tools are namespaced as  <server>__<tool>  so two servers can't collide
  * a tool's ANNOTATIONS decide whether the user must approve a call
  * child processes only get the env vars listed in config.json, never the
    host's own environment (which holds your LLM API key)
"""
import asyncio
import json
import logging
import os
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger("host.mcp")

MAX_RESULT_CHARS = 6000
CONNECT_TIMEOUT = 30
CALL_TIMEOUT = 120
SEP = "__"


def strip_titles(schema):
    """Remove the auto-generated 'title' annotations (pure noise, wastes context tokens).
    A *property* that happens to be called 'title' has a dict value, so it is kept."""
    if isinstance(schema, dict):
        return {k: strip_titles(v) for k, v in schema.items() if not (k == "title" and isinstance(v, str))}
    if isinstance(schema, list):
        return [strip_titles(v) for v in schema]
    return schema


@dataclass
class ManagedServer:
    name: str
    session: ClientSession | None = None
    tools: list = field(default_factory=list)
    error: str | None = None


class MCPManager:
    def __init__(self, config_path: Path, root: Path, data_dir: Path):
        self.cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.root, self.data_dir = Path(root), Path(data_dir)
        self.servers: dict[str, ManagedServer] = {}
        self._stack = AsyncExitStack()
        self.approval_mode = self.cfg.get("approval", "auto")       # "auto" or "always"
        self.always_ask = set(self.cfg.get("always_ask", []))       # qualified names
        self.auto_approve = set(self.cfg.get("auto_approve", []))   # qualified names

    # ---------- config helpers ----------
    def _expand(self, value: str) -> str:
        """Supports ${PYTHON} ${ROOT} ${DATA} and ${ENVVAR:-default}."""
        value = (value.replace("${PYTHON}", sys.executable)
                      .replace("${ROOT}", str(self.root))
                      .replace("${DATA}", str(self.data_dir)))
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}",
                      lambda m: os.environ.get(m.group(1)) or m.group(2), value)

    # ---------- lifecycle ----------
    async def start(self) -> None:
        for name, spec in self.cfg["servers"].items():
            if SEP in name:
                raise ValueError(f"Server name {name!r} must not contain '{SEP}'")
            await self._connect(name, spec)

    async def _connect(self, name: str, spec: dict) -> None:
        srv = self.servers[name] = ManagedServer(name)
        local = AsyncExitStack()   # per-server stack so one broken server can't take down the rest
        try:
            params = StdioServerParameters(
                command=self._expand(spec["command"]),
                args=[self._expand(a) for a in spec.get("args", [])],
                env={k: self._expand(v) for k, v in spec.get("env", {}).items()})  # NOT os.environ
            read, write = await local.enter_async_context(stdio_client(params))
            session = await local.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), CONNECT_TIMEOUT)
            srv.tools = (await asyncio.wait_for(session.list_tools(), CONNECT_TIMEOUT)).tools
            srv.session = session
            self._stack.push_async_callback(local.aclose)
            log.info("connected to %r: %d tools", name, len(srv.tools))
        except BaseException as e:   # noqa: BLE001 - includes cancel/timeouts; we report and move on
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            srv.error = f"{type(e).__name__}: {e}"
            log.error("could not connect to %r: %s", name, srv.error)
            try:
                await local.aclose()
            except BaseException:   # noqa: BLE001
                pass

    async def stop(self) -> None:
        await self._stack.aclose()

    # ---------- tool catalogue ----------
    def openai_tools(self, enabled: set[str] | None = None) -> list[dict]:
        """Tool list in OpenAI function-calling format, optionally limited to some servers."""
        out = []
        for name, srv in self.servers.items():
            if srv.session is None or (enabled is not None and name not in enabled):
                continue
            for t in srv.tools:
                out.append({"type": "function", "function": {
                    "name": f"{name}{SEP}{t.name}",
                    "description": (t.description or "")[:1000],
                    "parameters": strip_titles(t.inputSchema)}})
        return out

    def resolve(self, qualified: str):
        server, _, tool = qualified.partition(SEP)
        srv = self.servers.get(server)
        if not srv or srv.session is None:
            return None
        for t in srv.tools:
            if t.name == tool:
                return srv, t
        return None

    def needs_approval(self, qualified: str) -> bool:
        """MCP spec defaults: a tool is assumed to be destructive unless it says otherwise."""
        if qualified in self.always_ask or self.approval_mode == "always":
            return True
        if qualified in self.auto_approve:
            return False
        found = self.resolve(qualified)
        if not found:
            return True
        ann = found[1].annotations
        if ann is None:
            return True
        if ann.readOnlyHint is True:
            return False
        return ann.destructiveHint is not False

    def risk(self, tool) -> str:
        ann = tool.annotations
        if ann is not None and ann.readOnlyHint is True:
            return "read"
        if ann is not None and ann.destructiveHint is False:
            return "write"
        return "destructive"

    def status(self) -> list[dict]:
        return [{"name": n, "ok": s.session is not None, "error": s.error,
                 "tools": [{"name": t.name, "risk": self.risk(t),
                            "description": (t.description or "").strip().split("\n")[0][:140]}
                           for t in s.tools]}
                for n, s in self.servers.items()]

    # ---------- calling ----------
    async def call(self, qualified: str, args: dict) -> tuple[str, bool]:
        """Returns (text, is_error). Never raises: failures become error text the model can read."""
        found = self.resolve(qualified)
        if not found:
            return f"Unknown tool {qualified!r}.", True
        srv, tool = found
        try:
            result = await asyncio.wait_for(srv.session.call_tool(tool.name, args), CALL_TIMEOUT)
        except asyncio.TimeoutError:
            return f"Tool {qualified} timed out after {CALL_TIMEOUT}s.", True
        except Exception as e:   # noqa: BLE001
            log.exception("tool call failed")
            return f"Tool {qualified} failed: {type(e).__name__}: {e}", True
        parts = [c.text if getattr(c, "type", "") == "text" else f"[{getattr(c, 'type', 'unknown')} content omitted]"
                 for c in result.content]
        text = "\n".join(parts) or "(no output)"
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + f"\n...[truncated {len(text) - MAX_RESULT_CHARS} characters]"
        return text, bool(result.isError)
