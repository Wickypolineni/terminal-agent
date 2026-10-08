"""The agent engine: a session (tools, policy, memory, provider, history) and the loop.

    run_turn(session, prompt)       one user request, start to finish
      └ agent_loop(...)             stream a response -> run its tool calls -> send results ->
                                    repeat until the model answers without a tool call

The engine is UI- and provider-neutral: TerminalUI, HeadlessUI or ACPUI drive it, and
providers.py talks to OpenRouter or Anthropic.
"""
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from anthropic.tools.memory import BetaLocalFilesystemMemoryTool

import shell as shell_module
from hooks import Hooks
from http_tool import HttpTool
from ledger import Ledger, preview
from mcp_client import MCPManager
from policy import MODES, Policy
from prompts import main_prompt
from providers import ProviderError, make_provider
from registry import ToolCall, TurnCancelled
from sessions import SessionStore
from shell import Shell
from skills import Skills
from tools import FileTools

DATA_DIR = Path(os.environ.get("SYSADMIN_AGENT_DATA", Path.home() / ".local/share/sysadmin-agent"))
CONFIG_DIR = Path.home() / ".config/sysadmin-agent"
CONFIG_FILE = Path(os.environ.get("SYSADMIN_AGENT_CONFIG", CONFIG_DIR / "config.json"))
INSTRUCTIONS_FILE = CONFIG_DIR / "AGENT.md"
MAX_TOOL_ROUNDS = 100
STOP_HOOK_LIMIT = 3
CONTINUE_NUDGES = 2          # times per request the main agent is sent back to unfinished work
ANNOUNCED_STEP = re.compile(r"\b(let me|let's|i'll|i will|i'm going to|i am going to|next,? i|now i)\b", re.I)


def unfinished(todos: list[dict], text: str) -> str:
    """Why a turn that just ended looks unfinished, or "" if it looks done (or is a question to the user)."""
    if text.rstrip().endswith("?"):
        return ""
    open_items = [t.get("content", "") for t in todos if t.get("status") != "completed"]
    if open_items:
        return "your to-do list still has unfinished items: " + "; ".join(open_items[:6])
    last_sentence = re.split(r"(?<=[.!])\s+", text.strip())[-1] if text.strip() else ""
    if ANNOUNCED_STEP.search(last_sentence):
        return f"your last message announces a next step (\"{last_sentence[:120]}\") but you ended your turn"
    return ""

DEFAULT_CONFIG = {
    "provider": "openrouter",                # openrouter | anthropic
    "model": "anthropic/claude-opus-5.5",    # any OpenRouter model id with tool support
    "anthropic_model": "claude-opus-5-5",    # used when provider is "anthropic"
    "effort": "high",                        # reasoning effort for the main agent
    "subagent_effort": "medium",
    "subagent_model": "",                    # e.g. a cheaper model for subagents; "" = same as model
    "subagents": {"max_parallel": 4, "timeout_seconds": 900, "max_rounds": 40},
    "agents": {},                            # named subagent types (also .agent/agents/*.md)
    "max_tokens": 32000,
    "compact_at_tokens": 150000,             # OpenRouter: summarize the conversation past this size
    "stall_seconds": 300,                    # OpenRouter: retry a response that sends nothing for this long
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY",
                   "api_key_file": "~/.config/sysadmin-agent/openrouter.key", "fallback_models": []},
    "mode": "ask",                           # read-only | ask | edits | auto
    "budget_usd_per_request": 5.0,           # 0 = no limit
    "command_timeout_seconds": 300,
    "rules": {"allow": [], "ask": [], "deny": []},
    "hooks": {},
    "credentials": {},
    "mcp_servers": {},
    "shell_env_allow": [],
}


def load_config(path: Path = CONFIG_FILE) -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.is_file():
        for key, value in json.loads(path.read_text()).items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
    return config


def new_session_id() -> str:
    return time.strftime("%Y-%m-%d_%H%M%S") + "-" + uuid.uuid4().hex[:4]


class Session:
    def __init__(self, config: dict, ui, provider=None, client=None, session_id: str | None = None,
                 cwd: str | None = None) -> None:
        from toolset import build_registry
        self.config, self.ui = config, ui
        self.id = session_id or new_session_id()
        self.cancel = threading.Event()
        self.lock = threading.Lock()            # parallel subagents share the cost total
        shell_module.COMMAND_TIMEOUT = int(config["command_timeout_seconds"])
        self.shell = Shell(start_dir=cwd, env_allow=set(config["shell_env_allow"]), cancel=self.cancel)
        self.files = FileTools(self.shell, ui)
        self.policy = Policy(config["mode"], config["rules"])
        self.ledger = Ledger(DATA_DIR, self.id)
        self.store = SessionStore(DATA_DIR)
        self.hooks = Hooks(config["hooks"], self.id)
        self.skills = Skills.discover(self.shell.cwd)
        self.memory = BetaLocalFilesystemMemoryTool(base_path=str(DATA_DIR))
        self.http = HttpTool(config["credentials"])
        self.mcp = MCPManager(config["mcp_servers"])
        self.mcp.start()
        self.provider = provider or make_provider(config, ledger=self.ledger, client=client)
        self._client = client
        self._providers = {self.provider.model: self.provider}
        self.instructions = INSTRUCTIONS_FILE.read_text() if INSTRUCTIONS_FILE.is_file() else ""
        from subagents import SubagentManager
        self.subagents = SubagentManager(self)
        self.registry = build_registry(self, self.shell, self.files)
        self.system_text = self._system_text()
        self.history: list = []
        self.title = ""
        self.cost = 0.0
        self.turn_start_cost = 0.0
        self.todos: list[dict] = []           # the to-do list as the agent last set it this request
        self.pending_notes: list[str] = []
        self.ledger.event("session_start", provider=self.provider.name, model=self.provider.model,
                          mode=self.policy.mode, cwd=self.shell.cwd, tools=sorted(self.registry.tools),
                          mcp_errors=self.mcp.errors)

    def _system_text(self) -> str:
        facts = [f"Model: {self.provider.model} via {self.provider.name}.",
                 f"Permission mode at session start: {self.policy.mode} - {MODES[self.policy.mode]}.",
                 f"Working directory at session start: {self.shell.cwd}.",
                 f"Credentials for http_request: {self.http.describe_credentials()}."]
        if self.mcp.tools:
            facts.append(f"MCP servers connected: {', '.join(sorted({t.server for t in self.mcp.tools}))} "
                         "(their tools are named mcp__<server>__<tool>).")
        if self.skills.skills:
            facts.append("Skills available (load one with the skill tool when a task matches):\n" + self.skills.describe())
        if self.subagents.types:
            facts.append("Subagent types for delegate's `agent` parameter:\n" + self.subagents.describe_types())
        return main_prompt(facts, self.instructions)

    def provider_for(self, model: str):
        """The provider for a model (subagents may use a different, cheaper one)."""
        with self.lock:
            if model not in self._providers:
                key = "anthropic_model" if self.provider.name == "anthropic" else "model"
                self._providers[model] = make_provider({**self.config, key: model}, ledger=self.ledger,
                                                       client=self._client)
            return self._providers[model]

    def resume(self, session_id: str) -> None:
        data = self.store.load(session_id)
        if data["provider"] != self.provider.name:
            raise ValueError(f"session {session_id} was made with {data['provider']}; this run uses {self.provider.name}")
        self.ledger.event("resumed", from_session=session_id)
        self.id, self.title, self.cost = data["id"], data.get("title", ""), float(data.get("cost_usd", 0))
        self.history = self.provider.load(data["history"])
        if data.get("cwd") and os.path.isdir(data["cwd"]):
            self.shell.run(f"cd {json.dumps(data['cwd'])}")

    def set_mode(self, mode: str) -> None:
        self.policy.mode = mode
        self.pending_notes.append(f"The user changed the permission mode to {mode}: {MODES[mode]}.")
        self.ledger.event("mode_change", mode=mode)

    def interrupt(self) -> None:
        """Cancel the running request from another thread (an app's cancel button)."""
        self.cancel.set()
        self.subagents.stop_all("cancelled")
        self.shell.stop()   # kills a running command; the next command starts a fresh shell

    def close(self) -> None:
        self.subagents.close()
        self.shell.stop()
        self.mcp.close()
        self.ledger.event("session_end", cost_usd=round(self.cost, 4))


# ---------------------------------------------------------------------------------------------


def _not_run(calls: list[ToolCall], why: str, done: list | None = None) -> list[dict]:
    """A result for every call, in call order: the ones that ran, and `why` for the rest."""
    finished = {r["id"]: r for r in done or []}
    return [finished.get(c.id) or {"id": c.id, "content": why, "is_error": True} for c in calls]


def agent_loop(session: Session, history: list, *, registry, system_text: str, effort: str,
               cwd, subagent: bool = False, indent: str = "", max_rounds: int = MAX_TOOL_ROUNDS,
               provider=None, cancel: threading.Event | None = None, on_round=None) -> tuple[str, str]:
    """Run until the model answers without a tool call. Returns (final text, stop reason).
    Subagents pass their own provider (model), cancel switch and an on_round progress callback."""
    provider, cancel, ui = provider or session.provider, cancel or session.cancel, session.ui
    tools_payload = provider.render_tools(registry.available(subagent), registry.server_tools)
    budget = float(session.config["budget_usd_per_request"] or 0)
    registry.new_turn()
    last_turn, text, nudges = None, "", 0
    for _ in range(max_rounds):
        if cancel.is_set():
            raise TurnCancelled()
        if provider.needs_compaction(last_turn):
            history[:] = provider.compact(history, system_text, tools_payload, ui, cancel)
        try:
            # Subagents (possibly several at once) don't stream their text: only their tool calls show.
            turn = provider.stream(history, system_text, tools_payload, effort, ui, indent, cancel,
                                   display=not subagent)
        except KeyboardInterrupt:
            raise TurnCancelled() from None   # nothing from this attempt entered the history
        last_turn, text = turn, turn.text
        with session.lock:
            session.cost += turn.cost
            spent = session.cost - session.turn_start_cost   # this request, subagents included
        if on_round:
            on_round(turn)
        ui.usage(turn.usage_line, indent)
        session.ledger.event("model_round", subagent=subagent, model=provider.model, stop=turn.stop,
                             usage=turn.usage, cost_usd=round(turn.cost, 5))

        if turn.stop == "pause":
            continue   # a server-side tool paused; resending resumes it
        if turn.stop == "tool_use":
            if budget and spent > budget:
                history.extend(provider.tool_results(_not_run(turn.tool_calls, "Not run: the request reached its cost budget.")))
                session.ledger.event("budget_stop", cost_usd=round(spent, 4), budget_usd=budget, subagent=subagent)
                ui.notice(f"[stopped: this request has cost ~${spent:.2f}, over the ${budget:.2f} budget - "
                          "say 'continue' to keep going, or raise budget_usd_per_request]", indent)
                return text, "max_turn_requests"
            results: list = []
            try:
                registry.execute_round(turn.tool_calls, cwd, results, subagent, indent + "  ")
            except KeyboardInterrupt:
                # Every tool call needs a result, or the next request is invalid.
                history.extend(provider.tool_results(_not_run(turn.tool_calls, "Cancelled by the user before it ran.", results)))
                session.ledger.event("cancelled", subagent=subagent, during="tools")
                raise TurnCancelled() from None
            notes = [registry.repeat_warning(turn.tool_calls)]
            if not subagent:
                notes.append(session.subagents.collect())   # background subagents that have finished
            history.extend(provider.tool_results(results, "\n\n".join(n for n in notes if n) or None))
            continue

        if turn.tool_calls:   # cut off mid-call: leave no call without a result
            history.extend(provider.tool_results(_not_run(turn.tool_calls, "Not run: the response was cut off.")))
        if not subagent and turn.stop == "end_turn" and session.subagents.running_background():
            # Don't end the turn with work still out: wait for the background subagents, then
            # let the model use their reports.
            history.extend(provider.user(session.subagents.wait_background(ui)))
            continue
        if not subagent and turn.stop == "end_turn" and nudges < CONTINUE_NUDGES:
            # Some models stop mid-task ("Let me now write the report.") - send them back once or twice.
            if why := unfinished(session.todos, text):
                nudges += 1
                session.ledger.event("continue_nudge", reason=why[:300], nudge=nudges)
                ui.notice(f"[the agent stopped early - {why[:100]}; asking it to continue]", indent)
                history.extend(provider.user(
                    f"[agent runtime] You ended your turn, but {why}. If the task isn't finished, continue now "
                    "with your tools. If it is finished, or you're blocked, update the to-do list to match and "
                    "give the user your final answer."))
                continue
        if turn.stop == "refusal":
            ui.notice(f"[the model declined this request{f' ({turn.detail})' if turn.detail else ''}]", indent)
        elif turn.stop == "max_tokens":
            ui.notice("[the response hit its length limit and was cut off - ask it to continue]", indent)
        return text, turn.stop

    ui.notice(f"[stopped after {max_rounds} tool rounds - ask it to continue if it was on track]", indent)
    return text, "max_turn_requests"


def run_turn(session: Session, content) -> tuple[str, str]:
    """One user request. `content` is a string or neutral blocks (text/image).
    Returns (final text, stop reason: end_turn | max_tokens | max_turn_requests | refusal | cancelled | blocked)."""
    session.cancel.clear()
    session.turn_start_cost = session.cost
    session.todos = []
    provider, hooks = session.provider, session.hooks
    prompt_text = content if isinstance(content, str) else " ".join(b.get("text", "") for b in content if b.get("type") == "text")
    if hooks.active("UserPromptSubmit"):
        hook = hooks.run("UserPromptSubmit", {"prompt": prompt_text}, cwd=session.shell.cwd)
        if hook.decision == "block":
            session.ui.notice(f"[blocked by a UserPromptSubmit hook: {hook.reason}]")
            return "", "blocked"
        if hook.context:
            extra = f"\n\n[context from a hook]\n{hook.context}"
            content = content + extra if isinstance(content, str) else [*content, {"type": "text", "text": extra}]
    session.title = session.title or prompt_text[:80]
    session.ledger.event("user_message", text=preview(prompt_text))
    session.history.extend(provider.user(content))
    if session.pending_notes:
        session.history.extend(provider.system_note(" ".join(session.pending_notes)))
        session.pending_notes.clear()
    try:
        for _ in range(STOP_HOOK_LIMIT + 1):
            text, stop = agent_loop(session, session.history, registry=session.registry,
                                    system_text=session.system_text, effort=session.config["effort"],
                                    cwd=lambda: session.shell.cwd)
            if stop == "end_turn" and hooks.active("Stop"):
                hook = hooks.run("Stop", {"last_message": text}, cwd=session.shell.cwd)
                if hook.decision == "block":   # the hook says the work isn't finished
                    session.ui.notice(f"[a Stop hook asked for more work: {hook.reason}]")
                    session.history.extend(provider.user(f"[Stop hook] Not finished yet: {hook.reason}"))
                    continue
            return text, stop
        return text, stop
    except TurnCancelled:
        session.ledger.event("cancelled", during="turn")
        return "", "cancelled"
    finally:
        session.store.save(session)
