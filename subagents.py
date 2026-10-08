"""Subagents: child agents the main agent hands tasks to (the `delegate` and `subagents` tools).

Modelled on the Claude Agent SDK (named agent types, background runs, SubagentStart/Stop hooks),
Hermes Agent (concurrency cap, timeouts, structured results, children can't ask/remember/delegate)
and OpenClaw (list / stop / wait, results with a status line).

    delegate(task, name, agent, can_change, background)
      blocking (default)  the call returns the child's report. Several delegate calls in one
                          response run at the same time (registry.py batches them).
      background          returns at once; the report reaches the main agent with its next tool
                          results, or when it finishes its turn (the turn waits for running children).

Each child: its own bash shell, the same or a cheaper model, its own cancel switch and time limit.
Research children may only take actions that need no approval; worker children (can_change) may
change things, with each approval going to the user labelled with the child's name. Children can't
delegate further, ask the user, write memory or edit the to-do list. They see only their task (plus
the agent type's instructions and the user's standing instructions), never the conversation.

Agent types: markdown files with frontmatter in ./.agent/agents/ or ~/.config/sysadmin-agent/agents/
    ---
    name: reviewer
    description: Reviews a diff for bugs
    model: anthropic/claude-sonnet-5.5     (optional; else subagent_model, else the main model)
    effort: medium                          (optional)
    can_change: false                       (optional)
    ---
    <instructions for this kind of subagent>
or the same fields under "agents" in config.json ("prompt" for the instructions).
"""
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

from ledger import preview
from registry import TurnCancelled
from shell import Shell
from skills import _frontmatter
from tools import FileTools

MAX_ROUNDS = 40
REPORT_LIMIT = 24_000          # longer reports are cut, with the full text saved to a file
USER_AGENTS = Path.home() / ".config/sysadmin-agent/agents"


@dataclass
class AgentType:
    name: str
    description: str = ""
    prompt: str = ""
    model: str = ""
    effort: str = ""
    can_change: bool = False


def load_agent_types(config: dict, cwd: str) -> dict[str, AgentType]:
    types: dict[str, AgentType] = {}
    for name, spec in (config.get("agents") or {}).items():
        types[name] = AgentType(name, spec.get("description", ""), spec.get("prompt", ""), spec.get("model", ""),
                                spec.get("effort", ""), bool(spec.get("can_change", False)))
    for root in (Path(cwd) / ".agent/agents", USER_AGENTS):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.md")):
            meta, body = _frontmatter(path.read_text(errors="replace"))
            name = meta.get("name") or path.stem
            types.setdefault(name, AgentType(name, meta.get("description", ""), body.strip(), meta.get("model", ""),
                                             meta.get("effort", ""), meta.get("can_change", "").lower() == "true"))
    return types


@dataclass
class Child:
    id: str
    name: str
    task: str
    agent: str
    can_change: bool
    background: bool
    model: str
    cancel: threading.Event = field(default_factory=threading.Event)
    shell: Shell | None = None
    status: str = "queued"        # queued | running | completed | max_rounds | timeout | stopped | cancelled | error
    started: float = field(default_factory=time.monotonic)
    rounds: int = 0
    tool_calls: int = 0
    last_tool: str = ""
    cost: float = 0.0
    text: str = ""                # the latest text it wrote (its partial report if it's stopped)
    result: str = ""
    future: Future | None = None
    delivered: bool = False
    stop_reason: str = ""         # why it was stopped early: "timeout" | "stopped" | "cancelled"

    def stop(self, reason: str = "stopped") -> None:
        self.stop_reason = self.stop_reason or reason
        self.cancel.set()
        if self.shell:
            self.shell.stop()

    def elapsed(self) -> float:
        return time.monotonic() - self.started


class SubagentManager:
    def __init__(self, session) -> None:
        self.session = session
        settings = session.config.get("subagents") or {}
        self.max_parallel = int(settings.get("max_parallel", 4))
        self.timeout = float(settings.get("timeout_seconds", 900))
        self.max_rounds = int(settings.get("max_rounds", MAX_ROUNDS))
        self.slots = threading.Semaphore(self.max_parallel)
        self.children: dict[str, Child] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="subagent")
        self.types = load_agent_types(session.config, session.shell.cwd)

    # -- what the model sees ---------------------------------------------------------------------

    def describe_types(self) -> str:
        return "\n".join(f"- {t.name}: {t.description}" + (" (can change things)" if t.can_change else "")
                         for t in self.types.values())

    def delegate(self, task: str, name: str = "", agent: str = "", can_change: bool | None = None,
                 background: bool = False) -> tuple[str, bool]:
        session = self.session
        kind = self.types.get(agent) if agent else None
        if agent and not kind:
            return f"No subagent type {agent!r}. Available: {', '.join(self.types) or 'none'}.", True
        if session.cancel.is_set():
            raise TurnCancelled()
        if session.hooks.active("SubagentStart"):
            hook = session.hooks.run("SubagentStart", {"task": task, "name": name, "agent_type": agent},
                                     cwd=session.shell.cwd)
            if hook.decision == "block":
                return f"Not started: a SubagentStart hook blocked it: {hook.reason}", True
            if hook.context:
                task += f"\n\n[context from a hook]\n{hook.context}"
        change = kind.can_change if can_change is None and kind else bool(can_change)
        model = (kind.model if kind and kind.model else "") or session.config.get("subagent_model") or ""
        with self.lock:
            short = uuid.uuid4().hex[:4]
            base = (name or agent or "sub").strip()[:30]
            child = Child(f"{base}-{short}", base, task, agent, change, background, model or session.provider.model)
            self.children[child.id] = child
        if background:
            child.future = self.pool.submit(self._run, child, kind)
            return (f"Started subagent [{child.name}] (id {child.id}) in the background. Its report will arrive "
                    "automatically with a later tool result, or before you finish this turn. Carry on with "
                    "other work; use the subagents tool to list, wait for, or stop it."), False
        report = self._run(child, kind)
        child.delivered = True
        return report, child.status not in ("completed",)

    # -- running a child -------------------------------------------------------------------------

    def _run(self, child: Child, kind: AgentType | None) -> str:
        from prompts import subagent_prompt
        from toolset import build_registry
        from engine import agent_loop
        session = self.session
        while not self.slots.acquire(timeout=0.2):          # wait for a free slot, but stay cancellable
            if child.cancel.is_set() or session.cancel.is_set():
                child.status = "cancelled"
                return self._finish(child)
        timer = threading.Timer(self.timeout, child.stop, ("timeout",))
        timer.daemon = True
        try:
            child.started = time.monotonic()
            child.status = "running"
            child.shell = Shell(start_dir=session.shell.cwd, env_allow=set(session.config["shell_env_allow"]),
                                cancel=child.cancel)
            if session.cancel.is_set():
                child.stop("cancelled")
            registry = build_registry(session, child.shell, FileTools(child.shell, session.ui), subagent=True,
                                      worker=child.can_change, label=child.name, cancel=child.cancel,
                                      interrupt=child.stop)
            provider = session.provider_for(child.model)
            session.ledger.event("subagent_start", id=child.id, name=child.name, agent=child.agent,
                                 model=child.model, can_change=child.can_change, background=child.background,
                                 task=preview(child.task))
            session.ui.notice(f"↳ subagent [{child.name}] started{' in the background' if child.background else ''}"
                              f"{f' ({child.model})' if child.model != session.provider.model else ''}", "  ")
            timer.start()

            def on_round(turn) -> None:
                child.rounds += 1
                child.cost += turn.cost
                child.text = turn.text or child.text
                child.tool_calls += len(turn.tool_calls)
                if turn.tool_calls:
                    child.last_tool = turn.tool_calls[-1].name

            effort = (kind.effort if kind and kind.effort else "") or session.config["subagent_effort"]
            system = subagent_prompt(child.can_change, kind.prompt if kind else "", session.instructions)
            try:
                text, stop = agent_loop(session, provider.user(child.task), registry=registry, system_text=system,
                                        effort=effort, cwd=lambda: child.shell.cwd, subagent=True, indent="    ",
                                        max_rounds=self.max_rounds, provider=provider, cancel=child.cancel,
                                        on_round=on_round)
                child.text = text or child.text
                child.status = {"end_turn": "completed", "max_turn_requests": "max_rounds"}.get(stop, stop)
            except TurnCancelled:
                child.status = child.stop_reason or "cancelled"
            except Exception as e:   # a provider error ends this child, not the session
                child.status = "error"
                child.text = f"{child.text}\n\n[error: {type(e).__name__}: {e}]".strip()
        finally:
            timer.cancel()
            if child.shell:
                child.shell.stop()
            self.slots.release()
        return self._finish(child)

    def _finish(self, child: Child) -> str:
        session = self.session
        report = child.text.strip() or "(no report)"
        if len(report) > REPORT_LIMIT:
            folder = Path(session.ledger.dir).parent / "subagents" / session.id
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{child.id}.md"
            path.write_text(report)
            half = REPORT_LIMIT // 2
            report = (f"{report[:half]}\n\n... [report cut: {len(report):,} characters - full text in {path}] ...\n\n"
                      f"{report[-half:]}")
        if session.hooks.active("SubagentStop"):
            hook = session.hooks.run("SubagentStop", {"name": child.name, "agent_type": child.agent,
                                                      "status": child.status, "report": report},
                                     cwd=session.shell.cwd)
            if hook.context:
                report += f"\n\n[hook] {hook.context}"
        notes = {"max_rounds": " - hit its round limit; the report may be incomplete",
                 "timeout": f" - stopped after {self.timeout:.0f}s; partial report",
                 "stopped": " - stopped early; partial report", "cancelled": " - cancelled; partial report",
                 "error": " - failed"}
        child.result = (f"[subagent {child.name} · {child.status}{notes.get(child.status, '')} · {child.rounds} rounds · "
                        f"{child.tool_calls} tool calls · {child.elapsed():.0f}s · ${child.cost:.3f}]\n{report}")
        session.ledger.event("subagent_end", id=child.id, name=child.name, status=child.status, rounds=child.rounds,
                             tool_calls=child.tool_calls, seconds=round(child.elapsed(), 1), cost_usd=round(child.cost, 4))
        session.ui.notice(f"↳ subagent [{child.name}] {child.status} in {child.elapsed():.0f}s", "  ")
        return child.result

    # -- background bookkeeping ------------------------------------------------------------------

    def _background(self, pending_only: bool = False) -> list[Child]:
        with self.lock:
            return [c for c in self.children.values() if c.background and not c.delivered
                    and (not pending_only or not (c.future and c.future.done()))]

    def collect(self) -> str | None:
        """Reports of background children that finished since the last delivery."""
        done = [c for c in self._background() if c.future and c.future.done()]
        for c in done:
            c.delivered = True
        if not done:
            return None
        return "Background subagent reports:\n\n" + "\n\n".join(c.future.result() for c in done)

    def running_background(self) -> bool:
        return bool(self._background())

    def wait_background(self, ui) -> str:
        """Block until every undelivered background child is done (the turn is ending)."""
        futures = [c.future for c in self._background() if c.future]
        ui.notice(f"[waiting for {len(futures)} background subagent(s) to finish]")
        try:
            ui.serve_while(futures)
        except KeyboardInterrupt:
            self.stop_all("cancelled")
            ui.cancel_pending()
            raise TurnCancelled() from None
        return self.collect() or "Background subagents finished with no report."

    def stop_all(self, reason: str = "cancelled") -> None:
        with self.lock:
            children = list(self.children.values())
        for c in children:
            if c.status in ("queued", "running"):
                c.stop(reason)

    def active(self) -> list[Child]:
        with self.lock:
            return [c for c in self.children.values() if c.status in ("queued", "running")]

    # -- the `subagents` tool ----------------------------------------------------------------------

    def manage(self, action: str, ids: list[str] | None = None) -> tuple[str, bool]:
        with self.lock:
            children = list(self.children.values())
        if action == "list":
            if not children:
                return "No subagents in this session.", False
            return "\n".join(
                f"{c.id}  [{c.name}]  {c.status}{' (background)' if c.background else ''}  {c.elapsed():.0f}s  "
                f"{c.rounds} rounds, {c.tool_calls} tool calls{f', last: {c.last_tool}' if c.last_tool else ''}  "
                f"${c.cost:.3f}  - {c.task.strip().splitlines()[0][:80]}" for c in children), False
        picked = [c for c in children if not ids or c.id in ids or c.name in ids]
        if ids and not picked:
            return f"No subagent matching {ids}. Use action list to see them.", True
        if action == "stop":
            running = [c for c in picked if c.status in ("queued", "running")]
            for c in running:
                c.stop("stopped")
            return (f"Stopping {', '.join(c.id for c in running)}; partial reports will follow." if running
                    else "None of those are running."), False
        if action == "wait":
            pending = [c for c in picked if c.background and not c.delivered and c.future]
            if not pending:
                return "Nothing to wait for (no undelivered background subagents).", False
            try:
                self.session.ui.serve_while([c.future for c in pending])
            except KeyboardInterrupt:
                self.stop_all("cancelled")
                self.session.ui.cancel_pending()
                raise TurnCancelled() from None
            for c in pending:
                c.delivered = True
            return "\n\n".join(c.future.result() for c in pending), False
        return f"Unknown action {action!r}: use list, wait or stop.", True

    def close(self) -> None:
        self.stop_all("cancelled")
        self.pool.shutdown(wait=False, cancel_futures=True)
