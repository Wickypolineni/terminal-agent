"""LLM providers. The engine speaks one neutral interface; each provider owns its wire format.

  OpenRouterProvider  (default) any of OpenRouter's models via its OpenAI-compatible chat API:
                      streaming tool calls, reasoning passed back unchanged between tool calls,
                      prompt caching for Anthropic models, server-side web search/fetch,
                      model fallbacks, exact per-request cost, client-side compaction.
  AnthropicProvider   Claude through the Anthropic API directly, with its native tools
                      (bash/memory/web), server-side compaction and refusal fallbacks.

Each provider keeps the conversation history in its own format (a list of plain messages that
the engine appends to but never edits), so reasoning/thinking data survives exactly as the model
produced it.
"""
import copy
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from registry import ServerTool, Tool, ToolCall, TurnCancelled

RETRY_DELAYS = [2, 5, 15]
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}

SUMMARY_PROMPT = """The conversation is about to be compacted to free up context. Write a summary that
lets you continue the work seamlessly from it alone. Include: the user's goals and requests (in their
words where it matters); decisions made and why; the current state of the system and of the task;
files read or changed (exact paths) and what changed; commands that worked and ones that failed (with
the errors); open problems; and the immediate next step. Be specific - paths, values, versions, error
text. Reply with the summary only; do not call any tools."""


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None = None, auth: bool = False) -> None:
        super().__init__(message)
        self.status, self.auth = status, auth


@dataclass
class Turn:
    """One model response, provider-neutral."""
    text: str
    tool_calls: list[ToolCall]
    stop: str                   # tool_use | end_turn | max_tokens | refusal | pause
    cost: float
    prompt_tokens: int          # total size of the prompt that produced this turn
    usage_line: str
    usage: dict = field(default_factory=dict)
    detail: str = ""


def _usage_line(prompt: int, cached: int, output: int, cost: float) -> str:
    share = cached / prompt if prompt else 0
    return f"[{prompt:,} input tokens ({share:.0%} from cache), {output:,} output, ~${cost:.4f}]"


def _retry_wait(attempt: int, what: str, ui, indent: str, ledger=None) -> None:
    delay = RETRY_DELAYS[attempt]
    if ledger:
        ledger.event("api_retry", error=what, attempt=attempt + 1)
    ui.notice(f"[{what} - retrying in {delay}s]", indent)
    time.sleep(delay)


# =============================================================================================
# OpenRouter


BASH_SCHEMA = {"type": "object", "properties": {
    "command": {"type": "string", "description": "The bash command to run"},
    "restart": {"type": "boolean", "description": "Restart the shell instead of running a command"}}}
MEMORY_SCHEMA = {"type": "object", "required": ["command"], "properties": {
    "command": {"type": "string", "enum": ["view", "create", "str_replace", "insert", "delete", "rename"]},
    "path": {"type": "string", "description": "A path under /memories"},
    "view_range": {"type": "array", "items": {"type": "integer"}},
    "file_text": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"},
    "insert_line": {"type": "integer"}, "insert_text": {"type": "string"},
    "old_path": {"type": "string"}, "new_path": {"type": "string"}}}
NATIVE_DESCRIPTIONS = {
    "bash": "Run a command in a persistent bash shell on the user's machine. cd, exported variables and "
            "activated virtualenvs carry over between calls. Set restart to true to reset the shell.",
    "memory": "Your persistent memory: a directory of notes at /memories that survives across sessions. "
              "Commands: view (a file or directory), create (path, file_text), str_replace (path, old_str, "
              "new_str), insert (path, insert_line, insert_text), delete (path), rename (old_path, new_path).",
}


def _neutral_to_openai_content(content):
    """Neutral content (str, or text/image blocks in Anthropic shape) -> OpenAI chat content."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if block.get("type") == "text":
            parts.append({"type": "text", "text": block["text"]})
        elif block.get("type") == "image":
            src = block["source"]
            parts.append({"type": "image_url", "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
    return parts


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, config: dict, http_client=None, ledger=None) -> None:
        import httpx2
        settings = config.get("openrouter", {})
        self.model = config["model"]
        self.fallbacks = list(settings.get("fallback_models", []))
        self.base_url = settings.get("base_url", "https://openrouter.ai/api/v1").rstrip("/")
        self.api_key = self._api_key(settings)
        self.ledger = ledger
        # A stream that sends only keep-alive comments for this long is treated as stalled and retried
        # (OpenRouter keeps idle connections open with ": OPENROUTER PROCESSING" lines, so a plain read
        # timeout would never fire). Complete silence is caught by the read timeout.
        self.stall_seconds = float(config.get("stall_seconds", 300))
        self.http = http_client or httpx2.Client(timeout=httpx2.Timeout(self.stall_seconds + 60, connect=30))
        self.info = self._model_info()
        self.cache = self.model.startswith("anthropic/")     # explicit cache breakpoints
        params = set(self.info.get("supported_parameters") or [])
        self.reasoning = "reasoning" in params or not params
        top = self.info.get("top_provider") or {}
        self.max_tokens = min(int(config.get("max_tokens", 32000)),
                              int(top.get("max_completion_tokens") or 10**9))
        context = int(self.info.get("context_length") or 200_000)
        self.compact_at = min(int(config.get("compact_at_tokens", 150_000)), int(context * 0.8))
        pricing = self.info.get("pricing") or {}
        self.price_in = float(pricing.get("prompt") or 0)
        self.price_out = float(pricing.get("completion") or 0)

    @staticmethod
    def _api_key(settings: dict) -> str:
        """From the environment, else the key file, else a .env file (default: next to agent.py).
        Read into memory only - never exported, so the agent's own shell can't see it."""
        name = settings.get("api_key_env", "OPENROUTER_API_KEY")
        key = os.environ.get(name, "")
        key_file = Path(os.path.expanduser(settings.get("api_key_file", "~/.config/sysadmin-agent/openrouter.key")))
        if not key and key_file.is_file():
            key = key_file.read_text().strip()
        env_file = Path(os.path.expanduser(settings.get("env_file") or Path(__file__).resolve().parent / ".env"))
        if not key and env_file.is_file():
            for line in env_file.read_text().splitlines():
                left, sep, value = line.strip().removeprefix("export ").partition("=")
                if sep and left.strip() == name:
                    key = value.strip().strip("'\"")
        return key

    def _model_info(self) -> dict:
        try:
            response = self.http.get(f"{self.base_url}/models", timeout=30)
            for model in response.json().get("data", []):
                if model.get("id") == self.model:
                    return model
        except Exception:
            pass
        return {}

    def _headers(self) -> dict:
        if not self.api_key:
            raise ProviderError("No OpenRouter API key: set OPENROUTER_API_KEY, put it in a .env file next "
                                "to agent.py (OPENROUTER_API_KEY=...), or in ~/.config/sysadmin-agent/openrouter.key",
                                auth=True)
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/local/sysadmin-agent", "X-Title": "sysadmin-agent"}

    # -- history building --

    def render_tools(self, tools: list[Tool], server_tools: list[ServerTool]) -> list[dict]:
        rendered = []
        for tool in tools:
            schema = {"bash": BASH_SCHEMA, "memory": MEMORY_SCHEMA}.get(tool.native, tool.schema)
            description = NATIVE_DESCRIPTIONS.get(tool.native, tool.description)
            rendered.append({"type": "function", "function": {"name": tool.name, "description": description,
                                                              "parameters": schema}})
        rendered += [{"type": f"openrouter:{t.name}"} for t in server_tools]
        return rendered

    def user(self, content) -> list[dict]:
        return [{"role": "user", "content": _neutral_to_openai_content(content)}]

    def system_note(self, text: str) -> list[dict]:
        return [{"role": "system", "content": text}]

    def tool_results(self, results: list[dict], extra_text: str | None = None) -> list[dict]:
        messages, images = [], []
        for r in results:
            content = r["content"]
            if isinstance(content, list):   # images can't ride in a tool message: send them right after
                images += [b for b in content if b.get("type") == "image"]
                content = "\n".join(b.get("text", "") for b in content if b.get("type") == "text") or "(see image)"
            messages.append({"role": "tool", "tool_call_id": r["id"],
                             "content": ("ERROR: " if r.get("is_error") else "") + content})
        if images or extra_text:
            parts = _neutral_to_openai_content([*images, *([{"type": "text", "text": extra_text}] if extra_text else [])])
            if images and not extra_text:
                parts.append({"type": "text", "text": "(images returned by the tool call above)"})
            messages.append({"role": "user", "content": parts})
        return messages

    def _request_messages(self, history: list, system_text: str) -> list:
        if not self.cache:
            return [{"role": "system", "content": system_text}] + history
        # Anthropic models via OpenRouter: one breakpoint on the static system prompt and one moving
        # breakpoint on the newest message, so each request reuses everything before it.
        system = {"role": "system", "content": [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]}
        messages = list(history)
        if messages:
            last = copy.deepcopy(messages[-1])
            content = last.get("content")
            if isinstance(content, str) and content:
                last["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
            elif isinstance(content, list) and content:
                content[-1]["cache_control"] = {"type": "ephemeral"}
            messages[-1] = last
        return [system] + messages

    # -- one request --

    def stream(self, history: list, system_text: str, tools_payload: list, effort: str, ui, indent: str,
               cancel, tool_choice: str | None = None, display: bool = True) -> Turn:
        body = {"model": self.model, "messages": self._request_messages(history, system_text),
                "tools": tools_payload, "stream": True, "max_tokens": self.max_tokens,
                "usage": {"include": True}}
        if self.fallbacks:
            body["models"] = [self.model, *self.fallbacks]
        if self.reasoning and effort:
            body["reasoning"] = {"effort": effort}
        if tool_choice:
            body["tool_choice"] = tool_choice
        attempt, dropped_reasoning = 0, False
        while True:
            try:
                turn, message = self._stream_once(body, ui if display else None, indent, cancel)
                history.append(message)
                return turn
            except ProviderError as e:
                if e.status == 400 and "reasoning" in str(e).lower() and "reasoning" in body and not dropped_reasoning:
                    body.pop("reasoning")
                    dropped_reasoning = True
                    ui.notice("[this model rejected the reasoning setting - retrying without it]", indent)
                    continue
                if e.status not in RETRYABLE_STATUS or attempt >= len(RETRY_DELAYS):
                    raise
                _retry_wait(attempt, str(e)[:200], ui, indent, self.ledger)
                attempt += 1
            except OSError as e:   # connection problems (httpx2 errors are OSError subclasses or wrap them)
                if attempt >= len(RETRY_DELAYS):
                    raise ProviderError(f"Could not reach OpenRouter: {e}") from e
                _retry_wait(attempt, "connection problem", ui, indent, self.ledger)
                attempt += 1
            except Exception as e:
                if type(e).__module__.startswith("httpx") and attempt < len(RETRY_DELAYS):
                    _retry_wait(attempt, f"connection problem ({type(e).__name__})", ui, indent, self.ledger)
                    attempt += 1
                    continue
                raise

    def _stream_once(self, body: dict, ui, indent: str, cancel) -> tuple[Turn, dict]:
        text, details, calls = [], {}, {}
        finish, usage, thought_shown = None, {}, False
        last_data = time.monotonic()
        with self.http.stream("POST", f"{self.base_url}/chat/completions", json=body, headers=self._headers()) as response:
            if response.status_code != 200:
                raw = response.read().decode(errors="replace")
                try:
                    message = json.loads(raw).get("error", {}).get("message", raw)
                except ValueError:
                    message = raw
                raise ProviderError(f"OpenRouter {response.status_code}: {message}", response.status_code,
                                    auth=response.status_code in (401, 403))
            for line in response.iter_lines():
                if cancel is not None and cancel.is_set():
                    raise TurnCancelled()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    if time.monotonic() - last_data > self.stall_seconds:
                        raise ProviderError(f"OpenRouter stream stalled: no data for {self.stall_seconds:.0f}s", 504)
                    continue
                last_data = time.monotonic()
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("error"):
                    err = chunk["error"]
                    raise ProviderError(f"OpenRouter: {err.get('message', err)}", err.get("code") if isinstance(err.get("code"), int) else 500)
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        text.append(delta["content"])
                        if ui:
                            ui.text(delta["content"], indent)
                    if delta.get("reasoning") and ui:
                        ui.thought(delta["reasoning"], indent)
                        thought_shown = True
                    for d in delta.get("reasoning_details") or []:
                        self._merge_detail(details, d)
                        if ui and not delta.get("reasoning") and d.get("type") in ("reasoning.text", "reasoning.summary"):
                            piece = d.get("text") or d.get("summary") or ""
                            if piece:
                                ui.thought(piece, indent)
                                thought_shown = True
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", len(calls)), {"id": "", "name": "", "arguments": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] += fn.get("name") or ""
                        slot["arguments"] += fn.get("arguments") or ""
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        if ui:
            ui.block_end()

        full_text = "".join(text)
        ordered = [calls[i] for i in sorted(calls)]
        for c in ordered:                 # some models omit tool call ids; the history needs them
            c["id"] = c["id"] or f"call_{uuid.uuid4().hex[:12]}"
        tool_calls = []
        for c in ordered:
            try:
                parsed = json.loads(c["arguments"]) if c["arguments"].strip() else {}
            except ValueError:
                parsed = c["arguments"]          # left as a string: validation turns it into INVALID_JSON
            tool_calls.append(ToolCall(c["id"], c["name"], parsed))

        message = {"role": "assistant", "content": full_text or None}
        if ordered:
            message["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                                     for c in ordered]
        if details:
            # Passed back unchanged on the next request - required to keep reasoning (e.g. Claude's
            # thinking) valid across tool calls.
            message["reasoning_details"] = [details[i] for i in sorted(details)]

        stop = {"tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}.get(finish, "end_turn")
        if tool_calls and stop == "end_turn":
            stop = "tool_use"
        prompt = int(usage.get("prompt_tokens") or 0)
        output = int(usage.get("completion_tokens") or 0)
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        cost = usage.get("cost")
        if cost is None:
            cost = prompt * self.price_in + output * self.price_out
        turn = Turn(full_text, tool_calls, stop, float(cost), prompt, _usage_line(prompt, cached, output, float(cost)),
                    usage, detail=finish or "")
        return turn, message

    @staticmethod
    def _merge_detail(details: dict, d: dict) -> None:
        key = d.get("index", len(details))
        if key not in details:
            details[key] = dict(d)
            return
        current = details[key]
        for k, v in d.items():
            if k in ("text", "summary", "data") and isinstance(v, str):
                current[k] = current.get(k, "") + v
            elif v not in (None, ""):
                current[k] = v

    # -- context management --

    def needs_compaction(self, turn: Turn | None) -> bool:
        return bool(turn) and turn.prompt_tokens > self.compact_at

    def compact(self, history: list, system_text: str, tools_payload: list, ui, cancel) -> list:
        """Simple compaction: replace the whole history with a summary (plus the newest user
        message if the turn has just started), replaying no earlier reasoning."""
        ui.notice("[compacting the conversation to free up context]")
        keep_last = [history[-1]] if history and history[-1].get("role") == "user" else []
        work = history + [{"role": "user", "content": SUMMARY_PROMPT}]
        turn = self.stream(work, system_text, tools_payload, "", ui, "", cancel, tool_choice="none", display=False)
        summary = turn.text.strip() or "(the summary came back empty)"
        if self.ledger:
            self.ledger.event("compaction", tokens_before=sum(len(json.dumps(m)) for m in history) // 4)
        return [{"role": "user", "content": "This conversation was compacted to save context. Summary of "
                                            f"everything so far:\n\n{summary}\n\nContinue from here."}] + keep_last

    @staticmethod
    def dump(history: list) -> list:
        return history

    @staticmethod
    def load(data: list) -> list:
        return data


# =============================================================================================
# Anthropic (direct)


ANTHROPIC_BETAS = [
    "server-side-fallback-2026-07-01",      # fallbacks="default"
    "thinking-display-updates-2026-08-18",  # progress notes between tool calls
    "compact-2026-01-12",                   # server-side compaction for long sessions
]
DROP_BEFORE_FALLBACK = {"thinking", "redacted_thinking", "tool_use"}
SERVER_TOOL_LABELS = {"web_search": "searching the web", "web_fetch": "reading a web page"}
PRICES = {"input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20}   # Claude Opus 5.5, $/M


def blocks_to_keep(content: list) -> list:
    """The assistant blocks to keep in history after a mid-stream refusal fallback: the declined
    attempt's thinking and tool calls (and any unanswered web call) are dropped; everything
    else, including compaction blocks, is kept."""
    boundary = max((i for i, b in enumerate(content) if b.type == "fallback"), default=-1)
    if boundary < 0:
        return list(content)
    answered = {getattr(b, "tool_use_id", None) for b in content[:boundary]}
    return [b for i, b in enumerate(content)
            if i > boundary or not (b.type in DROP_BEFORE_FALLBACK
                                    or (b.type == "server_tool_use" and b.id not in answered))]


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, config: dict, client=None, ledger=None) -> None:
        import anthropic
        self.anthropic = anthropic
        self.model = config.get("anthropic_model", "claude-opus-5-5")
        self.client = client or anthropic.Anthropic()
        self.ledger = ledger

    def render_tools(self, tools: list[Tool], server_tools: list[ServerTool]) -> list[dict]:
        rendered = []
        for tool in tools:
            if tool.native == "bash":
                rendered.append({"type": "bash_20250124", "name": "bash"})
            elif tool.native == "memory":
                rendered.append({"type": "memory_20250818", "name": "memory"})
            else:
                # eager_input_streaming: big inputs (whole files) stream as generated; the API then
                # doesn't validate them, which the registry does instead.
                rendered.append({"name": tool.name, "description": tool.description,
                                 "input_schema": tool.schema, "eager_input_streaming": True})
        rendered += [{"type": f"{t.name}_20260209", "name": t.name} for t in server_tools]
        return rendered

    def user(self, content) -> list[dict]:
        return [{"role": "user", "content": content}]

    def system_note(self, text: str) -> list[dict]:
        return [{"role": "system", "content": text}]

    def tool_results(self, results: list[dict], extra_text: str | None = None) -> list[dict]:
        blocks = []
        for r in results:
            block = {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]}
            if r.get("is_error"):
                block["is_error"] = True
            blocks.append(block)
        if extra_text:
            blocks.append({"type": "text", "text": extra_text})   # appended, never edited later
        return [{"role": "user", "content": blocks}]

    def stream(self, history: list, system_text: str, tools_payload: list, effort: str, ui, indent: str,
               cancel, tool_choice: str | None = None, display: bool = True) -> Turn:
        anthropic = self.anthropic
        system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
        attempt, malformed = 0, 0
        while True:
            try:
                with self.client.beta.messages.stream(
                    model=self.model, max_tokens=64000, betas=ANTHROPIC_BETAS, fallbacks="default",
                    thinking={"type": "adaptive", "display": "updates"},
                    output_config={"effort": effort or "high"},
                    cache_control={"type": "ephemeral"},
                    context_management={"edits": [{"type": "compact_20260112"}]},
                    system=system, tools=tools_payload, messages=history,
                ) as stream:
                    self._show(stream, ui if display else _Quiet(), indent, cancel)
                    response = stream.get_final_message()
                break
            except ValueError:
                malformed += 1   # tool input JSON the SDK couldn't parse at all: ask again
                if malformed > 2:
                    raise ProviderError("The model kept producing malformed tool input - try rephrasing.")
                ui.notice("[malformed tool input - asking again]", indent)
            except anthropic.AuthenticationError as e:
                raise ProviderError(str(e), 401, auth=True) from e
            except anthropic.APIConnectionError as e:
                if attempt >= len(RETRY_DELAYS):
                    raise ProviderError(f"Could not reach the Anthropic API: {e}") from e
                _retry_wait(attempt, "connection problem", ui, indent, self.ledger)
                attempt += 1
            except anthropic.APIStatusError as e:
                if e.status_code not in RETRYABLE_STATUS or attempt >= len(RETRY_DELAYS):
                    raise ProviderError(f"Anthropic API error {e.status_code}: {e.message}", e.status_code) from e
                _retry_wait(attempt, f"API error {e.status_code}", ui, indent, self.ledger)
                attempt += 1

        content = blocks_to_keep(response.content)
        if content:
            history.append({"role": "assistant", "content": content})
        u = response.usage
        read, write = u.cache_read_input_tokens or 0, u.cache_creation_input_tokens or 0
        prompt = u.input_tokens + read + write
        cost = (u.input_tokens * PRICES["input"] + write * PRICES["cache_write"]
                + read * PRICES["cache_read"] + u.output_tokens * PRICES["output"]) / 1_000_000
        stop = {"tool_use": "tool_use", "pause_turn": "pause", "refusal": "refusal",
                "max_tokens": "max_tokens"}.get(response.stop_reason, "end_turn")
        detail = (response.stop_details.category or "") if response.stop_reason == "refusal" and response.stop_details else ""
        return Turn("\n".join(b.text for b in content if b.type == "text"),
                    [ToolCall(b.id, b.name, b.input) for b in content if b.type == "tool_use"],
                    stop, cost, prompt, _usage_line(prompt, read, u.output_tokens, cost),
                    u.model_dump(exclude_none=True), detail)

    @staticmethod
    def _show(stream, ui, indent: str, cancel) -> None:
        for event in stream:
            if cancel is not None and cancel.is_set():
                raise TurnCancelled()
            if event.type == "content_block_start":
                block = event.content_block
                if block.type == "fallback":
                    ui.notice("[the first model declined - continuing on a fallback model]", indent)
                elif block.type == "server_tool_use":
                    ui.notice(f"  [{SERVER_TOOL_LABELS.get(block.name, block.name)}...]", indent)
                elif block.type == "compaction":
                    ui.notice("[conversation compacted to free up context]", indent)
            elif event.type == "content_block_delta":
                if event.delta.type == "thinking_delta" and event.delta.thinking:
                    ui.thought(event.delta.thinking, indent)
                elif event.delta.type == "text_delta":
                    ui.text(event.delta.text, indent)
            elif event.type == "content_block_stop":
                ui.block_end()

    def needs_compaction(self, turn) -> bool:
        return False   # the API compacts server-side

    def compact(self, history, system_text, tools_payload, ui, cancel) -> list:
        return history

    @staticmethod
    def dump(history: list) -> list:
        return json.loads(json.dumps(history, default=lambda o: o.model_dump(mode="json", exclude_none=True)
                                     if hasattr(o, "model_dump") else str(o)))

    @staticmethod
    def load(data: list) -> list:
        return data


class _Quiet:
    """A UI that shows nothing (subagent streams, summaries)."""
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def make_provider(config: dict, ledger=None, client=None):
    if config.get("provider", "openrouter") == "anthropic":
        return AnthropicProvider(config, client=client, ledger=ledger)
    return OpenRouterProvider(config, http_client=client, ledger=ledger)
