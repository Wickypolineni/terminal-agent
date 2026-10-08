# Graph Report - sysadmin-agent  (2026-10-08)

## Corpus Check
- Corpus is ~34,001 words - fits in a single context window. You may not need a graph.

## Summary
- 597 nodes · 1330 edges · 20 communities (12 shown, 8 thin omitted)
- Extraction: 95% EXTRACTED · 5% INFERRED · 0% AMBIGUOUS · INFERRED: 71 edges (avg confidence: 0.9)
- Token cost: 92,408 input · 0 output

## Community Hubs (Navigation)
- Test Harness and Fakes
- Policy and Tool Registry
- Design Docs and Audit
- CLI and ACP Server
- Model Providers
- Engine Session and Ledger
- Agent Loop and Subagents
- HTTP Tool
- Hooks, Skills and Prompts
- MCP Client
- UI Base Interface
- Terminal UI
- File Tools
- MCP Test Server
- ACP UI Bridge
- OpenRouter Test Helpers
- Recording Test UIs
- Headless UI
- Test Runner Script

## God Nodes (most connected - your core abstractions)
1. `Session` - 30 edges
2. `TerminalUI` - 25 edges
3. `FileTools` - 23 edges
4. `UI` - 23 edges
5. `OpenRouterProvider` - 22 edges
6. `build_registry()` - 22 edges
7. `Call` - 20 edges
8. `Shell` - 20 edges
9. `SubagentManager` - 20 edges
10. `Registry` - 19 edges

## Surprising Connections (you probably didn't know these)
- `session()` --calls--> `Session`  [EXTRACTED]
  tests/test_openrouter.py → engine.py
- `session()` --calls--> `TerminalUI`  [EXTRACTED]
  tests/test_e2e.py → ui.py
- `Recorder` --inherits--> `UI`  [EXTRACTED]
  tests/test_openrouter.py → ui.py
- `Offline Test Suite (286 checks, 11 suites)` --conceptually_related_to--> `Live Runs on OpenRouter (2026-10-08)`  [AMBIGUOUS]
  README.md → AUDIT.md
- `chat()` --uses--> `ProviderError`  [INFERRED]
  agent.py → providers.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Single Tool-call Path: validate, policy, hooks, approve, run, ledger** — readme_registry, readme_policy, readme_hooks, handoff_session_request_permission, readme_sessions_ledger [EXTRACTED 1.00]
- **Three Front Ends Driving One Engine** — readme_terminal_ui, readme_headless_mode, readme_acp_server, readme_engine [EXTRACTED 1.00]
- **Guardrails Cheaper Models Lean On** — readme_skills, readme_early_stop_guard, readme_stall_detection, audit_live_runs [EXTRACTED 1.00]

## Communities (20 total, 8 thin omitted)

### Community 0 - "Test Harness and Fakes"
Cohesion: 0.06
Nodes (32): FakeOpenRouter, do_POST(), text_reply(), tool_reply(), usage(), check(), done(), FakeAPI (+24 more)

### Community 1 - "Policy and Tool Registry"
Cohesion: 0.05
Nodes (28): Ledger, Call, command_paths(), Decision, first_match(), _flag_hit(), is_read_only(), is_sensitive() (+20 more)

### Community 2 - "Design Docs and Audit"
Cohesion: 0.06
Nodes (56): argv-level Execution (planned), Capability Audit (terminal-agent), Capability Matrix, First Audit P0/P1 Gaps (Closed), Live Runs on OpenRouter (2026-10-08), Open Items (priority list), Sandbox Option for Unattended auto Mode (planned), TESTED / LIVE / UNTESTED Status Labels (+48 more)

### Community 3 - "CLI and ACP Server"
Cohesion: 0.06
Nodes (16): _content(), serve(), UnifiedAgent, chat(), handle_command(), list_schedules(), main(), schedule() (+8 more)

### Community 4 - "Model Providers"
Cohesion: 0.06
Nodes (11): AnthropicProvider, blocks_to_keep(), make_provider(), _neutral_to_openai_content(), OpenRouterProvider, ProviderError, _Quiet, _retry_wait() (+3 more)

### Community 5 - "Engine Session and Ledger"
Cohesion: 0.08
Nodes (9): new_session_id(), unfinished(), preview(), clean_env(), process_tree(), Shell, tagged_processes(), alive() (+1 more)

### Community 6 - "Agent Loop and Subagents"
Cohesion: 0.11
Nodes (7): agent_loop(), _not_run(), TurnCancelled, AgentType, Child, load_agent_types(), SubagentManager

### Community 7 - "HTTP Tool"
Cohesion: 0.11
Nodes (5): _CheckedRedirects, classify(), HttpTool, Handler, http_describe()

### Community 8 - "Hooks, Skills and Prompts"
Cohesion: 0.10
Nodes (9): HookResult, Hooks, machine(), main_prompt(), screenshot_hint(), subagent_prompt(), _frontmatter(), Skill (+1 more)

### Community 9 - "MCP Client"
Cohesion: 0.15
Nodes (4): _field(), MCPManager, MCPTool, _safe()

### Community 13 - "MCP Test Server"
Cohesion: 0.18
Nodes (4): echo(), fail(), hints(), wipe()

### Community 15 - "OpenRouter Test Helpers"
Cohesion: 0.21
Nodes (4): config(), body_text(), png(), session()

## Ambiguous Edges - Review These
- `Live Runs on OpenRouter (2026-10-08)` → `Offline Test Suite (286 checks, 11 suites)`  [AMBIGUOUS]
  README.md · relation: conceptually_related_to
- `Sandbox Option for Unattended auto Mode (planned)` → `macOS sudo Handling in ACP Mode`  [AMBIGUOUS]
  AUDIT.md · relation: conceptually_related_to

## Knowledge Gaps
- **9 isolated node(s):** `run_all.sh script`, `bash 3.2-safe Shell Marker Protocol`, `Always-ask Escalations and Hard Denies`, `Background Subagents`, `Terminal Chat UI (ui.TerminalUI)` (+4 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 213 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **8 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Live Runs on OpenRouter (2026-10-08)` and `Offline Test Suite (286 checks, 11 suites)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `Session` connect `CLI and ACP Server` to `Policy and Tool Registry`, `Model Providers`, `Engine Session and Ledger`, `Agent Loop and Subagents`, `HTTP Tool`, `Hooks, Skills and Prompts`, `MCP Client`, `File Tools`, `OpenRouter Test Helpers`?**
  _High betweenness centrality (0.074) - this node is a cross-community bridge._
- **Are the 13 inferred relationships involving `Session` (e.g. with `UnifiedAgent` and `chat()`) actually correct?**
  _`Session` has 13 INFERRED edges - model-reasoned connections that need verification._
- **What connects `run_all.sh script`, `bash 3.2-safe Shell Marker Protocol`, `Always-ask Escalations and Hard Denies` to the rest of the system?**
  _9 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Test Harness and Fakes` be split into smaller, more focused modules?**
  _Cohesion score 0.05536568694463431 - nodes in this community are weakly interconnected._
- **What is the exact relationship between `Sandbox Option for Unattended auto Mode (planned)` and `macOS sudo Handling in ACP Mode`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `UI` connect `UI Base Interface` to `Test Harness and Fakes`, `CLI and ACP Server`, `Terminal UI`, `ACP UI Bridge`, `OpenRouter Test Helpers`, `Recording Test UIs`, `Headless UI`, `UI Module Root`?**
  _High betweenness centrality (0.064) - this node is a cross-community bridge._