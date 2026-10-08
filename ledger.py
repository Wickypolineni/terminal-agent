"""An append-only record of everything the agent did, one JSON object per line.

Per session, in <data dir>/sessions/<session id>.jsonl: every model round (tokens, cost) and tool
call (input, policy decision, whether you approved, duration, success, output size). The resumable
conversation itself lives next to it in <session id>.session.json (sessions.py). Both files are
readable only by you. Credential values never reach either: the http tool
receives credentials by name and redacts their values from responses.
"""
import json
import os
import threading
import time
from pathlib import Path

PREVIEW_CHARS = 2000


def preview(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + f"... [{len(text) - PREVIEW_CHARS} more]"


class Ledger:
    def __init__(self, root: Path, session_id: str) -> None:
        self.dir = root / "sessions"
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.path = self.dir / f"{session_id}.jsonl"
        self.path.touch(mode=0o600)
        self.lock = threading.Lock()

    def event(self, kind: str, **fields) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": kind, **fields}
        line = json.dumps(record, default=str) + "\n"
        with self.lock, open(self.path, "a") as f:
            f.write(line)
