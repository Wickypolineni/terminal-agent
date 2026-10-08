"""MCP (Model Context Protocol) servers as agent tools.

Servers are listed in config.json under "mcp_servers" (stdio servers: command, args, env).
Each one runs inside a background asyncio loop; its tools are exposed to Claude as
`mcp__<server>__<tool>` and go through the same policy, approval and ledger as built-in tools.
MCP's own tool annotations (readOnlyHint, destructiveHint, openWorldHint) feed the policy.
"""
import asyncio
import json
import re
import threading
from dataclasses import dataclass

from anthropic.lib.tools.mcp import UnsupportedMCPValueError, mcp_content
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

CONNECT_TIMEOUT = 60
CALL_TIMEOUT = 300


@dataclass
class MCPTool:
    server: str
    name: str             # the server's own name for the tool
    api_name: str         # what Claude sees: mcp__server__tool
    description: str
    input_schema: dict
    hints: dict


def _safe(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


def _field(obj, *names):
    """Read a field under its MCP v2 (snake_case) or v1 (camelCase) name."""
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


HINTS = {"readOnlyHint": ("read_only_hint", "readOnlyHint"),
         "destructiveHint": ("destructive_hint", "destructiveHint"),
         "openWorldHint": ("open_world_hint", "openWorldHint")}


class MCPManager:
    def __init__(self, servers: dict) -> None:
        self.servers = servers
        self.sessions: dict[str, ClientSession] = {}
        self.tools: list[MCPTool] = []
        self.errors: dict[str, str] = {}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._stop = asyncio.run_coroutine_threadsafe(self._make_event(), self._loop).result()
        self._tasks: list[asyncio.Future] = []

    @staticmethod
    async def _make_event() -> asyncio.Event:
        return asyncio.Event()

    def start(self) -> None:
        """Connect to every configured server; failures are recorded, not fatal."""
        for name, cfg in self.servers.items():
            ready: asyncio.Future = asyncio.run_coroutine_threadsafe(self._make_future(), self._loop).result()
            self._tasks.append(asyncio.run_coroutine_threadsafe(self._serve(name, cfg, ready), self._loop))
            try:
                tools = asyncio.run_coroutine_threadsafe(self._wait(ready), self._loop).result(CONNECT_TIMEOUT + 5)
            except Exception as e:  # a broken server shouldn't stop the agent
                self.errors[name] = f"{type(e).__name__}: {e}"
                continue
            for tool in tools:
                annotations = getattr(tool, "annotations", None)
                hints = {}
                if annotations is not None:
                    hints = {key: value for key, names in HINTS.items()
                             if (value := _field(annotations, *names)) is not None}
                self.tools.append(MCPTool(
                    server=name, name=tool.name, api_name=f"mcp__{_safe(name)}__{_safe(tool.name)}"[:128],
                    description=(tool.description or f"{tool.name} (from MCP server {name})")[:1024],
                    input_schema=_field(tool, "input_schema", "inputSchema") or {"type": "object", "properties": {}},
                    hints=hints))

    @staticmethod
    async def _make_future() -> asyncio.Future:
        return asyncio.get_running_loop().create_future()

    @staticmethod
    async def _wait(ready: asyncio.Future):
        return await asyncio.wait_for(asyncio.shield(ready), CONNECT_TIMEOUT)

    async def _serve(self, name: str, cfg: dict, ready: asyncio.Future) -> None:
        # The stdio client and session must be opened and closed in the same task, so each
        # server lives in one long-running task that waits for the shutdown signal.
        params = StdioServerParameters(command=cfg["command"], args=cfg.get("args", []),
                                       env=cfg.get("env"), cwd=cfg.get("cwd"))
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self.sessions[name] = session
                    ready.set_result(listed.tools)
                    await self._stop.wait()
        except Exception as e:
            if not ready.done():
                ready.set_exception(e)

    def call(self, tool: MCPTool, arguments: dict):
        session = self.sessions.get(tool.server)
        if session is None:
            return f"MCP server {tool.server} is not connected.", True
        try:
            result = asyncio.run_coroutine_threadsafe(
                session.call_tool(tool.name, arguments), self._loop).result(CALL_TIMEOUT)
        except TimeoutError:
            return f"MCP tool {tool.api_name} timed out after {CALL_TIMEOUT}s.", True
        except Exception as e:
            return f"MCP tool {tool.api_name} failed: {type(e).__name__}: {e}", True

        content = getattr(result, "content", None)
        if content is None:   # MCP v2 can ask for interactive input mid-call; not supported here
            return f"MCP tool {tool.api_name} asked for interactive input, which this agent can't provide.", True
        blocks = []
        for item in content:
            try:
                blocks.append(mcp_content(item))
            except UnsupportedMCPValueError:
                blocks.append({"type": "text", "text": f"[unsupported {getattr(item, 'type', '?')} content omitted]"})
        structured = _field(result, "structured_content", "structuredContent")
        if not blocks and structured is not None:
            blocks = [{"type": "text", "text": json.dumps(structured, indent=2)}]
        if not blocks:
            blocks = [{"type": "text", "text": "(no output)"}]
        return blocks, bool(_field(result, "is_error", "isError"))

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._stop.set)
        for task in self._tasks:
            try:
                task.result(10)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
