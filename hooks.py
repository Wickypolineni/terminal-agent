"""Hooks: your own scripts, run at fixed points of the agent loop (same contract as Claude Code's
hooks, so existing hook scripts carry over).

config.json:
  "hooks": {
    "PreToolUse":       [{"matcher": "bash|edit_file", "command": "~/hooks/check.sh"}],
    "PostToolUse":      [{"matcher": "*", "command": "..."}],
    "UserPromptSubmit": [{"command": "..."}],
    "Stop":             [{"command": "..."}],
    "SubagentStart":    [{"command": "..."}],   gets task/name/agent_type; exit 2 = don't start it,
                                                stdout/additionalContext is added to its task
    "SubagentStop":     [{"command": "..."}]    gets name/status/report; additionalContext is added
  }

Each hook gets a JSON object on stdin (hook_event_name, session_id, cwd, and tool_name /
tool_input / tool_response / prompt as relevant). It answers with its exit code:
  0  carry on. Optional JSON on stdout: {"decision": "block"|"allow", "reason": "...",
     "additionalContext": "..."} (plain text on stdout counts as additionalContext)
  2  block: the action is refused (PreToolUse, UserPromptSubmit) or Claude is told to keep
     working (Stop); stderr is the reason, shown to Claude
  anything else: the hook failed - reported, and the agent carries on
"""
import fnmatch
import json
import os
import subprocess
from dataclasses import dataclass

HOOK_TIMEOUT = 60
EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SubagentStart", "SubagentStop")


@dataclass
class HookResult:
    decision: str | None = None   # "block" | "allow" | None
    reason: str = ""
    context: str = ""             # extra text handed to Claude
    errors: str = ""              # hooks that failed


class Hooks:
    def __init__(self, config: dict | None, session_id: str = "") -> None:
        self.config = {event: list((config or {}).get(event, [])) for event in EVENTS}
        self.session_id = session_id

    def active(self, event: str) -> bool:
        return bool(self.config.get(event))

    def run(self, event: str, payload: dict, tool_name: str = "", cwd: str = "") -> HookResult:
        result = HookResult()
        for hook in self.config.get(event, []):
            matcher = hook.get("matcher", "*")
            if tool_name and not any(fnmatch.fnmatchcase(tool_name, m.strip()) for m in matcher.split("|")):
                continue
            data = {"hook_event_name": event, "session_id": self.session_id, "cwd": cwd, **payload}
            try:
                proc = subprocess.run(os.path.expanduser(hook["command"]), shell=True, input=json.dumps(data, default=str),
                                      capture_output=True, text=True, timeout=hook.get("timeout", HOOK_TIMEOUT), cwd=cwd or None)
            except subprocess.TimeoutExpired:
                result.errors += f"hook {hook['command']!r} timed out; "
                continue
            if proc.returncode == 2:
                return HookResult("block", proc.stderr.strip() or "blocked by a hook", result.context, result.errors)
            if proc.returncode != 0:
                result.errors += f"hook {hook['command']!r} exited {proc.returncode}: {proc.stderr.strip()[:200]}; "
                continue
            out = proc.stdout.strip()
            if not out:
                continue
            try:
                answer = json.loads(out)
            except ValueError:
                answer = {"additionalContext": out}
            if not isinstance(answer, dict):
                answer = {"additionalContext": out}
            decision = answer.get("decision")
            if decision in ("block", "deny"):
                return HookResult("block", answer.get("reason") or "blocked by a hook", result.context, result.errors)
            if decision in ("allow", "approve"):
                result.decision = "allow"
            if answer.get("reason") and not result.reason:
                result.reason = answer["reason"]
            if answer.get("additionalContext"):
                result.context += ("\n" if result.context else "") + str(answer["additionalContext"])
        return result
