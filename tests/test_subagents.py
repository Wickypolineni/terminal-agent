"""Subagents: several run in parallel, research ones can't change anything, worker ones ask the user
(on the main thread, labelled), and Ctrl+C stops all of them. Model: the fake OpenRouter."""
import builtins
import contextlib
import io
import json
import os
import tempfile
import threading
import time
from pathlib import Path

work = Path(tempfile.mkdtemp())
os.environ["SYSADMIN_AGENT_DATA"] = str(work / "data")
os.environ["SYSADMIN_AGENT_CONFIG"] = str(work / "no-config.json")
os.environ["OPENROUTER_API_KEY"] = "test-key"

from harness import check, done  # noqa: E402
from fake_openrouter import FakeOpenRouter, config, text_reply, tool_reply  # noqa: E402
import engine  # noqa: E402
import providers  # noqa: E402
from ui import UI, TerminalUI  # noqa: E402


class Recorder(UI):
    def approve(self, call_id, title, reason, grantable, grant_label):
        return True, "", False

providers.RETRY_DELAYS = [0, 0, 0]
os.chdir(work)
fake = FakeOpenRouter()
inputs: list = []
asked: list = []   # (prompt shown, thread it was read on)


def scripted_input(prompt=""):
    asked.append((prompt, threading.current_thread() is threading.main_thread()))
    answer = inputs.pop(0) if inputs else ""
    if answer == "^C":
        raise KeyboardInterrupt
    return answer


builtins.input = scripted_input


def text_of(message: dict) -> str:
    content = message["content"]
    return content if isinstance(content, str) else "".join(p.get("text", "") for p in content or [])


def all_text(messages: list) -> str:
    return "\n".join(text_of(m) for m in messages)


def kind(body: dict) -> tuple[str, int]:
    """Who sent this request (main / the task tag of a subagent), and how many tool rounds it has had."""
    system = text_of(body["messages"][0])
    rounds = sum(m["role"] == "tool" for m in body["messages"])
    if not system.startswith("You are a subagent"):
        return "main", rounds
    return text_of(body["messages"][1]).split(":")[0], rounds


# ---- 1. two subagents in parallel: a research one and a worker one
barrier = threading.Barrier(2, timeout=10)
tools_seen: dict = {}


def route(body):
    who, rounds = kind(body)
    if who == "main":
        if rounds == 0:
            # like the live run: a non-parallel call (memory) in the same response as the delegates
            return tool_reply([("m0", "memory", {"command": "view", "path": "/memories"}),
                               ("d1", "delegate", {"task": "TASK-A: look around, then try to touch a.txt", "name": "logs"}),
                               ("d2", "delegate", {"task": "TASK-B: create built.txt", "name": "build", "can_change": True})])
        return text_reply("Both done.")
    tools_seen[who] = {t["function"]["name"] for t in body["tools"] if t["type"] == "function"}
    if rounds == 0:
        try:
            barrier.wait()            # both subagents must be asking the model at the same time
        except threading.BrokenBarrierError:
            return text_reply("SEQUENTIAL")
        return tool_reply([(f"{who}-1", "bash", {"command": "touch a.txt" if who == "TASK-A" else "touch built.txt"})])
    return text_reply(f"{who} report: " + text_of(body["messages"][-1])[:80])


fake.router = route
s = engine.Session(config(fake.url), TerminalUI(), cwd=str(work))
inputs[:] = ["y"]
out = io.StringIO()
with contextlib.redirect_stdout(out):
    final, stop = engine.run_turn(s, "build it and check the logs")
shown = out.getvalue()
main_reqs = [r["body"] for r in fake.requests if kind(r["body"])[0] == "main"]
results = [m for m in main_reqs[-1]["messages"] if m["role"] == "tool"]
check("1. both subagents asked the model at the same time (parallel)", "SEQUENTIAL" not in json.dumps(results), results)
check("1. reports come back to the main agent, one per call, in order",
      [m["tool_call_id"] for m in results] == ["m0", "d1", "d2"]
      and "TASK-A report" in text_of(results[1]) and "TASK-B report" in text_of(results[2]), results)
check("1. research subagent: change refused, nothing written",
      "Blocked by policy" in text_of(results[1]) and not (work / "a.txt").exists(), text_of(results[1]))
check("1. worker subagent: approved change really happened", (work / "built.txt").exists())
check("1. exactly one approval asked - the worker's - on the main thread",
      len(asked) == 1 and asked[0][1], asked)
check("1. the approval is labelled with the subagent's name", "[build] $ touch built.txt" in shown, shown[-600:])
check("1. worker gets file-editing tools, research doesn't",
      {"edit_file", "write_file"} <= tools_seen["TASK-B"] and not {"edit_file", "write_file"} & tools_seen["TASK-A"]
      and "delegate" not in tools_seen["TASK-B"])
check("1. subagents' own text isn't streamed into the terminal", "TASK-A report" not in shown)
check("1. final answer", (final, stop) == ("Both done.", "end_turn"))
events = [json.loads(line)["event"] for line in s.ledger.path.read_text().splitlines()]
check("1. ledger records each subagent start and end", events.count("subagent_start") == 2 and events.count("subagent_end") == 2)
s.close()

# ---- 2. Ctrl+C while subagents work stops all of them, and the session carries on
def route_cancel(body):
    who, rounds = kind(body)
    if who == "main":
        if rounds == 0 and "again" not in json.dumps(body["messages"][-1]):
            return tool_reply([("e1", "delegate", {"task": "TASK-C: sleep", "name": "c", "can_change": True}),
                               ("e2", "delegate", {"task": "TASK-D: sleep", "name": "d", "can_change": True})])
        return text_reply("Ready again.")
    if rounds == 0:
        return tool_reply([(f"{who}-1", "bash", {"command": f"touch {who}.txt; sleep 30"})])
    return text_reply("finished sleeping")


fake.router = route_cancel
asked.clear()
s = engine.Session(config(fake.url), TerminalUI(), cwd=str(work))
inputs[:] = ["y", "^C"]                 # approve one sleep, then Ctrl+C at the second approval
started = time.monotonic()
with contextlib.redirect_stdout(io.StringIO()):
    result = engine.run_turn(s, "sleep twice")
elapsed = time.monotonic() - started
check("2. Ctrl+C cancels the turn", result == ("", "cancelled"), result)
check("2. ... promptly: the running `sleep 30` was killed", elapsed < 10, f"{elapsed:.1f}s")
calls = [m for m in s.history if m.get("role") == "tool"]
check("2. both delegate calls still got a result (history stays valid)", [m["tool_call_id"] for m in calls] == ["e1", "e2"], calls)
time.sleep(0.5)
with contextlib.redirect_stdout(io.StringIO()):
    again = engine.run_turn(s, "again")
check("2. the next request works", again[0] == "Ready again.", again)
check("2. no subagents left running", not s.subagents.active())
s.close()

# ---- 4. background: the parent keeps working; the report arrives before the turn ends
def text_reply_slow(text, delay):
    time.sleep(delay)
    return text_reply(text)


def route_bg(body):
    who, rounds = kind(body)
    last = text_of(body["messages"][-1])
    if who == "main":
        if rounds == 0:
            return tool_reply([("g1", "delegate", {"task": "BG: slow research", "name": "slow", "background": True})])
        if "Background subagent reports" in json.dumps(body["messages"]):
            return text_reply("Final, with the background report.")
        return text_reply("Main work done.")
    return text_reply_slow("BG report: found it", 1.0)


fake.router = route_bg
fake.requests.clear()
s = engine.Session(config(fake.url), Recorder(), cwd=str(work))
started = time.monotonic()
final, stop = engine.run_turn(s, "go")
mains = [r["body"] for r in fake.requests if kind(r["body"])[0] == "main"]
first_result = [m for m in mains[1]["messages"] if m["role"] == "tool"][0]
check("4. background delegate returns at once", "in the background" in text_of(first_result), text_of(first_result))
check("4. the turn waited for the report and the model used it",
      final == "Final, with the background report." and "BG report: found it" in json.dumps(mains[-1]["messages"]), final)
check("4. report carries a status line", "[subagent slow · completed" in all_text(mains[-1]["messages"]))
s.close()

# ---- 5. named agent types (file), their own model + instructions; subagent_model for plain ones
agents_dir = work / ".agent/agents"
agents_dir.mkdir(parents=True, exist_ok=True)
(agents_dir / "reviewer.md").write_text("---\nname: reviewer\ndescription: Reviews code\nmodel: other/model\n"
                                        "effort: low\n---\nROLE-MARKER: review carefully.\n")


def route_types(body):
    who, rounds = kind(body)
    if who == "main":
        if rounds == 0:
            return tool_reply([("t1", "delegate", {"task": "TYPED: review x", "agent": "reviewer"}),
                               ("t2", "delegate", {"task": "PLAIN: look", "name": "plain"}),
                               ("t3", "delegate", {"task": "BAD: x", "agent": "nope"})])
        return text_reply("ok")
    return text_reply(f"{who} done")


fake.router = route_types
fake.requests.clear()
s = engine.Session(config(fake.url, subagent_model="cheap/model"), Recorder(), cwd=str(work))
engine.run_turn(s, "review")
subs = {kind(r["body"])[0]: r["body"] for r in fake.requests if kind(r["body"])[0] != "main"}
check("5. agent types listed in the main system prompt", "- reviewer: Reviews code" in text_of(fake.requests[0]["body"]["messages"][0]))
check("5. typed subagent: its model, effort and instructions",
      subs["TYPED"]["model"] == "other/model" and subs["TYPED"]["reasoning"] == {"effort": "low"}
      and "ROLE-MARKER" in text_of(subs["TYPED"]["messages"][0]))
check("5. plain subagent uses subagent_model", subs["PLAIN"]["model"] == "cheap/model")
bad = [m for m in fake.requests[-1]["body"]["messages"] if m["role"] == "tool"][2]
check("5. unknown type is an error listing the types", "No subagent type 'nope'" in text_of(bad) and "reviewer" in text_of(bad))
s.close()

# ---- 6. the subagents tool: list, then stop a running background child
release = threading.Event()


def route_manage(body):
    who, rounds = kind(body)
    if who == "main":
        if "Background subagent reports" in json.dumps(body["messages"]):
            return text_reply("final")
        return [tool_reply([("k1", "delegate", {"task": "HANG: wait", "name": "hang", "background": True})]),
                tool_reply([("k2", "subagents", {"action": "list"})]),
                tool_reply([("k3", "subagents", {"action": "stop", "ids": ["hang"]})])][rounds] if rounds < 3 else (
                release.set() or text_reply("stopped it"))
    release.wait(10)
    return tool_reply([("h1", "bash", {"command": "ls"})])


fake.router = route_manage
fake.requests.clear()
s = engine.Session(config(fake.url), Recorder(), cwd=str(work))
final, _ = engine.run_turn(s, "go")
results = [m for m in fake.requests[-1]["body"]["messages"] if m["role"] == "tool"]
check("6. list shows the running child", "[hang]  running (background)" in text_of(results[1]), text_of(results[1]))
check("6. stop acknowledged", "Stopping hang-" in text_of(results[2]), text_of(results[2]))
check("6. stopped child's partial report delivered", "[subagent hang · stopped" in all_text(fake.requests[-1]["body"]["messages"]))
check("6. turn completes", final == "final", final)
s.close()

# ---- 7. time limit: a slow child is stopped and reported as timed out
def route_timeout(body):
    who, rounds = kind(body)
    if who == "main":
        return tool_reply([("o1", "delegate", {"task": "SLOW: sleep", "name": "slowpoke"})]) if rounds == 0 else text_reply("ok")
    return tool_reply([("s1", "bash", {"command": "sleep 20"})]) if rounds == 0 else text_reply("woke up")


fake.router = route_timeout
fake.requests.clear()
s = engine.Session(config(fake.url, subagents={"timeout_seconds": 1}), Recorder(), cwd=str(work))
started = time.monotonic()
engine.run_turn(s, "go")
result = [m for m in fake.requests[-1]["body"]["messages"] if m["role"] == "tool"][0]
check("7. child stopped at its time limit", "slowpoke · timeout" in text_of(result) and time.monotonic() - started < 8,
      (text_of(result)[:120], time.monotonic() - started))
s.close()

# ---- 8. hooks: SubagentStart can block or add context; SubagentStop adds a note
hook_dir = work / "hooks"
hook_dir.mkdir(exist_ok=True)
start_hook = hook_dir / "start.sh"
start_hook.write_text("#!/usr/bin/env bash\nif grep -q FORBIDDEN; then echo 'not this one' >&2; exit 2; fi\n"
                      "echo '{\"additionalContext\": \"HOOK-CONTEXT\"}'\n")
stop_hook = hook_dir / "stop.sh"
stop_hook.write_text("#!/usr/bin/env bash\ncat > /dev/null; echo '{\"additionalContext\": \"STOP-NOTE\"}'\n")
start_hook.chmod(0o755)
stop_hook.chmod(0o755)


def route_hooks(body):
    who, rounds = kind(body)
    if who == "main":
        return tool_reply([("x1", "delegate", {"task": "OK: fine", "name": "ok"}),
                           ("x2", "delegate", {"task": "NO: FORBIDDEN task", "name": "no"})]) if rounds == 0 else text_reply("ok")
    return text_reply("child saw: " + text_of(body["messages"][1]).replace("\n", " "))


fake.router = route_hooks
fake.requests.clear()
s = engine.Session(config(fake.url, hooks={"SubagentStart": [{"command": str(start_hook)}],
                                           "SubagentStop": [{"command": str(stop_hook)}]}), Recorder(), cwd=str(work))
engine.run_turn(s, "go")
results = [m for m in fake.requests[-1]["body"]["messages"] if m["role"] == "tool"]
check("8. SubagentStart context reaches the child", "HOOK-CONTEXT" in text_of(results[0]), text_of(results[0]))
check("8. SubagentStop context added to the report", "STOP-NOTE" in text_of(results[0]))
check("8. SubagentStart exit 2 blocks the spawn", "SubagentStart hook blocked it: not this one" in text_of(results[1]))
check("8. the blocked child never called the model", not any(kind(r["body"])[0] == "NO" for r in fake.requests))
s.close()

# ---- 9. concurrency cap, long-report trimming, and the request budget covering subagents
active, peak = [0], [0]
gate = threading.Lock()


def route_cap(body):
    who, rounds = kind(body)
    if who == "main":
        if rounds == 0:
            return tool_reply([(f"c{i}", "delegate", {"task": f"CAP{i}: x", "name": f"c{i}"}) for i in range(3)])
        return text_reply("ok")
    with gate:
        active[0] += 1
        peak[0] = max(peak[0], active[0])
    time.sleep(0.5)
    with gate:
        active[0] -= 1
    return text_reply("L" * 30_000 if who == "CAP0" else "short")


fake.router = route_cap
fake.requests.clear()
s = engine.Session(config(fake.url, subagents={"max_parallel": 2}), Recorder(), cwd=str(work))
engine.run_turn(s, "go")
results = [m for m in fake.requests[-1]["body"]["messages"] if m["role"] == "tool"]
check("9. at most max_parallel subagents at once", peak[0] == 2, peak[0])
saved = list((work / "data/subagents").rglob("c0-*.md"))
check("9. long report cut, full text saved to a file",
      "report cut: 30,000 characters" in text_of(results[0]) and saved and len(saved[0].read_text()) == 30_000)
s.close()


def route_budget(body):
    who, rounds = kind(body)
    if who == "main":
        return tool_reply([("b1", "delegate", {"task": "PRICEY: x", "name": "pricey"})]) if rounds == 0 else \
            tool_reply([("b2", "bash", {"command": "ls"})])
    return tool_reply([(f"p{rounds}", "bash", {"command": "ls"})], cost=0.6)


fake.router = route_budget
fake.requests.clear()
s = engine.Session(config(fake.url, budget_usd_per_request=1.0), Recorder(), cwd=str(work))
_, stop = engine.run_turn(s, "go")
child_rounds = sum(kind(r["body"])[0] == "PRICEY" for r in fake.requests)
check("9. budget counts subagent spending: child and parent both stop", child_rounds == 2 and stop == "max_turn_requests",
      (child_rounds, stop))
s.close()

# ---- 3. the API key can come from a .env file (and isn't exported to the agent's shell)
env_file = work / ".env"
env_file.write_text('# keys\nexport OPENROUTER_API_KEY_TEST="from-dotenv"\n')
key = providers.OpenRouterProvider._api_key({"api_key_env": "OPENROUTER_API_KEY_TEST", "api_key_file": str(work / "none"),
                                              "env_file": str(env_file)})
check("3. key read from .env (quotes stripped)", key == "from-dotenv", key)
check("3. ... and not exported to the environment", "OPENROUTER_API_KEY_TEST" not in os.environ)

fake.close()
done()
