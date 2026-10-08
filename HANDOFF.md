# Handoff: the unified agent layer (for Reflick on macOS)

This repo is the **agent layer**: model access, the tool loop, tools, permissions, hooks, skills,
sessions and an audit log. Reflick (the Mac app) should not reimplement any of that. It starts
`agent.py --acp` as a child process and talks the **Agent Client Protocol** to it, the same way
Zed talks to Claude Code, Gemini CLI or Hermes.

```
 Reflick (Swift)                       agent.py --acp  (Python, this repo)
 ───────────────                       ─────────────────────────────────────────────
 chat UI, approvals,   JSON-RPC 2.0    acp_server.py   ACPUI: engine events → session/update
 plan view, tool cards ◄────────────►  engine.py       run_turn → agent_loop (plan → act → observe)
                       over stdio      registry.py     validate → policy → hooks → approve → run → ledger
                                       toolset.py      bash, files, grep, http, MCP, memory, skills, subagents
                                       providers.py    OpenRouter (default) | Anthropic
```

## 1. Running it on a Mac

```bash
brew install python@3.12 ripgrep imagemagick     # rg for grep; ImageMagick shrinks screenshots
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
echo 'sk-or-...' > ~/.config/sysadmin-agent/openrouter.key && chmod 600 ~/.config/sysadmin-agent/openrouter.key
.venv/bin/python agent.py            # check it works in the terminal first
tests/run_all.sh                     # all suites should pass (no API key needed)
```

## 2. Talking to it from Swift

Spawn the process with `Process`, pipe stdin/stdout, and exchange **one JSON object per line**
(newline-delimited JSON-RPC 2.0). stderr is free-form logging — show or discard it, never parse it.
Look for an ACP client library for Swift at agentclientprotocol.com before writing your own; the
protocol is small enough to hand-roll if there isn't one.

These are real messages captured from this agent (session ids shortened):

**Start up**
```json
→ {"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}
← {"jsonrpc":"2.0","id":0,"result":{"protocolVersion":1,"agentCapabilities":{"loadSession":true,
   "promptCapabilities":{"image":true,"embeddedContext":true},"sessionCapabilities":{"list":{},"close":{}}},
   "agentInfo":{"name":"sysadmin-agent","title":"Unified agent","version":"0.3.0"}}}
→ {"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":"/Users/you/project","mcpServers":[]}}
← {"jsonrpc":"2.0","id":1,"result":{"sessionId":"2026-10-08_123842-ee4e","modes":{"currentModeId":"ask",
   "availableModes":[{"id":"read-only",...},{"id":"ask",...},{"id":"edits",...},{"id":"auto",...}]}}}
```

**A prompt** — the agent streams `session/update` notifications, then answers the request:
```json
→ {"jsonrpc":"2.0","id":2,"method":"session/prompt","params":{"sessionId":"…","prompt":[{"type":"text","text":"what OS is this?"}]}}
← {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"…","update":{"sessionUpdate":"tool_call",
   "toolCallId":"call_1","title":"$ uname -s","kind":"execute","status":"in_progress"}}}
← {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"…","update":{"sessionUpdate":"tool_call_update",
   "toolCallId":"call_1","status":"completed","content":[{"type":"content","content":{"type":"text","text":"Linux\n\n[exit code 0]"}}]}}}
← {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"…","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"You're "}}}}
← ... more chunks ...
← {"jsonrpc":"2.0","id":2,"result":{"stopReason":"end_turn"}}
```

**Approval** — the agent sends *you* a request and waits for the user's choice:
```json
← {"jsonrpc":"2.0","id":0,"method":"session/request_permission","params":{"sessionId":"…",
   "toolCall":{"toolCallId":"call_1","status":"pending","title":"$ touch notes.txt",
               "content":[{"type":"content","content":{"type":"text","text":"Asking because: ask mode"}}]},
   "options":[{"optionId":"allow","name":"Allow","kind":"allow_once"},
              {"optionId":"allow_all","name":"Allow all `touch` commands this session","kind":"allow_always"},
              {"optionId":"reject","name":"Reject","kind":"reject_once"}]}}
→ {"jsonrpc":"2.0","id":0,"result":{"outcome":{"outcome":"selected","optionId":"allow"}}}
```
For file edits the `content` text is the diff. `allow_all` is only offered when it's safe to grant.
Parallel subagents can have several permission requests open at once; their titles start with the
subagent's name (`[tests] $ npm test`), and their tool calls arrive as ordinary `tool_call` updates.
Background subagents keep going after `delegate` returns. A prompt only finishes (`stopReason`) once
they're done and the main agent has used their reports, and `session/cancel` stops them too.

**Everything else**

| Method | Use |
|---|---|
| `session/cancel` (notification) | Stop button. Kills the running command; the prompt returns `stopReason: "cancelled"`. If a permission request is open, answer it `{"outcome":{"outcome":"cancelled"}}` |
| `session/set_mode` | Switch `read-only` / `ask` / `edits` / `auto`; the model is told on its next turn |
| `session/list`, `session/load` | History sidebar; `load` replays the conversation as `user_message_chunk` / `agent_message_chunk` |
| `session/close` | Free a session's shell |

Update kinds to render: `agent_message_chunk` (the answer, markdown), `agent_thought_chunk`
(progress/reasoning — show dimmed or collapsible), `tool_call` / `tool_call_update` (cards; `kind`
is `execute`, `read`, `edit`, `search`, `fetch`, `think` or `other`), `plan` (the to-do list).
Prompts can include images (`{"type":"image","mimeType":"image/png","data":"<base64>"}`) — e.g.
a screenshot the user drops in.

`tests/test_acp.py` drives every one of these against the real process; use it as the reference
client when something on the Swift side doesn't behave.

## 3. macOS specifics to finish

- **sudo**: in ACP mode there is no terminal, so `sudo` can't ask for a password and the command
  fails. Options, best first: (a) leave root work to the user — the agent already says exactly
  what to run; (b) have Reflick run `osascript -e 'do shell script "…" with administrator privileges'`
  after the approval card (macOS shows its own password dialog; the agent never sees it); (c) a
  privileged helper (SMAppService) for a fixed set of operations. Never pass the password through
  the agent.
- **Process tree / cancel**: `shell.py` reads `/proc` on Linux and falls back to `pgrep -P` on
  macOS. Written, not yet run on a Mac — `tests/test_shell.py` covers it.
- **Screenshots**: the prompt tells the model to use `screencapture -x /tmp/screen.png` on macOS;
  the first run triggers the Screen Recording permission for whichever app spawned the agent.
- **Shell**: `/bin/bash` on macOS is 3.2; the marker protocol in `shell.py` only uses 3.2
  features. Users who live in zsh still get bash for the agent (deliberate: predictable output).
- **Sandboxing**: if Reflick ships through the App Store sandbox, a child process inherits the
  sandbox and can't touch most of the disk. Ship outside the store (Developer ID + notarization)
  or embed the agent as a separately signed helper.
- **Packaging**: simplest is a venv in `~/Library/Application Support/Reflick/agent`; later,
  bundle with PyInstaller/briefcase so users don't need Python.

## 4. Extending it

- **A tool**: one `reg.add(Tool(...))` in `toolset.py` — name, description, JSON schema, `run`,
  `risk` (decides the policy class), `acp_kind` (decides the icon in apps). Both providers and
  every UI pick it up.
- **A provider**: implement the interface `OpenRouterProvider` has (`render_tools`, `user`,
  `system_note`, `tool_results`, `stream` → `Turn`, `needs_compaction`/`compact`, `dump`/`load`)
  and add it to `make_provider`.
- **A front end** (Telegram bot, menu-bar app, web): subclass `ui.UI` and call
  `engine.run_turn(session, text)`. `acp_server.py` is the worked example.
- **Skills**: drop a folder with a `SKILL.md` into `~/.config/sysadmin-agent/skills/` (or
  `./.agent/skills/`). Claude Code skills in `~/.claude/skills/` are picked up too.
- **Hooks**: org rules without code changes — see `hooks.py` and `config.example.json`.

## 5. Where the ideas came from

| From | Taken | Left out (for now) |
|---|---|---|
| Claude Agent SDK / Claude Code | tool set and semantics, permission modes, hooks contract (exit 2 = block, incl. SubagentStart/Stop), sessions/resume, headless `-p --json`, named subagent types with their own model, background subagents, parallel batching of safe tool calls, skills | the SDK itself (it drives the Claude Code CLI and is Anthropic-only; we needed OpenRouter) |
| Hermes Agent | OpenRouter handling: reasoning_details round-trip, cache breakpoints (text parts on tool messages, never a top-level marker), ACP adapter shape, compaction, skills; subagent concurrency cap, timeouts, structured results with partial output, report trimming, children barred from ask/memory/delegate | 7 terminal backends, messaging gateways, RL/trajectory tooling |
| OpenClaw | exec approvals per command, gateway ↔ native-app split (the model for Reflick), SKILL.md skills; subagent list/stop/wait, status line on results | the always-on gateway daemon and channel integrations (WhatsApp/Telegram/…) — add as new UIs when needed |

## 6. Before the first real run

1. Put the OpenRouter key in `~/.config/sysadmin-agent/openrouter.key` (chmod 600).
2. `agent.py -p "say hi" --json` — confirms the key, the model id and pricing lookup.
3. Try a real task in `ask` mode and read the ledger (`/ledger`) afterwards.
