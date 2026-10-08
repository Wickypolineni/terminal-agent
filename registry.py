"""The tool registry: one place that knows every tool, and one path every tool call takes.

    validate input -> policy -> PreToolUse hooks -> approval (if needed) -> run
                   -> PostToolUse hooks -> UI + ledger

Tools are described once, provider-neutrally (name, description, JSON schema); each LLM provider
renders them in its own wire format (providers.py). Adding a tool is one Registry.add(Tool(...)).
"""
import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from hooks import Hooks
from ledger import Ledger, preview
from policy import Call, Decision, Policy
from tools import declined, validate

REPEAT_LIMIT = 3   # identical failing calls before Claude is told to change approach


class TurnCancelled(KeyboardInterrupt):
    """The user cancelled the current request (Ctrl+C, or an app's cancel)."""


@dataclass
class ToolCall:
    """A tool call from the model, in provider-neutral form."""
    id: str
    name: str
    input: Any


@dataclass
class Context:
    """Passed to every tool's run function."""
    cwd: str
    subagent: bool
    call_id: str
    confirm: Callable[[str], tuple[bool, str]]   # for tools that show their own preview (diffs)
    on_output: Callable[[str], None]             # live output lines (shell)


@dataclass
class Tool:
    name: str
    description: str
    schema: dict                                           # JSON schema of the input
    run: Callable[[dict, Context], tuple[Any, bool]]       # -> (content, is_error)
    risk: str                                              # policy class: read/write/exec/network/mcp/memory/meta
    native: str = ""                                       # a provider-native equivalent: "bash" | "memory"
    acp_kind: str = "other"                                # how apps should show it: read/edit/execute/search/fetch/think/other
    describe: Callable[[dict, str], Call] | None = None    # (input, cwd) -> what policy evaluates
    summary: Callable[[dict], str] | None = None           # one line shown when it runs / at the prompt
    confirms_itself: bool = False                          # shows its own preview (file diffs)
    subagent: bool = False                                 # available to subagents
    parallel_safe: bool = False                            # may run concurrently with other calls


@dataclass
class ServerTool:
    """Run by the model provider itself (web search/fetch); no client code."""
    name: str          # web_search | web_fetch


def content_chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    return sum(len(b.get("text", "")) for b in content if isinstance(b, dict))


def content_text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


class Registry:
    def __init__(self, policy: Policy, ledger: Ledger, ui, hooks: Hooks | None = None,
                 cancel: threading.Event | None = None, interrupt: Callable[[], None] | None = None,
                 label: str = "", subagent_approvals: bool = False) -> None:
        self.policy = policy
        self.ledger = ledger
        self.ui = ui
        self.hooks = hooks or Hooks({})
        self.cancel = cancel or threading.Event()
        self.interrupt = interrupt or self.cancel.set      # stops running work (kills shells)
        self.label = label                                 # e.g. a subagent's name, shown on its calls
        self.subagent_approvals = subagent_approvals       # worker subagent: approvals go to the user
        self.tools: dict[str, Tool] = {}
        self.server_tools: list[ServerTool] = []
        self.failures: Counter = Counter()

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def add_server_tool(self, name: str) -> None:
        self.server_tools.append(ServerTool(name))

    def available(self, subagent: bool = False) -> list[Tool]:
        return [t for t in self.tools.values() if t.subagent or not subagent]

    def new_turn(self) -> None:
        self.failures.clear()

    # -- one call ------------------------------------------------------------------------------

    def _evaluate(self, call: ToolCall, cwd: str, subagent: bool):
        tool = self.tools.get(call.name)
        if tool is None or (subagent and not tool.subagent):
            return tool, None, None, f"Unknown tool: {call.name}"
        if problem := validate(call.input, tool.schema):
            return tool, None, None, json.dumps({"INVALID_JSON": json.dumps(call.input, default=str), "problem": problem})
        policy_call = tool.describe(call.input, cwd) if tool.describe else Call(tool.name, tool.risk)
        return tool, policy_call, self.policy.evaluate(policy_call, subagent and not self.subagent_approvals), None

    def _title(self, tool: Tool | None, call: ToolCall) -> str:
        title = tool.summary(call.input) if tool and tool.summary else f"{call.name} {preview(call.input)[:200]}"
        return f"[{self.label}] {title}" if self.label else title

    def execute(self, call: ToolCall, cwd: str, subagent: bool = False, indent: str = "  ", evaluated=None) -> dict:
        """Run one call. Returns a provider-neutral result: {id, content, is_error}."""
        started = time.monotonic()
        tool, policy_call, decision, error = evaluated or self._evaluate(call, cwd, subagent)
        title = self._title(tool, call)
        approved, hook_context = None, ""

        if not error and decision.action != "deny" and self.hooks.active("PreToolUse"):
            hook = self.hooks.run("PreToolUse", {"tool_name": call.name, "tool_input": call.input},
                                  tool_name=call.name, cwd=cwd)
            if hook.decision == "block":
                decision = Decision("deny", f"a PreToolUse hook blocked it: {hook.reason}")
            elif hook.decision == "allow" and decision.action == "ask":
                decision = Decision("allow", f"approved by a hook{': ' + hook.reason if hook.reason else ''}")
            hook_context = hook.context

        if error:
            content, is_error = error, True
        elif decision.action == "deny":
            content, is_error = f"Blocked by policy: {decision.reason}.", True
            self.ui.notice(f"✗ {title}   (blocked: {decision.reason})", indent)
        else:
            ok, feedback = True, ""
            if decision.action == "ask" and not tool.confirms_itself:
                ok, feedback, grant = self.ui.approve(call.id, title, decision.reason, decision.grantable, decision.grant_label)
                approved = ok
                if grant:
                    self.policy.grant(policy_call)
                if ok:
                    self.ui.tool_start(call.id, title, tool.acp_kind, indent, "approved")
            elif not tool.confirms_itself and tool.summary:
                self.ui.tool_start(call.id, title, tool.acp_kind, indent, decision.reason)

            if not ok:
                content, is_error = declined(feedback), True
            else:
                def confirm(preview_text: str) -> tuple[bool, str]:
                    nonlocal approved
                    if decision.action == "allow":
                        self.ui.tool_start(call.id, preview_text.splitlines()[0], tool.acp_kind, indent,
                                           f"auto-approved: {decision.reason}")
                        return True, ""
                    answer, note, grant_all = self.ui.approve(call.id, preview_text, decision.reason,
                                                              decision.grantable, decision.grant_label)
                    approved = answer
                    if grant_all:
                        self.policy.grant(policy_call)
                    return answer, note

                ctx = Context(cwd, subagent, call.id, confirm, lambda line: self.ui.tool_output(call.id, line))
                try:
                    content, is_error = tool.run(call.input, ctx)
                except KeyboardInterrupt:
                    raise
                except Exception as e:   # a tool bug shouldn't end the session - tell the model instead
                    content, is_error = f"The tool failed: {type(e).__name__}: {e}", True

                if self.hooks.active("PostToolUse"):
                    hook = self.hooks.run("PostToolUse", {"tool_name": call.name, "tool_input": call.input,
                                                          "tool_response": content_text(content), "is_error": is_error},
                                          tool_name=call.name, cwd=cwd)
                    extra = "\n".join(x for x in (hook.reason if hook.decision == "block" else "", hook.context) if x)
                    if extra:
                        hook_context = (hook_context + "\n" + extra).strip()

        if hook_context:
            note = f"\n\n[hook] {hook_context}"
            content = content + note if isinstance(content, str) else [*content, {"type": "text", "text": note}]
        if is_error:
            self.failures[(call.name, json.dumps(call.input, sort_keys=True, default=str))] += 1
        self.ui.tool_end(call.id, content, is_error)
        self.ledger.event(
            "tool_call", id=call.id, tool=call.name, subagent=subagent, input=preview(call.input),
            decision=decision.action if decision else "invalid", reason=decision.reason if decision else error,
            approved=approved, ms=round((time.monotonic() - started) * 1000), is_error=is_error,
            output_chars=content_chars(content))
        return {"id": call.id, "content": content, "is_error": is_error}

    # -- one round of calls ---------------------------------------------------------------------

    def execute_round(self, calls: list[ToolCall], cwd: Callable[[], str], results: list,
                      subagent: bool = False, indent: str = "  ") -> None:
        """Run every tool call from one response, appending each result to `results` in call order
        (so a cancel keeps the ones already done). Like Claude Code: consecutive calls that are
        parallel-safe and need no approval run together; anything else runs on its own, in order -
        a `cd` must land before the next command."""
        if len(calls) < 2 or self.hooks.active("PreToolUse"):
            for call in calls:
                if self.cancel.is_set():
                    raise TurnCancelled()
                results.append(self.execute(call, cwd(), subagent, indent))
            return
        batch: list = []
        for call in calls:
            evaluated = self._evaluate(call, cwd(), subagent)
            tool, _, decision, error = evaluated
            if tool and tool.parallel_safe and not error and decision.action == "allow":
                batch.append((call, evaluated))
                continue
            self._run_batch(batch, cwd, results, subagent, indent)
            batch = []
            if self.cancel.is_set():
                raise TurnCancelled()
            results.append(self.execute(call, cwd(), subagent, indent))
        self._run_batch(batch, cwd, results, subagent, indent)

    def _run_batch(self, batch: list, cwd, results: list, subagent: bool, indent: str) -> None:
        if self.cancel.is_set() and batch:
            raise TurnCancelled()
        if len(batch) == 1:
            call, evaluated = batch[0]
            results.append(self.execute(call, cwd(), subagent, indent, evaluated))
            return
        if not batch:
            return
        pool = ThreadPoolExecutor(max_workers=min(8, len(batch)))
        futures = [pool.submit(self.execute, c, cwd(), subagent, indent, e) for c, e in batch]
        try:
            self.ui.serve_while(futures)   # the terminal answers worker threads' approvals here
            results.extend(f.result() for f in futures)
        except KeyboardInterrupt:
            self.interrupt()               # stop the other calls' commands too
            self.ui.cancel_pending()
            results.extend(f.result() for f in futures
                           if f.done() and not f.cancelled() and f.exception() is None)
            raise TurnCancelled() from None
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def repeat_warning(self, calls: list[ToolCall]) -> str | None:
        repeated = [c.name for c in calls
                    if self.failures[(c.name, json.dumps(c.input, sort_keys=True, default=str))] >= REPEAT_LIMIT]
        if not repeated:
            return None
        return (f"Note from the agent runtime: the exact same {', '.join(sorted(set(repeated)))} call has now "
                f"failed {REPEAT_LIMIT}+ times. Don't repeat it - diagnose why it fails and change approach, "
                "or stop and tell the user what is blocking you.")
