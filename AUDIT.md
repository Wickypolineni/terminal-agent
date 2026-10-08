# Capability audit — terminal-agent

Status as of 2026-10-08. This replaces the first audit, which covered the original three-file
Claude-only prototype. Everything it listed as P0 or P1 has since been built (section C).

**What the labels mean**
- **TESTED**: the offline suite exercises it (`tests/run_all.sh`, 286 checks across 11 suites).
  The model in those tests is a scripted fake: a fake Anthropic API, or a local fake OpenRouter
  server that speaks the real streaming protocol. These tests prove the runtime's mechanics, not
  the model's judgement.
- **LIVE**: it has also been seen working against a real model through OpenRouter, in the live
  runs listed in section D.
- **UNTESTED**: written, but not exercised by either.

---

## A. Architecture

About 4,000 lines of Python. One engine, with three ways to drive it.

```text
 terminal chat (ui.TerminalUI) ─┐
 headless -p / cron (HeadlessUI) ┼─► engine.run_turn ─► agent_loop: stream → tools → results → repeat
 apps over ACP (acp_server.ACPUI)┘        │                │
                                          │                ├─ providers.py  OpenRouter (default) | Anthropic
                                          │                ├─ registry.py   validate → policy → hooks → approve → run → ledger
                                          │                │                parallel batches of safe calls
                                          │                └─ toolset.py    every tool, defined once
                                          ├─ subagents.py   delegate / subagents: parallel, background, types
                                          ├─ policy.py      modes, rules, read-only classifier, secret paths
                                          ├─ hooks.py · skills.py · sessions.py · ledger.py · prompts.py
                                          └─ shell.py · tools.py · http_tool.py · mcp_client.py
```

---

## B. Capability matrix

| Capability | Status | Where | Remaining gaps |
|---|---|---|---|
| Agent loop | LIVE | `engine.agent_loop` | — |
| Model providers | LIVE (OpenRouter: Opus 5.5, Muse Spark 1.3 Contributor) · TESTED (Anthropic direct) | `providers.py` | Anthropic direct never run live |
| Streaming, retries, stall detection | TESTED · retries LIVE | `OpenRouterProvider.stream` | The stall detector was added after a real 8-minute hang; it has not yet fired live |
| Reasoning round-trip, prompt caching | TESTED · caching LIVE on Claude models | `providers.py` | Non-Claude models don't get explicit cache breakpoints |
| Shell | LIVE | `shell.py`: persistent session, secret-free environment, timeouts, cancel; background jobs found by an environment tag and killed with the shell | Background jobs that print later can leak output into the next result; no sandbox (it runs on your machine) |
| Files and search | LIVE | `tools.py`: read-before-write, stale check, diff approval, backups, images; grep withholds matches in secret files | — |
| HTTP | TESTED | `http_tool.py`: credentials passed by name, redirect and metadata-address guards | DNS rebinding isn't caught |
| Web search / fetch | UNTESTED live | OpenRouter / Anthropic server tools | Never run against a real model |
| MCP | TESTED | `mcp_client.py`, against a real local MCP server | No real third-party server tried |
| Permissions | LIVE | `policy.py`: 4 modes, allow/ask/deny rules, hard denies, risky escalations, secret paths, grants, read-only classifier for pipelines, chains and subcommands | Rules match command text; there's no argv-level execution |
| Hooks | TESTED | `hooks.py`: PreToolUse, PostToolUse, UserPromptSubmit, Stop, SubagentStart, SubagentStop | — |
| Skills | LIVE (the model loaded `data-analysis` without being told to) | `skills.py`: SKILL.md format, shared with Claude Code, Hermes and OpenClaw | — |
| Subagents | LIVE (parallel research; parallel workers with approvals) · TESTED (background, types, list/stop/wait, limits, hooks) | `subagents.py` | No nesting, worktrees, steering a running child, or resuming after a restart |
| Memory | LIVE | memory tool (SDK backend) | — |
| Sessions and resume | TESTED | `sessions.py`, `-c` / `-r`, ACP `session/load` | — |
| Context management | TESTED | Anthropic server-side compaction; OpenRouter summary compaction | A real compaction has never been triggered live |
| Cancellation | TESTED | Ctrl+C or ACP `session/cancel` stops the turn and every subagent and command; history stays valid | — |
| Budgets and loop guards | TESTED · LIVE | Per-request cost budget (subagents included), round limits, repeated-failure warning, early-stop nudge (fired live) | — |
| Ledger and observability | LIVE | `ledger.py`: every model round (tokens, exact cost, model) and every tool call (decision, approval, duration) | — |
| Headless, cron | TESTED · `-p` LIVE | `agent.py -p`, `--schedule` | — |
| App integration (ACP) | TESTED (subprocess, wire format captured) | `acp_server.py` | Not yet driven by a real app or a live model |
| macOS | UNTESTED | `pgrep` / `ps -E` fallbacks, `screencapture`, bash 3.2-safe shell | sudo has no terminal in ACP mode (see HANDOFF.md) |
| Browser automation | Through MCP only (e.g. Playwright) | config | Untested |

---

## C. The first audit's gaps, now closed

| First audit | Now |
|---|---|
| P0: the shell leaked API keys | Environment scrubbed (`clean_env`), tested |
| P0: `cd` then a relative path bypassed the secret check | Arguments resolved against the shell's cwd, tested |
| P0: grep returned secret contents | Matches in secret files withheld, tested |
| P0: incomplete sensitive-path list | Expanded, tested |
| P1: Ctrl+C killed the program | Cancels the turn only, tested |
| P1: rules scattered across 4 places | One `policy.py` with modes and rules |
| P1: no ledger or transcript | `ledger.py` plus resumable sessions |
| P1: static tool list | Registry, MCP, skills |
| P1: no HTTP tool | `http_request` with credentials by name |
| P1: no API retry | Retries with backoff, plus stall detection |
| P1: no prompt-injection stance | In the system prompt; enforced by policy |
| P2: parallel reads, budgets, failure detector | Done |

---

## D. Live runs (OpenRouter, 2026-10-08)

| Run | Model | Result |
|---|---|---|
| Health check, 3 parallel research subagents | Opus 5.5 | First run: subagents ran one at a time, 6.5 min, $1.62. Fixed by batching parallel-safe calls and a better read-only classifier. Second run: 2.2 min, $0.75 |
| Fix a bug and add a CLI with 2 worker subagents (approvals) | Opus 5.5 / Haiku 5.5 | All tests pass, plus hidden tests; 92 s, $0.19 |
| Same task | Muse Spark 1.3 Contributor (main and subagents) | All tests pass, plus hidden tests; 55 s, $0.009 |
| Open-ended Excel/CSV analysis, HTML report with charts | Muse Spark 1.3 Contributor | Run 1: hung (stall bug, fixed). Run 2: left a web server listening on the network (process-leak bug, fixed) and missed 2 of 6 planted trends (led to the `data-analysis` skill). Run 3: stopped half-way (led to the early-stop nudge). Run 4: all 6 planted findings, 9 embedded charts, 4.75 min, $0.066 |

**Lesson:** a cheaper model gets there, but it leans on the guardrails: skills, the early-stop
nudge and stall detection. Opus needed none of them.

---

## E. Open items, in priority order

1. Run web search and a real compaction live.
2. Drive the ACP server from a real client (Zed, then Reflick) with a live model.
3. macOS pass: run the test suite on a Mac; decide how to handle sudo (osascript or a privileged helper).
4. A sandbox option (Docker or another user account) for unattended runs in `auto` mode.
5. Subagent extras if needed: worktree isolation, steering a running child, resuming after a restart.
6. argv-level execution, so policy rules match commands exactly.

---

## F. Reflick integration

The plan changed from the first audit. Instead of porting these designs to Swift, Reflick starts
`agent.py --acp` as a child process and talks the Agent Client Protocol to it. Then the loop,
tools, policy, ledger, skills and subagents all stay in one tested place. **HANDOFF.md** has the
real wire messages, the macOS notes and an extension guide.

The first audit's notes on the Reflick codebase (an existing `ToolRegistry`, approvals and a
ledger; no `CentralPolicyEngine` or `ShellProcessRunner`) came from a quick look at file names
only. They are still unverified.
