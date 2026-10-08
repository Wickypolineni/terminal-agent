"""The whole engine, end to end, with the Anthropic provider against a scripted fake Anthropic API."""
import builtins
import json
import os
import tempfile
from pathlib import Path

work = Path(tempfile.mkdtemp())
os.environ["SYSADMIN_AGENT_DATA"] = str(work / "data")
os.environ["SYSADMIN_AGENT_CONFIG"] = str(work / "no-config.json")

from harness import FakeAPI, check, done, message, note, text, tool_use  # noqa: E402
import agent  # noqa: E402
import engine  # noqa: E402
import providers  # noqa: E402
import tools  # noqa: E402
from ui import TerminalUI  # noqa: E402

providers.RETRY_DELAYS = [0, 0, 0]
os.chdir(work)
(work / "app.conf").write_text("port = 80\n")
tools.BACKUP_ROOT = work / "backups"
inputs: list = []


def scripted_input(prompt=""):
    answer = inputs.pop(0) if inputs else ""
    if answer == "^C":
        raise KeyboardInterrupt
    return answer


builtins.input = scripted_input


def session(api: FakeAPI, **config):
    cfg = engine.load_config()
    cfg.update(provider="anthropic", **config)
    return engine.Session(cfg, TerminalUI(), client=api.client(), cwd=str(work))


def last_user(api, n=-1):
    return api.main_requests()[n]["body"]["messages"][-1]["content"]


# ---- 1. full flow: parallel reads, approved edit, to-dos, subagent, web pause/resume, compaction
api = FakeAPI(
    main=[
        message([note("Checking memory and config."), tool_use("t1", "memory", {"command": "view", "path": "/memories"}),
                 tool_use("t2", "read_file", {"path": "app.conf"})], "tool_use"),
        message([tool_use("t3", "edit_file", {"path": "app.conf", "old_string": "port = 80", "new_string": "port = 8080"}),
                 tool_use("t4", "update_todos", {"todos": [{"content": "Change port", "status": "completed"}]})], "tool_use"),
        message([tool_use("t5", "delegate", {"task": "List what is in the current directory."})], "tool_use"),
        message([({"type": "server_tool_use", "id": "srv1", "name": "web_search", "input": {}},
                  [{"type": "input_json_delta", "partial_json": "{\"query\": \"port 8080\"}"}])], "pause_turn"),
        message([({"type": "web_search_tool_result", "tool_use_id": "srv1", "content": []}, []),
                 ({"type": "compaction", "content": "Summary."}, []), text("Done: port is now 8080.")], "end_turn"),
    ],
    sub=[
        message([tool_use("s1", "bash", {"command": "ls"}), tool_use("s2", "bash", {"command": "rm -rf app.conf"})], "tool_use"),
        message([text("Report: one file, app.conf.")], "end_turn"),
    ])
s = session(api)
inputs[:] = ["y"]                       # approve the edit
final, stop = engine.run_turn(s, "move the app to port 8080")
main = api.main_requests()
check("1. betas: fallback + progress notes + compaction", all(b in main[0]["beta"] for b in ("server-side-fallback", "thinking-display-updates", "compact-2026-01-12")))
eager = {t["name"]: t.get("eager_input_streaming") for t in main[0]["body"]["tools"]}
check("1. eager streaming only on custom tools", eager["read_file"] and eager["http_request"] and eager["bash"] is None and eager["web_search"] is None)
check("1. native tool types", {t.get("type") for t in main[0]["body"]["tools"]} >= {"bash_20250124", "memory_20250818", "web_search_20260209", "web_fetch_20260209"})
check("1. both tool results returned in ONE message", [r["tool_use_id"] for r in last_user(api, 1)] == ["t1", "t2"])
check("1. approved edit applied to the real file", (work / "app.conf").read_text() == "port = 8080\n")
sub = [r for r in api.requests if r["sub"]]
check("1. subagent: medium effort, no write tools offered",
      sub[0]["body"]["output_config"]["effort"] == "medium" and not {"edit_file", "write_file", "memory", "delegate"} & {t["name"] for t in sub[0]["body"]["tools"]})
sres = sub[1]["body"]["messages"][-1]["content"]
check("1. subagent: ls ran, rm blocked by policy", "app.conf" in sres[0]["content"] and "Blocked by policy" in sres[1]["content"], sres)
check("1. subagent never deleted the file", (work / "app.conf").exists())
check("1. subagent report reached the main agent", "Report: one file" in last_user(api, 3)[0]["content"])
check("1. pause_turn resumed with no extra user message", main[4]["body"]["messages"][-1]["role"] == "assistant")
check("1. compaction block kept in history", any(b.type == "compaction" for b in s.history[-1]["content"]))
check("1. final answer returned", (final, stop) == ("Done: port is now 8080.", "end_turn"), (final, stop))
ledger = [json.loads(line) for line in s.ledger.path.read_text().splitlines()]
calls = {e["tool"]: e for e in ledger if e["event"] == "tool_call"}
check("1. ledger: every tool call recorded", {"memory", "read_file", "edit_file", "update_todos", "delegate", "bash"} <= set(calls), set(calls))
check("1. ledger: the edit's approval recorded", calls["edit_file"]["decision"] == "ask" and calls["edit_file"]["approved"] is True)
check("1. ledger: model rounds with cost", sum(e["event"] == "model_round" for e in ledger) == 7)
check("1. ledger file readable only by you", oct(s.ledger.path.stat().st_mode)[-3:] == "600")
saved = s.store.load(s.id)
check("1. session saved with its history (0600)", saved["provider"] == "anthropic" and len(saved["history"]) == len(s.history)
      and oct(s.store.path(s.id).stat().st_mode)[-3:] == "600")
s.close()

# ---- 1b. resume the saved session: its history goes back to the API as-is
api2 = FakeAPI(main=[message([text("still 8080")], "end_turn")])
r = session(api2)
r.resume(s.id)
engine.run_turn(r, "what port?")
sent = api2.main_requests()[0]["body"]["messages"]
check("1b. resumed history sent before the new message",
      len(sent) == len(saved["history"]) + 1 and sent[0]["content"] == "move the app to port 8080" and sent[-1]["content"] == "what port?")
check("1b. thinking signature survives the save/load round trip",
      any(b.get("type") == "thinking" and b.get("signature") == "sig" for m in sent for b in (m["content"] if isinstance(m["content"], list) else [])))
r.close()

# ---- 2. API retry on overload
api = FakeAPI(main=[529, message([text("ok")], "end_turn")])
s = session(api)
check("2. overloaded (529) request is retried", engine.run_turn(s, "hi")[0] == "ok" and len(api.requests) == 2)
check("2. retry recorded in the ledger", "api_retry" in s.ledger.path.read_text())
s.close()

# ---- 3. Ctrl+C during a task cancels the turn, not the session
api = FakeAPI(main=[message([tool_use("c1", "bash", {"command": "make build"})], "tool_use"),
                    message([text("ok, stopping")], "end_turn")])
s = session(api)
inputs[:] = ["^C"]                      # Ctrl+C at the approval prompt
check("3. Ctrl+C cancels the turn", engine.run_turn(s, "build it") == ("", "cancelled"))
check("3. the pending call got a 'cancelled' result (history stays valid)",
      s.history[-1]["role"] == "user" and "Cancelled" in s.history[-1]["content"][0]["content"], s.history[-1])
check("3. the next request works", engine.run_turn(s, "never mind")[0] == "ok, stopping")
s.close()

# ---- 4. per-request budget
huge = {"input_tokens": 2_000_000, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
api = FakeAPI(main=[message([tool_use("b1", "bash", {"command": "ls"})], "tool_use", usage=huge)])
s = session(api, budget_usd_per_request=1.0)
_, stop = engine.run_turn(s, "go")
check("4. stops when the request goes over budget",
      len(api.requests) == 1 and stop == "max_turn_requests" and "budget" in s.history[-1]["content"][0]["content"])
s.close()

# ---- 5. the same failing call three times -> told to change approach
fail = lambda i: message([tool_use(f"r{i}", "bash", {"command": "ls /no/such/dir"})], "tool_use")  # noqa: E731
api = FakeAPI(main=[fail(1), fail(2), fail(3), message([text("changing approach")], "end_turn")])
s = session(api)
engine.run_turn(s, "look")
warned = [b for b in last_user(api) if b.get("type") == "text"]
check("5. repeat-failure warning appended after the 3rd identical failure", warned and "failed 3+ times" in warned[0]["text"], last_user(api))
check("5. ... but not after the 2nd", not [b for b in last_user(api, 2) if b.get("type") == "text"])
s.close()

# ---- 6. declining with feedback, and hard denies
api = FakeAPI(main=[message([tool_use("d1", "bash", {"command": "make build"})], "tool_use"),
                    message([tool_use("d2", "bash", {"command": "rm -rf /"})], "tool_use"),
                    message([text("ok")], "end_turn")])
s = session(api)
inputs[:] = ["use ninja instead"]
engine.run_turn(s, "build")
declined = last_user(api, 1)[0]
check("6. decline + typed feedback reaches the model", declined["is_error"] and "use ninja instead" in declined["content"], declined)
denied = last_user(api, 2)[0]
check("6. `rm -rf /` blocked without even asking", "Blocked by policy" in denied["content"] and not inputs, denied)
s.close()

# ---- 7. the chat loop: /mode sends a system note with the next message; session saved
api = FakeAPI(main=[message([text("hello!")], "end_turn")])
s = session(api)
inputs[:] = ["/mode auto", "/tools", "hi there", "/exit"]
agent.chat(s)
sent = api.main_requests()[0]["body"]["messages"]
check("7. /mode switches the policy", s.policy.mode == "auto")
check("7. mode change delivered as a mid-conversation system message",
      sent[-2]["role"] == "user" and sent[-1]["role"] == "system" and "auto" in sent[-1]["content"], sent)
check("7. session saved after the turn", "hello!" in s.store.path(s.id).read_text())
s.close()

done()
