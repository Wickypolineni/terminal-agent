"""Agent Client Protocol (ACP) server: how apps drive this agent.

ACP (agentclientprotocol.com) is JSON-RPC over stdio, also spoken by Zed, Hermes Agent and
OpenClaw. An app (Reflick, an editor, ...) starts `agent.py --acp` as a child process and:
  initialize -> session/new (or session/load) -> session/prompt ... session/cancel, session/set_mode
and receives session/update notifications: streamed answer text, progress ("thoughts"), tool calls
with their status and output, and plan (to-do) updates. When a tool call needs approval the agent
sends session/request_permission and waits for the app's choice.

Nothing may print to stdout in this mode: stdout is the protocol channel.
"""
import asyncio
import json

import acp
from acp import schema

from engine import DATA_DIR, Session, run_turn
from policy import MODES
from registry import content_text
from sessions import SessionStore
from ui import UI

VERSION = "0.3.0"
STOP_REASONS = {"end_turn": "end_turn", "max_tokens": "max_tokens", "max_turn_requests": "max_turn_requests",
                "refusal": "refusal", "cancelled": "cancelled", "blocked": "refusal"}


class ACPUI(UI):
    """Turns engine events into ACP notifications. Runs on the engine's worker thread and hands
    each message to the asyncio loop, waiting for it so updates arrive in order."""
    interactive = True

    def __init__(self, conn, loop) -> None:
        self.conn, self.loop = conn, loop
        self.session_id = ""
        self.announced: set[str] = set()

    def _call(self, coroutine, timeout: float | None = 30):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    def _send(self, update) -> None:
        self._call(self.conn.session_update(session_id=self.session_id, update=update))

    def text(self, delta, indent=""):
        # Subagent text is working-out, not the answer: show it as a thought.
        self._send(acp.update_agent_thought_text(delta) if indent else acp.update_agent_message_text(delta))

    def thought(self, delta, indent=""):
        self._send(acp.update_agent_thought_text(delta))

    def notice(self, message, indent=""):
        self._send(acp.update_agent_thought_text(f"\n{message.strip()}\n"))

    def tool_start(self, call_id, title, kind, indent="", note=""):
        if call_id in self.announced:
            self._send(acp.update_tool_call(call_id, title=title, kind=kind, status="in_progress"))
        else:
            self.announced.add(call_id)
            self._send(acp.start_tool_call(call_id, title, kind=kind, status="in_progress"))

    def tool_end(self, call_id, content, is_error):
        if call_id not in self.announced:
            self.announced.add(call_id)
            self._send(acp.start_tool_call(call_id, call_id, status="pending"))
        self._send(acp.update_tool_call(call_id, status="failed" if is_error else "completed",
                                        content=[acp.tool_content(acp.text_block(content_text(content)[:20_000] or "(done)"))]))

    def approve(self, call_id, title, reason, grantable, grant_label):
        first, _, rest = title.partition("\n")
        if call_id not in self.announced:
            self.announced.add(call_id)
            self._send(acp.start_tool_call(call_id, first, status="pending"))
        options = [schema.PermissionOption(option_id="allow", name="Allow", kind="allow_once")]
        if grantable:
            options.append(schema.PermissionOption(option_id="allow_all", name=f"Allow all {grant_label} this session",
                                                   kind="allow_always"))
        options.append(schema.PermissionOption(option_id="reject", name="Reject", kind="reject_once"))
        details = (rest.strip() + "\n\n" if rest.strip() else "") + f"Asking because: {reason}"
        tool_call = schema.ToolCallUpdate(tool_call_id=call_id, title=first, status="pending",
                                          content=[acp.tool_content(acp.text_block(details))])
        response = self._call(self.conn.request_permission(session_id=self.session_id, tool_call=tool_call,
                                                           options=options), timeout=None)
        outcome = response.outcome
        if getattr(outcome, "outcome", "") != "selected":
            return False, "The request was cancelled.", False
        if outcome.option_id == "reject":
            return False, "", False
        return True, "", outcome.option_id == "allow_all"

    def todos(self, todos):
        self._send(acp.update_plan([acp.plan_entry(t["content"], status=t["status"]) for t in todos]))


def _content(prompt: list):
    """ACP prompt blocks -> the engine's neutral content."""
    blocks = []
    for block in prompt:
        kind = getattr(block, "type", "")
        if kind == "text":
            blocks.append({"type": "text", "text": block.text})
        elif kind == "image" and block.data:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": block.mime_type, "data": block.data}})
        elif kind == "resource_link":
            blocks.append({"type": "text", "text": f"[attached: {block.name} - {block.uri}]"})
        elif kind == "resource":
            resource = block.resource
            body = getattr(resource, "text", None) or "(binary content)"
            blocks.append({"type": "text", "text": f"[attached: {resource.uri}]\n{body}"})
    if len(blocks) == 1 and blocks[0]["type"] == "text":
        return blocks[0]["text"]
    return blocks


class UnifiedAgent(acp.Agent):
    def __init__(self, config: dict) -> None:
        self.config = config
        self.sessions: dict[str, Session] = {}
        self.conn = None
        self.loop = None

    def on_connect(self, conn) -> None:
        self.conn = conn

    async def initialize(self, protocol_version, client_capabilities=None, client_info=None, **kwargs):
        self.loop = asyncio.get_running_loop()
        return schema.InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_capabilities=schema.AgentCapabilities(
                load_session=True, prompt_capabilities=schema.PromptCapabilities(image=True, embedded_context=True),
                session_capabilities=schema.SessionCapabilities(list=schema.SessionListCapabilities(),
                                                                close=schema.SessionCloseCapabilities())),
            agent_info=schema.Implementation(name="sysadmin-agent", title="Unified agent", version=VERSION))

    async def authenticate(self, method_id, **kwargs):
        return None

    def _modes(self, session: Session):
        return schema.SessionModeState(
            current_mode_id=session.policy.mode,
            available_modes=[schema.SessionMode(id=m, name=m, description=d) for m, d in MODES.items()])

    def _make(self, cwd: str, resume_id: str | None = None) -> Session:
        ui = ACPUI(self.conn, self.loop)
        session = Session(self.config, ui, cwd=cwd or None)
        if resume_id:
            session.resume(resume_id)
        ui.session_id = session.id
        self.sessions[session.id] = session
        return session

    async def new_session(self, cwd, additional_directories=None, mcp_servers=None, **kwargs):
        session = await asyncio.to_thread(self._make, cwd)
        return schema.NewSessionResponse(session_id=session.id, modes=self._modes(session))

    async def load_session(self, cwd, session_id, mcp_servers=None, additional_directories=None, **kwargs):
        session = await asyncio.to_thread(self._make, cwd, session_id)
        for message in session.history:   # replay the conversation so the app can show it
            text = message.get("content") if isinstance(message, dict) else None
            if isinstance(text, list):
                text = " ".join(b.get("text", "") for b in text if isinstance(b, dict) and b.get("type") == "text")
            if not text:
                continue
            if message.get("role") == "user":
                await self.conn.session_update(session_id=session.id, update=acp.update_user_message_text(text))
            elif message.get("role") == "assistant":
                await self.conn.session_update(session_id=session.id, update=acp.update_agent_message_text(text))
        return schema.LoadSessionResponse(modes=self._modes(session))

    async def list_sessions(self, cwd=None, cursor=None, **kwargs):
        items = SessionStore(DATA_DIR).list()
        return schema.ListSessionsResponse(sessions=[
            schema.SessionInfo(session_id=s["id"], cwd=s.get("cwd") or "", title=s.get("title") or None,
                               updated_at=s.get("updated") or None)
            for s in items if not cwd or s.get("cwd") == cwd][:100])

    async def prompt(self, session_id, prompt, **kwargs):
        session = self.sessions[session_id]
        _, stop = await asyncio.to_thread(run_turn, session, _content(prompt))
        return schema.PromptResponse(stop_reason=STOP_REASONS.get(stop, "end_turn"))

    async def cancel(self, session_id, **kwargs):
        if session := self.sessions.get(session_id):
            session.interrupt()

    async def set_session_mode(self, session_id, mode_id, **kwargs):
        if mode_id not in MODES:
            raise acp.RequestError.invalid_params({"mode_id": f"unknown mode {mode_id}"})
        self.sessions[session_id].set_mode(mode_id)
        return schema.SetSessionModeResponse()

    async def close_session(self, session_id, **kwargs):
        if session := self.sessions.pop(session_id, None):
            await asyncio.to_thread(session.close)
        return None


def serve(config: dict) -> None:
    agent = UnifiedAgent(config)
    try:
        asyncio.run(acp.run_agent(agent, use_unstable_protocol=True))   # session/close is still "unstable"
    finally:
        for session in agent.sessions.values():
            session.close()
