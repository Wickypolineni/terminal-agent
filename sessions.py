"""Saved sessions: each conversation is stored so it can be resumed later (--continue, --resume,
or an app's session/load over ACP).

    <data dir>/sessions/<id>.session.json   metadata + the provider's history, readable only by you
"""
import json
import os
import time
from pathlib import Path


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.dir = root / "sessions"
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)

    def path(self, session_id: str) -> Path:
        return self.dir / f"{session_id}.session.json"

    def save(self, session) -> None:
        data = {"id": session.id, "provider": session.provider.name, "model": session.provider.model,
                "cwd": session.shell.cwd, "title": session.title, "mode": session.policy.mode,
                "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "cost_usd": round(session.cost, 4),
                "history": session.provider.dump(session.history)}
        tmp = self.path(session.id).with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path(session.id))

    def load(self, session_id: str) -> dict:
        path = self.path(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"no saved session {session_id!r}")
        return json.loads(path.read_text())

    def list(self) -> list[dict]:
        found = []
        for path in self.dir.glob("*.session.json"):
            try:
                data = json.loads(path.read_text())
            except ValueError:
                continue
            data.pop("history", None)
            found.append(data)
        return sorted(found, key=lambda d: d.get("updated", ""), reverse=True)

    def latest(self) -> str | None:
        sessions = self.list()
        return sessions[0]["id"] if sessions else None
