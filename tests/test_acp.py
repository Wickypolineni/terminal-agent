"""The Agent Client Protocol server, driven the way an app (Reflick, Zed) drives it: spawn
`agent.py --acp` as a child process and talk JSON-RPC over its stdio. The model is the fake OpenRouter."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import acp
from acp import schema

from harness import ROOT, check, done
from fake_openrouter import FakeOpenRouter, text_reply, tool_reply

work = Path(tempfile.mkdtemp())
fake = FakeOpenRouter()
cfg_file = work / "config.json"
cfg_file.write_text(json.dumps({"openrouter": {"base_url": fake.url}}))
env = {**os.environ, "SYSADMIN_AGENT_CONFIG": str(cfg_file), "SYSADMIN_AGENT_DATA": str(work / "data"),
       "OPENROUTER_API_KEY": "test-key"}


def reset(*script):
    fake.requests.clear()
    fake.script[:] = list(script)


class App:
    """A minimal ACP client: records updates, answers permission requests from a script."""

    def __init__(self):
        self.updates, self.permissions, self.answers = [], [], []
        self.conn = None

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update)

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        self.permissions.append((tool_call, [o.option_id for o in options]))
        answer = self.answers.pop(0) if self.answers else "reject"
        if answer == "cancel":
            await self.conn.cancel(session_id=session_id)
            return schema.RequestPermissionResponse(outcome=schema.DeniedOutcome(outcome="cancelled"))
        return schema.RequestPermissionResponse(outcome=schema.AllowedOutcome(outcome="selected", option_id=answer))

    def message_text(self):
        return "".join(u.content.text for u in self.updates if u.session_update == "agent_message_chunk")

    def of(self, kind):
        return [u for u in self.updates if u.session_update == kind]


async def main():
    app = App()
    async with acp.spawn_agent_process(app, sys.executable, str(ROOT / "agent.py"), "--acp", env=env, cwd=work) as (conn, proc):
        app.conn = conn
        init = await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
        check("1. initialize: protocol + load_session + images",
              init.protocol_version == acp.PROTOCOL_VERSION and init.agent_capabilities.load_session
              and init.agent_capabilities.prompt_capabilities.image and init.agent_info.name == "sysadmin-agent")
        new = await conn.new_session(cwd=str(work))
        sid = new.session_id
        check("1. new session advertises the permission modes",
              new.modes.current_mode_id == "ask" and {m.id for m in new.modes.available_modes} == {"read-only", "ask", "edits", "auto"})

        # 2. a turn with a command needing approval, a plan update and streamed text
        reset(tool_reply([("a1", "bash", {"command": "make build"}),
                          ("a2", "update_todos", {"todos": [{"content": "Build", "status": "in_progress"}]})],
                         reasoning="Build first."),
              text_reply("Build attempted. Should I retry it?"))
        app.answers[:] = ["allow"]
        response = await conn.prompt(session_id=sid, prompt=[acp.text_block("build the project")])
        check("2. prompt ends with end_turn", response.stop_reason == "end_turn", response)
        check("2. answer streamed as agent_message_chunk", app.message_text() == "Build attempted. Should I retry it?", app.message_text())
        thoughts = "".join(u.content.text for u in app.of("agent_thought_chunk"))
        check("2. reasoning streamed as agent_thought_chunk", "Build first" in thoughts, [u.session_update for u in app.updates])
        perm = app.permissions[0] if app.permissions else (None, [])
        check("2. permission requested for the command with allow/reject options",
              perm[0] is not None and perm[0].tool_call_id == "a1" and "make build" in perm[0].title
              and perm[1][0] == "allow" and perm[1][-1] == "reject", perm)
        starts = {u.tool_call_id: u for u in app.of("tool_call")}
        check("2. tool call announced once, with its kind", "a1" in starts and len([u for u in app.of("tool_call") if u.tool_call_id == "a1"]) == 1)
        ends = [u for u in app.of("tool_call_update") if u.tool_call_id == "a1" and u.status in ("completed", "failed")]
        check("2. tool call finished with its output", ends and ends[-1].content and "make" in ends[-1].content[0].content.text, ends)
        plans = app.of("plan")
        check("2. to-dos sent as a plan", plans and plans[-1].entries[0].content == "Build" and plans[-1].entries[0].status == "in_progress")
        tool_result = [m for m in fake.bodies()[1]["messages"] if m["role"] == "tool"][0]
        check("2. the approved command really ran", "make" in json.dumps(tool_result))

        # 3. read-only mode: writes blocked without asking
        await conn.set_session_mode(session_id=sid, mode_id="read-only")
        app.permissions.clear()
        reset(tool_reply([("b1", "bash", {"command": f"touch {work}/x"})]), text_reply("Could not write."))
        await conn.prompt(session_id=sid, prompt=[acp.text_block("make a file")])
        check("3. read-only mode blocks writes without a permission request", not app.permissions and not (work / "x").exists())
        check("3. the mode change reached the model as a note", any("read-only" in json.dumps(m) for m in fake.bodies()[0]["messages"] if m["role"] == "system"))

        # 4. cancel from the app while a permission request is open
        await conn.set_session_mode(session_id=sid, mode_id="ask")
        reset(tool_reply([("c1", "bash", {"command": "make deploy"})]), text_reply("should not happen"))
        app.answers[:] = ["cancel"]
        response = await conn.prompt(session_id=sid, prompt=[acp.text_block("deploy")])
        check("4. cancel -> stop_reason cancelled, no further model request", response.stop_reason == "cancelled" and len(fake.requests) == 1, response)

        # 5. the session keeps working after a cancel
        app.updates.clear()
        reset(text_reply("Still here."))
        response = await conn.prompt(session_id=sid, prompt=[acp.text_block("hello?")])
        check("5. next prompt works after a cancel", response.stop_reason == "end_turn" and app.message_text() == "Still here.")

        # 6. sessions are listed and can be loaded (history replayed to the app)
        listed = await conn.list_sessions(cwd=str(work))
        check("6. session listed with its title", any(s.session_id == sid and s.title == "build the project" for s in listed.sessions), listed)
        await conn.close_session(session_id=sid)
        app.updates.clear()
        await conn.load_session(cwd=str(work), session_id=sid)
        replayed = [u.session_update for u in app.updates]
        check("6. load_session replays user and agent messages",
              "user_message_chunk" in replayed and "Still here." in app.message_text(), replayed)
    return proc.returncode


asyncio.run(main())
fake.close()
done()
