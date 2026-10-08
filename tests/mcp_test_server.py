"""A tiny MCP server used by test_mcp.py (works with the mcp package v1 or v2)."""
from mcp.types import ToolAnnotations

try:
    from mcp.server.mcpserver import MCPServer as Server   # mcp v2
except ImportError:
    from mcp.server.fastmcp import FastMCP as Server       # mcp v1


def hints(**kw) -> ToolAnnotations:
    try:
        return ToolAnnotations(**kw)                                     # v2: snake_case
    except Exception:
        camel = {"read_only_hint": "readOnlyHint", "destructive_hint": "destructiveHint"}
        return ToolAnnotations(**{camel[k]: v for k, v in kw.items()})   # v1: camelCase


server = Server("test")


@server.tool(annotations=hints(read_only_hint=True))
def echo(text: str) -> str:
    """Return the text unchanged."""
    return f"echo: {text}"


@server.tool(annotations=hints(destructive_hint=True))
def wipe(target: str) -> str:
    """Pretend to delete something."""
    return f"wiped {target}"


@server.tool()
def fail(reason: str) -> str:
    """Always fails."""
    raise ValueError(reason)


if __name__ == "__main__":
    server.run()
