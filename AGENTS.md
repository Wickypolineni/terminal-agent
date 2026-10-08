# AGENTS.md — guide for coding agents working on this repo

A general-purpose agent for your own computer: a model-driven tool loop with a persistent shell,
file, search, HTTP and MCP tools, a permission policy, hooks, skills, subagents and an audit
ledger. Users drive it from a terminal (`agent.py`), headless (`agent.py -p`), or from apps over
the Agent Client Protocol (`agent.py --acp`), which is how Reflick uses it.

## Find your way around

- **`graphify-out/GRAPH_REPORT.md`** — a knowledge graph of this codebase:
  - 20 communities (one per subsystem)
  - the most-connected "god nodes": `Session`, `TerminalUI`, `FileTools`, `UI`, `OpenRouterProvider`,
    `build_registry`, `Call`, `Shell`, `SubagentManager`, `Registry`
  - cross-module links
- `graphify-out/graph.json` — the same graph as data. Query it with `graphify query "<question>"`,
  or load it with networkx.
- `graphify-out/graph.html` — an interactive view.
- Regenerate after structural changes with `graphify . --update` (needs `uv tool install graphifyy`).
- Prose docs: `README.md` (use), `HANDOFF.md` (ACP wire protocol, macOS), `AUDIT.md`
  (capabilities, live results, open items).

## How a request flows

```text
agent.py / acp_server.py ─► engine.run_turn ─► engine.agent_loop
   provider.stream (providers.py)  ─►  registry.execute_round (registry.py)
      validate input ─► policy.evaluate (policy.py) ─► PreToolUse hooks ─► ui.approve ─► tool.run
      ─► PostToolUse hooks ─► ui.tool_end ─► ledger.event
   tool results ─► next round, until the model answers without a tool call
```

| To change… | Edit |
|---|---|
| a tool | `toolset.py`: one `reg.add(Tool(...))`. The implementations live in `shell.py`, `tools.py`, `http_tool.py`, `mcp_client.py` |
| what's allowed or asked | `policy.py`: modes, rules, `is_read_only`, the RISKY and CATASTROPHIC lists, secret paths |
| a model provider / wire format | `providers.py`. Implement the `OpenRouterProvider` interface and register it in `make_provider` |
| subagents | `subagents.py` (`SubagentManager`) |
| a front end | subclass `ui.UI`, then call `engine.run_turn` (`acp_server.ACPUI` is the worked example) |
| prompts | `prompts.py` |
| settings and defaults | `engine.DEFAULT_CONFIG`, `config.example.json` |

## Rules that keep it working

1. **The engine never does I/O itself.** No `print()` or `input()` outside `ui.py` and `agent.py`:
   in ACP mode stdout *is* the protocol channel. `policy.py` decides; the UI asks.
2. **Conversation history is append-only.** Never edit or reorder past messages. Reasoning data
   (`reasoning_details`, thinking blocks) must go back to the API exactly as received.
3. **Every tool call needs a result**, including after a cancel, a cut-off or a budget stop
   (`engine._not_run`). Results stay in call order.
4. **Every tool goes through `Registry.execute`**, so policy, hooks, approval and the ledger
   always apply. Never call a tool's implementation directly from the loop.
5. **Secrets never reach the model or the shell.** The shell environment is scrubbed
   (`shell.clean_env`); credentials are passed by name (`http_tool`); grep withholds matches in
   secret files. The OpenRouter key lives in `.env`, which git ignores; never commit it.
6. **Nothing the agent starts may outlive its shell.** `Shell.stop` kills both the process tree
   and anything carrying the shell's environment tag.
7. **Parallel work only runs together when it's safe.** Only consecutive calls that are
   `parallel_safe`, already allowed, and not behind a PreToolUse hook. On the terminal, worker
   threads must not read the keyboard: `TerminalUI` routes their questions to the main thread.

## Tests

```bash
tests/run_all.sh          # 286 checks, 11 suites; no API key, no network
```

The model is always a scripted fake: `tests/harness.FakeAPI` (Anthropic) or
`tests/fake_openrouter.FakeOpenRouter`, a real local HTTP server that speaks OpenRouter's streaming
protocol. Add a test for every behaviour change; run the whole suite before claiming something
works. Thread-related tests (`test_subagents.py`) should pass repeatedly, not just once.

## Conventions

- Python 3.11+, standard library first. Runtime dependencies are pinned in `requirements.txt`.
- Match the surrounding style: small functions, docstrings that explain *why*, comments only
  where the code can't say it.
- User-facing text is plain English: say what happened and what to do next.
