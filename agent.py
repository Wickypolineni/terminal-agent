#!/usr/bin/env python3
"""A general-purpose agent for your own computer.

    agent.py                         interactive chat in the terminal
    agent.py -p "task" [--json]      one-shot, non-interactive (scripts, cron); approvals are refused
    agent.py --continue / --resume ID   pick up a saved session
    agent.py --acp                   serve the Agent Client Protocol on stdio (for apps: Reflick, Zed, ...)
    agent.py --schedule "0 9 * * *" -p "task"   run a task on a schedule (crontab)

Settings: ~/.config/sysadmin-agent/config.json (see config.example.json).
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from engine import DATA_DIR, CONFIG_FILE, Session, load_config, run_turn
from policy import MODES
from providers import ProviderError
from tools import IMAGE_TYPES, image_block
from ui import HeadlessUI, TerminalUI

PATH_TOKEN = re.compile(r"""'([^']+)'|"([^"]+)"|(\S+)""")
CRON_TAG = "# sysadmin-agent:"


def user_content(text: str, cwd: str, ui):
    """Attach any image files mentioned in the message (drag a screenshot into the terminal)."""
    images = []
    for match in PATH_TOKEN.finditer(text):
        token = next(group for group in match.groups() if group)
        path = Path(cwd, os.path.expanduser(token))
        if path.suffix.lower() in IMAGE_TYPES and path.is_file():
            try:
                images.append(image_block(path))
                ui.notice(f"  [attached {path}]")
            except Exception as e:
                ui.notice(f"  [couldn't attach {path}: {e}]")
    return [*images, {"type": "text", "text": text}] if images else text


HELP = f"""Commands:
  /mode [name]   show or change the permission mode: {', '.join(MODES)}
  /tools         list the agent's tools        /skills    list skills
  /sessions      list saved sessions           /cost      cost of this session
  /memory        list what the agent remembers /ledger    where this session is recorded
  /clear         start a new conversation      /exit      quit (Ctrl+D also works)
Ctrl+C while the agent works cancels that request; Ctrl+C at the prompt quits.
Mention an image path (or drag a screenshot into the terminal) to show it to the agent.
Settings: {CONFIG_FILE} (see config.example.json)."""


def show_memory() -> None:
    root = DATA_DIR / "memories"
    notes = sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []
    print(f"[memory folder: {root}]")
    for note in notes:
        print(f"  {note.relative_to(root)}")
    if not notes:
        print("  (empty)")


def handle_command(session: Session, command: str, args: list[str]) -> bool:
    """Run a slash command. Returns False when the chat should end."""
    if command in ("/exit", "/quit"):
        return False
    if command == "/mode":
        if args and args[0] in MODES:
            session.set_mode(args[0])
        elif args:
            print(f"[unknown mode - choose from {', '.join(MODES)}]")
            return True
        print(f"[mode: {session.policy.mode} - {MODES[session.policy.mode]}]")
        if not args:
            for name, description in MODES.items():
                print(f"  {name:10} {description}")
    elif command == "/tools":
        for tool in session.registry.tools.values():
            print(f"  {tool.name:28} {tool.risk}")
        for tool in session.registry.server_tools:
            print(f"  {tool.name:28} web (run by {session.provider.name})")
    elif command == "/skills":
        print(session.skills.describe() or "  (no skills found)")
    elif command == "/sessions":
        for s in session.store.list()[:15]:
            print(f"  {s['id']}  {s.get('updated', '')[:16]}  ${s.get('cost_usd', 0):.2f}  {s.get('title', '')[:60]}")
    elif command == "/cost":
        print(f"[this session so far: ~${session.cost:.4f}]")
    elif command == "/memory":
        show_memory()
    elif command == "/ledger":
        print(f"[ledger: {session.ledger.path}]\n[session: {session.store.path(session.id)}]")
    elif command == "/clear":
        session.store.save(session)
        from engine import new_session_id
        session.id, session.history, session.title = new_session_id(), [], ""
        print("[new conversation]")
    elif command == "/help":
        print(HELP)
    return True


COMMANDS = {"/exit", "/quit", "/mode", "/tools", "/skills", "/sessions", "/cost", "/memory", "/ledger", "/clear", "/help"}


def chat(session: Session) -> None:
    tool_count = len(session.registry.tools) + len(session.registry.server_tools)
    print(f"Agent ready - {session.provider.model} via {session.provider.name} · mode: {session.policy.mode} · "
          f"{tool_count} tools · {len(session.skills.skills)} skills"
          + (f" · MCP: {', '.join(sorted({t.server for t in session.mcp.tools}))}" if session.mcp.tools else "")
          + (f"\nResumed session {session.id} ({len(session.history)} messages)" if session.history else "")
          + "\n/help for commands, Ctrl+D to quit.")
    for server, error in session.mcp.errors.items():
        print(f"  [MCP server {server} failed to start: {error}]")
    while True:
        try:
            user_input = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not user_input:
            continue
        command, *args = user_input.split()
        if command.lower() in COMMANDS:
            if not handle_command(session, command.lower(), args):
                return
            continue
        try:
            _, stop = run_turn(session, user_content(user_input, session.shell.cwd, session.ui))
            if stop == "cancelled":
                print("\n[cancelled - tell me what to do instead, or carry on]")
            else:
                print(f"  [session total: ~${session.cost:.4f}]")
        except ProviderError as e:
            session.ledger.event("api_error", message=str(e)[:500])
            print(f"\n[{e}]")
            if e.auth:
                return


def schedule(cron: str, prompt: str, mode: str) -> None:
    if len(cron.split()) != 5:
        sys.exit("--schedule needs a 5-field cron expression, e.g. \"0 9 * * *\"")
    tag = os.urandom(3).hex()
    log = DATA_DIR / "scheduled.log"
    command = (f"{cron} cd {shlex.quote(os.getcwd())} && {shlex.quote(sys.executable)} "
               f"{shlex.quote(str(Path(__file__).resolve()))} -p {shlex.quote(prompt)} --mode {mode} "
               f">> {shlex.quote(str(log))} 2>&1 {CRON_TAG}{tag}")
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    subprocess.run(["crontab", "-"], input=current.rstrip("\n") + "\n" + command + "\n", text=True, check=True)
    print(f"Scheduled [{tag}]: {cron} -> {prompt!r} (mode {mode}); output appends to {log}")


def list_schedules() -> None:
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    found = [line for line in current.splitlines() if CRON_TAG in line]
    for line in found:
        print(f"[{line.rsplit(CRON_TAG, 1)[1]}] {line.split(' cd ', 1)[0]}  ->  {shlex.split(line.split(' -p ', 1)[1])[0]}")
    if not found:
        print("No scheduled tasks.")


def unschedule(tag: str) -> None:
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    kept = [line for line in current.splitlines() if not line.endswith(CRON_TAG + tag)]
    subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n", text=True, check=True)
    print(f"Removed scheduled task {tag}." if len(kept) < len(current.splitlines()) else f"No scheduled task {tag}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="A general-purpose agent for your own computer.")
    parser.add_argument("-p", "--prompt", help="run one request non-interactively and exit")
    parser.add_argument("--json", action="store_true", help="with -p: print a JSON result")
    parser.add_argument("--mode", choices=list(MODES), help="permission mode for this run")
    parser.add_argument("--model", help="model id (OpenRouter id, or Anthropic id with --provider anthropic)")
    parser.add_argument("--provider", choices=["openrouter", "anthropic"])
    parser.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the latest session")
    parser.add_argument("-r", "--resume", metavar="ID", help="resume a saved session")
    parser.add_argument("--acp", action="store_true", help="serve the Agent Client Protocol on stdio")
    parser.add_argument("--schedule", metavar="CRON", help="with -p: add a crontab entry running it")
    parser.add_argument("--schedules", action="store_true", help="list scheduled tasks")
    parser.add_argument("--unschedule", metavar="ID", help="remove a scheduled task")
    args = parser.parse_args()

    if args.schedules:
        return list_schedules()
    if args.unschedule:
        return unschedule(args.unschedule)
    config = load_config()
    for key in ("mode", "provider"):
        if getattr(args, key):
            config[key] = getattr(args, key)
    if args.model:
        config["anthropic_model" if config["provider"] == "anthropic" else "model"] = args.model
    if args.schedule:
        if not args.prompt:
            sys.exit("--schedule needs a task: -p \"...\"")
        return schedule(args.schedule, args.prompt, args.mode or "read-only")
    if args.acp:
        from acp_server import serve
        return serve(config)

    ui = HeadlessUI() if args.prompt else TerminalUI()
    try:
        session = Session(config, ui)
    except ProviderError as e:
        sys.exit(str(e))
    try:
        if args.cont or args.resume:
            session.resume(args.resume or session.store.latest() or sys.exit("No saved session to continue."))
        if args.prompt:
            try:
                text, stop = run_turn(session, args.prompt)
            except ProviderError as e:
                text, stop = str(e), "error"
            if args.json:
                print(json.dumps({"result": text, "stop_reason": stop,
                                  "session_id": session.id, "cost_usd": round(session.cost, 4)}))
            else:
                print(text.strip())
            sys.exit(0 if stop in ("end_turn", "max_tokens") else 1)
        chat(session)
    finally:
        session.close()


if __name__ == "__main__":
    main()
