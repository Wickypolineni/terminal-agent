"""Shared helpers: a PASS/FAIL checker and a fake Claude API that replays scripted responses."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_results: list[bool] = []


def check(name: str, ok, detail="") -> None:
    _results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"   -> {detail}"))


def done() -> None:
    passed = sum(_results)
    print(f"\n{passed}/{len(_results)} passed")
    sys.exit(0 if passed == len(_results) else 1)


# --- fake streaming API ------------------------------------------------------------------------

def sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def message(blocks: list, stop: str, usage: dict | None = None) -> list[dict]:
    usage = usage or {"input_tokens": 50, "output_tokens": 1, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 2000}
    events = [{"type": "message_start", "message": {
        "id": "msg", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": usage}}]
    for n, (start, deltas) in enumerate(blocks):
        events += [{"type": "content_block_start", "index": n, "content_block": start},
                   *[{"type": "content_block_delta", "index": n, "delta": d} for d in deltas],
                   {"type": "content_block_stop", "index": n}]
    return events + [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                      "usage": {"output_tokens": 30}}, {"type": "message_stop"}]


def tool_use(id: str, name: str, input: dict):
    return ({"type": "tool_use", "id": id, "name": name, "input": {}},
            [{"type": "input_json_delta", "partial_json": json.dumps(input)}])


def text(t: str):
    return ({"type": "text", "text": ""}, [{"type": "text_delta", "text": t}])


def note(t: str, signature: str = "sig"):
    return ({"type": "thinking", "thinking": "", "signature": ""},
            [{"type": "thinking_delta", "thinking": t}, {"type": "signature_delta", "signature": signature}])


class FakeAPI:
    """Scripted responses; an int in a script is returned as that HTTP error status instead."""

    def __init__(self, main: list, sub: list | None = None) -> None:
        self.main, self.sub = list(main), list(sub or [])
        self.requests: list[dict] = []

    def handler(self, request):
        import httpx2
        body = json.loads(request.content)
        is_sub = body["system"][0]["text"].startswith("You are a subagent")
        self.requests.append({"sub": is_sub, "body": body, "beta": request.headers.get("anthropic-beta", "")})
        item = (self.sub if is_sub else self.main).pop(0)
        if isinstance(item, int):
            return httpx2.Response(item, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse(item))

    def client(self):
        import anthropic
        import httpx2
        return anthropic.Anthropic(api_key="test", max_retries=0,
                                   http_client=httpx2.Client(transport=httpx2.MockTransport(self.handler)))

    def main_requests(self) -> list[dict]:
        return [r for r in self.requests if not r["sub"]]
