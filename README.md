# sysadmin-agent

A general-purpose agent for your own computer. It runs the plan → act → observe → decide loop with
the tools a coding agent has; every tool call goes through one policy check and into an audit
ledger. One engine, three ways to drive it:

| Front end | How | For |
|---|---|---|
| Terminal chat | `agent.py` | you, at the keyboard |
| Headless | `agent.py -p "task" [--json]` | scripts, cron (`--schedule`) |
| **ACP server** | `agent.py --acp` | apps: **Reflick** (macOS), Zed, any Agent Client Protocol client |

Models come from **OpenRouter** by default (any of its tool-capable models; default
`anthropic/claude-opus-5.5`), or straight from the Anthropic API with `--provider anthropic`.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p ~/.config/sysadmin-agent
cp config.example.json ~/.config/sysadmin-agent/config.json     # optional
```

API key, any one of: a `.env` file next to `agent.py` containing `OPENROUTER_API_KEY=sk-or-...`
(`chmod 600 .env`), the file `~/.config/sysadmin-agent/openrouter.key`, or an exported
`OPENROUTER_API_KEY`. The key is read into memory only; the agent's own shell never sees it, and
reading `.env` with its tools needs your approval. For the Anthropic provider, export `ANTHROPIC_API_KEY`.

## Run

```bash
.venv/bin/python agent.py                              # chat
.venv/bin/python agent.py -p "is the disk filling up?"  # one-shot; approvals are refused
.venv/bin/python agent.py -c                           # continue the last session (-r ID for a specific one)
.venv/bin/python agent.py --model openai/gpt-5.2       # any OpenRouter model id
.venv/bin/python agent.py --schedule "0 9 * * *" -p "summarize overnight errors in journalctl"
.venv/bin/python agent.py --acp                        # serve ACP on stdin/stdout
```

Chat commands: `/mode [name]`, `/tools`, `/skills`, `/sessions`, `/cost`, `/memory`, `/ledger`,
`/clear`, `/help`, `/exit`. **Ctrl+C while it works cancels that request**; Ctrl+C at the prompt quits.
Mention an image path (or drag a screenshot into the terminal) to show it to the agent.
Standing instructions (like a CLAUDE.md): `~/.config/sysadmin-agent/AGENT.md`.

## Tools

| Tool | What it does |
|---|---|
| `bash` | Persistent shell (`cd`, `export`, venvs carry over). Secret env vars are removed. Approved commands stream their output live |
| `read_file` | Text with line numbers; opens images (screenshots shrunk to ≤2000px) |
| `edit_file` / `write_file` | Exact-string edits / whole files, shown as a diff; read-before-write and stale-file checks; originals backed up |
| `glob` / `grep` | Find files / search contents (ripgrep); matches inside secret files are withheld |
| `http_request` | REST calls with JSON; **credentials by name only** (attached for allowed hosts, never shown, never forwarded on redirects) |
| `web_search` / `web_fetch` | Run server-side by the provider (OpenRouter server tools, or Anthropic's) |
| `memory` | Notes that persist across sessions |
| `skill` | Loads a skill's instructions on demand (SKILL.md folders - same format as Claude Code / Hermes / OpenClaw) |
| `delegate` / `subagents` | Child agents, each with its own shell, that report back — see **Subagents** below |
| `ask_user` / `update_todos` | Questions with options / a visible checklist (a "plan" in ACP apps) |
| `mcp__<server>__<tool>` | Every tool from the MCP servers in your config |

Independent read-only calls in one response run in parallel.

## Subagents (subagents.py)

The main agent hands tasks to child agents, like Claude Code's Agent tool, Hermes's
`delegate_task` and OpenClaw's `sessions_spawn`:

- **Parallel**: several `delegate` calls in one response run at the same time (at most
  `subagents.max_parallel`, default 4). Each gets its own bash shell and sees only its task.
- **Research or worker**: by default a subagent only takes actions that need no approval. With
  `can_change` it may run commands and edit files, and each approval comes to you labelled `[name]`.
- **Background**: `background: true` returns at once. The report reaches the main agent with a
  later tool result, and the turn waits for any still running before it ends. The `subagents` tool
  can list them, wait for some, or stop one early.
- **Types**: put `.agent/agents/<name>.md` (or `~/.config/sysadmin-agent/agents/`, or `agents`
  in config) with frontmatter `name`, `description`, `model`, `effort`, `can_change`, and the role
  instructions as the body. `subagent_model` sets a cheaper default model for all subagents.
- **Limits**: a time limit (`subagents.timeout_seconds`, default 900), a round limit, and the
  per-request cost budget covering subagents too. Results start with a status line
  (`[subagent logs · completed · 12 rounds · 30 tool calls · 41s · $0.13]`). Stopped subagents
  return a partial report, and reports over 24k characters are cut, with the full text saved
  to a file.
- **Hooks**: `SubagentStart` (exit 2 stops it from starting; output is added to its task) and
  `SubagentStop`.

Subagents can't delegate further, ask you questions, write memory or edit the to-do list.

## Permissions (policy.py)

Every call gets **allow**, **ask** or **deny**, in this order:
1. your `rules` — `"bash:git status*"`, `"edit_file:/etc/*"`, `"mcp__playwright__*"` (deny > ask > allow)
2. hard denies — wiping disks or the root/home folder, cloud metadata addresses
3. the **mode**: `read-only` (changes refused) · `ask` (default: every change asks) · `edits`
   (file edits in your home run automatically) · `auto` (unattended; only risky actions ask)
   Read-only commands always run, even in read-only mode: whole pipelines and `;`/`&&` chains
   in which every part only reads (`journalctl -b | grep … | sort | uniq -c`, `systemctl
   --failed`, `git log`, `nvidia-smi`, `sed -n`). Write redirects, `$(…)`, `xargs`, `-exec` and
   subcommands that change things are excluded.
4. always-ask escalations in every mode: secret-holding paths, risky commands (sudo, `curl | sh`,
   `rm -r`, installs, service changes, force-push…), files outside your home, HTTP requests that
   send data, MCP tools marked destructive
5. "yes to all" grants given at a prompt — never for sudo, rm, shells or interpreters

**Hooks** (`hooks` in config, same contract as Claude Code's): your scripts run on
`PreToolUse`, `PostToolUse`, `UserPromptSubmit` and `Stop`; exit code 2 blocks (or, for `Stop`,
sends the agent back to work). See hooks.py.

## Records

`~/.local/share/sysadmin-agent/sessions/`: `<id>.jsonl` logs every model round (tokens, exact
cost) and tool call (input, decision, your answer, duration, result); `<id>.session.json` holds
the conversation for resuming. Both are readable only by you.

## Reliability

Retries with backoff (429/5xx, dropped connections) · **stalled responses are detected and retried**
(`stall_seconds`, default 300: OpenRouter's keep-alives otherwise hide a dead stream) · per-request
cost budget (subagents included) · a warning to the model after the same call fails 3 times ·
**early-stop guard**: if the agent ends its turn with unfinished to-dos, or announces a next
step and stops, it's sent back to work (at most twice per request) · **nothing the agent starts
outlives its shell**: background jobs and servers are found by an environment tag and stopped,
even if they were re-parented · conversation compaction for long sessions · prompt caching ·
reasoning passed back unchanged between tool calls · model fallbacks.

## Tests

```bash
tests/run_all.sh
```

286 checks across 11 suites: shell, policy, tools, HTTP and MCP against real local servers, the
engine end to end on both providers (a scripted fake Anthropic API, and a local fake OpenRouter
server speaking the real streaming protocol), hooks/skills/CLI, subagents (parallel, background, types,
limits, hooks, approvals, cancel), the ACP server driven as a subprocess the way an app drives it, and the terminal/sudo prompt.
No test calls a real model, so the suite needs no API key. Live runs against real models on OpenRouter are recorded in [AUDIT.md](AUDIT.md) (section D).

## Files

| File | Role |
|---|---|
| `engine.py` | Session + the agent loop (`run_turn`, `agent_loop`, `delegate`) |
| `providers.py` | OpenRouter and Anthropic wire formats behind one interface |
| `toolset.py` | Every tool, defined once |
| `registry.py` | The one path every tool call takes |
| `ui.py` / `acp_server.py` | Terminal + headless UIs / the ACP server |
| `agent.py` | Command line |
| `policy.py`, `hooks.py` | Permissions, user hooks |
| `prompts.py`, `skills.py`, `sessions.py`, `ledger.py` | Prompts, skills, saved sessions, audit log |
| `shell.py`, `tools.py`, `http_tool.py`, `mcp_client.py` | The tools' implementations |

Architecture and the Reflick/macOS handoff: **[HANDOFF.md](HANDOFF.md)**.

For coding agents: start with **[AGENTS.md](AGENTS.md)**. A knowledge graph of the codebase (made with graphify) is in `graphify-out/`: `GRAPH_REPORT.md` to read, `graph.json` to query, and `graph.html` to explore in a browser.

## Known limits

- Background jobs (`cmd &`) that print later can leak output into the next command's result.
- A hung sudo command can't be killed on timeout (it runs as root). In headless/ACP mode sudo has no terminal to ask on — see HANDOFF.md.
- Full-screen programs (`vim`, `top`) don't work; commands get no keyboard input.
- HTTP address checks resolve DNS once (DNS rebinding isn't caught).
- OpenRouter compaction is a simple summary-and-restart; Anthropic's is server-side.
- Linux-tested; the macOS paths (pgrep, screencapture) are written but not yet run on a Mac.
