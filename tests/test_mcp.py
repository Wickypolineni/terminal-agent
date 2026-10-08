"""MCP: connect to a real stdio server, list tools with annotations, call them, shut down."""
import sys
from pathlib import Path

from harness import check, done
from mcp_client import MCPManager

server_script = str(Path(__file__).with_name("mcp_test_server.py"))
manager = MCPManager({
    "test": {"command": sys.executable, "args": [server_script]},
    "broken": {"command": "/nonexistent/mcp-server"},
})
manager.start()

names = {t.api_name: t for t in manager.tools}
check("tools discovered and namespaced", {"mcp__test__echo", "mcp__test__wipe", "mcp__test__fail"} <= set(names), names)
check("a broken server is reported, not fatal", "broken" in manager.errors, manager.errors)
check("readOnlyHint annotation carried through", names["mcp__test__echo"].hints.get("readOnlyHint") is True)
check("destructiveHint annotation carried through", names["mcp__test__wipe"].hints.get("destructiveHint") is True)
check("input schema carried through", names["mcp__test__echo"].input_schema.get("required") == ["text"])

out, err = manager.call(names["mcp__test__echo"], {"text": "hi"})
check("call returns content blocks", not err and out[0]["type"] == "text" and "echo: hi" in out[0]["text"], out)
out, err = manager.call(names["mcp__test__fail"], {"reason": "boom"})
check("a failing tool comes back flagged as an error", err and "Error" in str(out), out)

manager.close()
check("shuts down cleanly", not manager._thread.is_alive())
done()
