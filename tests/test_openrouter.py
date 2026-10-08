"""The engine on the OpenRouter provider, end to end, against a local fake OpenRouter server."""
import json
import os
import struct
import tempfile
import time
import zlib
from pathlib import Path

work = Path(tempfile.mkdtemp())
os.environ["SYSADMIN_AGENT_DATA"] = str(work / "data")
os.environ["SYSADMIN_AGENT_CONFIG"] = str(work / "no-config.json")
os.environ["OPENROUTER_API_KEY"] = "test-key"

from harness import check, done  # noqa: E402
from fake_openrouter import FakeOpenRouter, config, text_reply, tool_reply  # noqa: E402
import engine  # noqa: E402
import providers  # noqa: E402
import tools  # noqa: E402
from providers import SUMMARY_PROMPT, ProviderError  # noqa: E402
from ui import UI, HeadlessUI  # noqa: E402

providers.RETRY_DELAYS = [0, 0, 0]
tools.BACKUP_ROOT = work / "backups"
os.chdir(work)
(work / "app.conf").write_text("port = 80\n")
(work / "notes.txt").write_text("hello\n")


def png(path: Path) -> None:
    raw = b"\x00\xff\x00\x00" * 1          # one red pixel
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))  # noqa: E731
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw[:4])) + chunk(b"IEND", b""))


png(work / "shot.png")


class Recorder(UI):
    def __init__(self):
        self.events = []

    def text(self, delta, indent=""):
        self.events.append(("text", delta))

    def thought(self, delta, indent=""):
        self.events.append(("thought", delta))

    def notice(self, message, indent=""):
        self.events.append(("notice", message))

    def tool_start(self, call_id, title, kind, indent="", note=""):
        self.events.append(("tool_start", call_id, kind))

    def tool_end(self, call_id, content, is_error):
        self.events.append(("tool_end", call_id, is_error))

    def approve(self, call_id, title, reason, grantable, grant_label):
        self.events.append(("approve", call_id, title))
        return True, "", False

    def todos(self, todos):
        self.events.append(("todos", todos))


fake = FakeOpenRouter()


def body_text(message: dict) -> str:
    """A request message's text, whether plain or split into (cache-marked) parts."""
    content = message["content"]
    return content if isinstance(content, str) else "".join(p.get("text", "") for p in content)


def session(ui=None, **overrides):
    return engine.Session(config(fake.url, **overrides), ui or Recorder(), cwd=str(work))


def reset(*script):
    fake.requests.clear()
    fake.script[:] = list(script)


# ---- 1. a full turn: reasoning + parallel reads, then an approved command, then the answer
reset(tool_reply([("c1", "read_file", {"path": "app.conf"}), ("c2", "read_file", {"path": "notes.txt"})],
                 text="Let me look.", reasoning="I should read both files first.", cost=0.01),
      tool_reply([("c3", "bash", {"command": "make build"})], cost=0.02),
      text_reply("All done: port is 80.", cost=0.005))
ui = Recorder()
s = session(ui)
final, stop = engine.run_turn(s, "check the app")
b = fake.bodies()
check("1. three requests, final answer + stop", len(b) == 3 and (final, stop) == ("All done: port is 80.", "end_turn"), (final, stop, len(b)))
check("1. API key sent as a bearer token", fake.requests[0]["auth"] == "Bearer test-key")
first = b[0]
check("1. streaming with usage accounting + reasoning effort",
      first["stream"] is True and first["usage"] == {"include": True} and first["reasoning"] == {"effort": "high"})
names = {t["function"]["name"] for t in first["tools"] if t["type"] == "function"}
check("1. client tools rendered as functions", {"bash", "read_file", "edit_file", "write_file", "glob", "grep", "http_request",
                                                 "memory", "skill", "delegate", "ask_user", "update_todos"} <= names, names)
check("1. web search/fetch as OpenRouter server tools",
      {"type": "openrouter:web_search"} in first["tools"] and {"type": "openrouter:web_fetch"} in first["tools"])
check("1. max_tokens capped by the model's limit", first["max_tokens"] == 32000)
sysmsg = first["messages"][0]
check("1. system prompt cached (anthropic/* model)", sysmsg["role"] == "system" and sysmsg["content"][0]["cache_control"] == {"type": "ephemeral"})
check("1. newest message carries the moving cache breakpoint", first["messages"][-1]["content"][-1].get("cache_control") == {"type": "ephemeral"})
check("1. ... without editing the stored history", s.history[0]["content"] == "check the app")
second = b[1]["messages"]
assistant = second[-3]
check("1. assistant tool calls replayed with full arguments",
      [c["id"] for c in assistant["tool_calls"]] == ["c1", "c2"]
      and json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"path": "app.conf"}, assistant)
details = assistant.get("reasoning_details") or [{}]
check("1. reasoning_details merged and passed back unchanged (text + signature)",
      len(details) == 1 and details[0]["text"] == "I should read both files first." and details[0]["signature"] == "sig-abc", details)
check("1. one tool message per call, in order",
      [(m["role"], m["tool_call_id"]) for m in second[-2:]] == [("tool", "c1"), ("tool", "c2")] and "port = 80" in second[-2]["content"])
check("1. reasoning streamed to the UI as thoughts", ("thought", "I should read b") in ui.events or any(e[0] == "thought" for e in ui.events))
check("1. the command needed (and got) approval", any(e[0] == "approve" and e[1] == "c3" for e in ui.events))
check("1. exact cost from OpenRouter's usage", abs(s.cost - 0.035) < 1e-9, s.cost)
check("1. session saved", s.store.load(s.id)["provider"] == "openrouter")
s.close()

# ---- 2. HeadlessUI refuses approvals and tells the model
reset(tool_reply([("h1", "bash", {"command": "make build"})]), text_reply("I could not build; run make build."))
s = session(HeadlessUI(quiet=True))
engine.run_turn(s, "build")
result = fake.bodies()[1]["messages"][-1]
check("2. headless: approval refused, the model is told why",
      result["role"] == "tool" and body_text(result).startswith("ERROR: ") and "non-interactive" in body_text(result), result)
check("2. the newest tool message carries the cache breakpoint as a text part",
      result["content"][-1].get("cache_control") == {"type": "ephemeral"} and "cache_control" not in result)
s.close()

# ---- 3. images from a tool go in a user message right after the tool results
reset(tool_reply([("i1", "read_file", {"path": "shot.png"})]), text_reply("A red pixel."))
s = session()
engine.run_turn(s, "what's in shot.png?")
msgs = fake.bodies()[1]["messages"]
check("3. tool message is text", msgs[-2]["role"] == "tool" and isinstance(msgs[-2]["content"], str))
check("3. image follows as image_url data URL",
      msgs[-1]["role"] == "user" and msgs[-1]["content"][0]["image_url"]["url"].startswith("data:image/"), msgs[-1])
s.close()

# ---- 4. errors: 429 retried, 400 'reasoning' retried without it, 401 is an auth error, mid-stream error
reset(429, text_reply("ok after retry"))
s = session()
check("4. 429 retried", engine.run_turn(s, "hi")[0] == "ok after retry" and len(fake.requests) == 2)
reset((400, "reasoning is not supported for this model"), text_reply("ok without reasoning"))
check("4. 400 about reasoning: retried without the reasoning parameter",
      engine.run_turn(s, "hi")[0] == "ok without reasoning" and "reasoning" not in fake.bodies()[1])
reset((401, "No auth credentials found"))
try:
    engine.run_turn(s, "hi")
    err = None
except ProviderError as e:
    err = e
check("4. 401 raises an auth ProviderError (no retries)", err and err.auth and len(fake.requests) == 1, err)
reset([{"error": {"message": "upstream exploded", "code": 502}}], text_reply("recovered"))
check("4. mid-stream error chunk is retried", engine.run_turn(s, "hi")[0] == "recovered")
s.close()

# ---- 4b. a stream that only sends keep-alives is abandoned and retried
reset({"stall": 4}, text_reply("after the stall"))
s = session(stall_seconds=1)
started = time.monotonic()
text, _ = engine.run_turn(s, "hi")
check("4b. stalled stream detected and retried", text == "after the stall" and len(fake.requests) == 2
      and time.monotonic() - started < 4, (text, len(fake.requests), time.monotonic() - started))
check("4b. the stall is recorded as a retry", "stalled" in s.ledger.path.read_text())
s.close()

# ---- 5. model quirks: missing tool-call ids, fragmented arguments, length stop, non-Anthropic model
quirky = tool_reply([("", "glob", {"pattern": "*.conf"})])
reset(quirky, text_reply("cut off", finish="length"))
s = session(model="openai/gpt-5.2", openrouter={**config(fake.url)["openrouter"], "fallback_models": ["anthropic/claude-sonnet-5.5"]})
text, stop = engine.run_turn(s, "find configs")
call = fake.bodies()[1]["messages"][-2]["tool_calls"][0]
check("5. a missing tool-call id is generated and used for the result",
      call["id"].startswith("call_") and fake.bodies()[1]["messages"][-1]["tool_call_id"] == call["id"])
check("5. finish_reason length -> max_tokens", stop == "max_tokens")
first = fake.bodies()[0]
check("5. no cache_control for non-Anthropic models", isinstance(first["messages"][0]["content"], str)
      and "cache_control" not in json.dumps(first["messages"]))
check("5. fallback models sent as `models`", first["models"] == ["openai/gpt-5.2", "anthropic/claude-sonnet-5.5"])
s.close()

# ---- 6. compaction when the prompt grows past compact_at_tokens
reset(tool_reply([("k1", "read_file", {"path": "app.conf"})], prompt=5000),
      text_reply("Summary: user wants the port; app.conf says 80."),
      text_reply("The port is 80."))
s = session(compact_at_tokens=1000)
text, _ = engine.run_turn(s, "what port?")
b = fake.bodies()
check("6. summary requested with tools disabled", b[1]["tool_choice"] == "none" and b[1]["messages"][-1]["content"][-1]["text"] == SUMMARY_PROMPT)
check("6. summary request has no reasoning", "reasoning" not in b[1])
check("6. work continues from the summary", text == "The port is 80." and "Summary: user wants the port" in json.dumps(b[2]["messages"]))
check("6. history replaced by the compacted form", len(s.history) == 2 and "compacted" in s.history[0]["content"])
s.close()

# ---- 7. cancel from another thread (an app's stop button) mid-stream
class Canceller(Recorder):
    def text(self, delta, indent=""):
        super().text(delta, indent)
        holder[0].interrupt()


holder = []
reset(text_reply("a long answer that will be cut"), text_reply("fine"))
s = session(Canceller())
holder.append(s)
check("7. interrupt mid-stream cancels the turn", engine.run_turn(s, "talk") == ("", "cancelled"))
s.ui = Recorder()
s.registry.ui = s.ui
check("7. the next turn works", engine.run_turn(s, "again")[0] == "fine")
s.close()

# ---- 8. main-agent tools through OpenRouter: memory, update_todos, ask_user without a UI answer
reset(tool_reply([("m1", "memory", {"command": "create", "path": "/memories/port.md", "file_text": "port 80"}),
                  ("m2", "update_todos", {"todos": [{"content": "Check port", "status": "completed"}]}),
                  ("m3", "ask_user", {"question": "Which port?", "options": ["80", "8080"]})]),
      text_reply("Remembered."))
ui = Recorder()
s = session(ui, mode="auto")
engine.run_turn(s, "remember the port")
tool_msgs = [m for m in fake.bodies()[1]["messages"] if m["role"] == "tool"]
check("8. memory file written under the data dir", (work / "data/memories/port.md").read_text() == "port 80")
check("8. to-dos reached the UI", ("todos", [{"content": "Check port", "status": "completed"}]) in ui.events)
check("8. ask_user without an answer says so", "No answer is available" in body_text(tool_msgs[2]))
s.close()

# ---- 9. stopping mid-task: sent back to unfinished work (at most twice), not when asking the user
todo = lambda status: {"todos": [{"content": "Read data", "status": "completed"}, {"content": "Write report", "status": status}]}  # noqa: E731
reset(tool_reply([("u1", "update_todos", todo("in_progress"))]),
      text_reply("Numbers check out. Let me now write the report."),
      tool_reply([("u2", "update_todos", todo("completed"))]),
      text_reply("Report written to output/report.html."))
s = session()
final, _ = engine.run_turn(s, "analyse")
nudge = body_text(fake.bodies()[2]["messages"][-1])
check("9. early stop with open to-dos -> nudged to continue", "[agent runtime]" in nudge and "Write report" in nudge, nudge)
check("9. ... and the work finishes", final == "Report written to output/report.html." and len(fake.requests) == 4)
reset(tool_reply([("u3", "update_todos", todo("pending"))]), text_reply("Let me continue."), text_reply("Let me continue."),
      text_reply("Let me continue."))
final, _ = engine.run_turn(s, "again")
check("9. at most two nudges per request", len(fake.requests) == 4 and final == "Let me continue.", len(fake.requests))
reset(tool_reply([("u4", "update_todos", todo("pending"))]), text_reply("Should I overwrite the old report?"))
engine.run_turn(s, "and again")
check("9. a question to the user is not nudged", len(fake.requests) == 2)
check("9. nudges are recorded in the ledger", s.ledger.path.read_text().count("continue_nudge") == 3)
s.close()

fake.close()
done()
