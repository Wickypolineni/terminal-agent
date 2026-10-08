"""A fake OpenRouter: a real local HTTP server speaking the chat-completions streaming protocol.

Scripted: each POST /chat/completions takes the next scripted response - a list of SSE chunks, an
int (an HTTP error status), or (status, message). Requests are recorded. GET /models describes one model.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "anthropic/claude-opus-5.5"
MODEL_INFO = {"id": MODEL, "context_length": 1_000_000, "top_provider": {"max_completion_tokens": 128_000},
              "pricing": {"prompt": "0.000004", "completion": "0.00002"}, "supported_parameters": ["tools", "reasoning"]}


def usage(prompt=1000, completion=50, cached=800, cost=0.0015):
    return {"prompt_tokens": prompt, "completion_tokens": completion, "cost": cost,
            "prompt_tokens_details": {"cached_tokens": cached}}


def text_reply(text: str, finish="stop", **u) -> list[dict]:
    pieces = [text[i:i + 7] for i in range(0, len(text), 7)] or [""]
    chunks = [{"choices": [{"index": 0, "delta": {"content": p}}]} for p in pieces]
    return chunks + [{"choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage(**u)}]


def tool_reply(calls: list[tuple], text: str = "", reasoning: str = "", signature: str = "sig-abc", **u) -> list[dict]:
    chunks = []
    if reasoning:   # reasoning text in two pieces, then its signature
        half = len(reasoning) // 2
        for piece in (reasoning[:half], reasoning[half:]):
            chunks.append({"choices": [{"index": 0, "delta": {"reasoning": piece, "reasoning_details": [
                {"type": "reasoning.text", "text": piece, "index": 0, "format": "anthropic-claude-v1"}]}}]})
        chunks.append({"choices": [{"index": 0, "delta": {"reasoning_details": [
            {"type": "reasoning.text", "signature": signature, "index": 0}]}}]})
    if text:
        chunks.append({"choices": [{"index": 0, "delta": {"content": text}}]})
    for i, (call_id, name, args) in enumerate(calls):
        raw = json.dumps(args)
        cut = len(raw) // 2   # arguments arrive in fragments
        chunks.append({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": i, "id": call_id, "type": "function", "function": {"name": name, "arguments": raw[:cut]}}]}}]})
        chunks.append({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": i, "function": {"arguments": raw[cut:]}}]}}]})
    return chunks + [{"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], "usage": usage(**u)}]


class FakeOpenRouter:
    def __init__(self, script: list | None = None) -> None:
        self.script = list(script or [])
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        self.router = None    # optional: request body -> response, instead of the script
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.dumps({"data": [MODEL_INFO]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with fake.lock:
                    fake.requests.append({"body": request, "auth": self.headers.get("Authorization")})
                    if not fake.router:
                        item = fake.script.pop(0) if fake.script else text_reply("(script exhausted)")
                if fake.router:   # outside the lock: a router may wait for concurrent requests
                    item = fake.router(request)
                if isinstance(item, (int, tuple)):
                    status, text = item if isinstance(item, tuple) else (item, f"scripted error {item}")
                    payload = json.dumps({"error": {"message": text, "code": status}}).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if isinstance(item, dict) and "stall" in item:   # keep-alives only, never any data
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    end = time.monotonic() + item["stall"]
                    try:
                        while time.monotonic() < end:
                            self.wfile.write(b": OPENROUTER PROCESSING\n\n")
                            self.wfile.flush()
                            time.sleep(0.2)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    self.wfile.write(b": OPENROUTER PROCESSING\n\n")
                    for chunk in item:
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass   # the client cancelled mid-stream

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, *items) -> None:
        with self.lock:
            self.script.extend(items)

    def bodies(self) -> list[dict]:
        return [r["body"] for r in self.requests]

    def close(self) -> None:
        self.server.shutdown()


def config(url: str, **overrides) -> dict:
    """A config dict pointing the agent at the fake server."""
    import engine
    cfg = engine.load_config(engine.Path("/nonexistent"))
    cfg["openrouter"]["base_url"] = url
    cfg.update(overrides)
    return cfg
