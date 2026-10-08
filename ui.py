"""How the agent talks to whoever is driving it.

The engine never calls print() or input() directly - it calls a UI. Three implementations:
  TerminalUI  interactive chat in a terminal
  HeadlessUI  one-shot runs from scripts or cron: answers go to stdout, status to stderr,
              approvals are refused (nobody is there to answer)
  ACPUI       (acp_server.py) an app such as Reflick or Zed, over the Agent Client Protocol
"""
import queue
import sys
import threading
from concurrent.futures import wait


class UI:
    """Default implementations do nothing; subclasses override what they can show."""

    interactive = False

    def text(self, delta: str, indent: str = "") -> None: ...          # streamed answer text
    def thought(self, delta: str, indent: str = "") -> None: ...       # streamed progress notes
    def block_end(self) -> None: ...                                   # a text/thought block finished
    def notice(self, message: str, indent: str = "") -> None: ...      # status lines
    def usage(self, line: str, indent: str = "") -> None: ...          # token/cost line per round

    def tool_start(self, call_id: str, title: str, kind: str, indent: str = "", note: str = "") -> None: ...
    def tool_output(self, call_id: str, line: str) -> None: ...        # live output of a command
    def tool_end(self, call_id: str, content, is_error: bool) -> None: ...

    def approve(self, call_id: str, title: str, reason: str, grantable: bool, grant_label: str) -> tuple[bool, str, bool]:
        """Ask whether a tool call may run. Returns (approved, feedback, yes_to_all_for_session)."""
        return False, "No one is available to approve this action (non-interactive run).", False

    def ask(self, question: str, options: list[str] | None) -> str:
        return ""

    def todos(self, todos: list[dict]) -> None: ...

    def serve_while(self, futures) -> None:
        """Wait for parallel tool calls (e.g. several subagents) to finish."""
        wait(futures)

    def cancel_pending(self) -> None:
        """The turn was cancelled: release anything waiting on the user."""


TODO_MARKS = {"pending": "☐", "in_progress": "◐", "completed": "☑"}


class TerminalUI(UI):
    """Parallel subagents run on worker threads, but only the main thread may read the keyboard
    (Ctrl+C lands there). So a worker's question is queued, and the main thread answers it while it
    waits for the workers (serve_while)."""
    interactive = True
    LIVE_LINES = 40

    def __init__(self) -> None:
        self._open: str | None = None    # "text" or "thought" while a block is streaming
        self._live: dict[str, int] = {}
        self._questions: queue.Queue = queue.Queue()
        self._inflight = None    # the worker question being answered right now: (reply, refused)

    def _on_main_thread(self, method, *args, refused):
        if threading.current_thread() is threading.main_thread():
            return method(*args)
        reply: queue.Queue = queue.Queue(maxsize=1)
        self._questions.put((method, args, reply, refused))
        return reply.get()

    def serve_while(self, futures):
        if threading.current_thread() is not threading.main_thread():
            return wait(futures)
        while not all(f.done() for f in futures):
            try:
                method, args, reply, refused = self._questions.get(timeout=0.05)
            except queue.Empty:
                continue
            # On Ctrl+C the asking thread is left waiting; the caller stops everything first and
            # only then calls cancel_pending() - so that thread wakes up already cancelled.
            self._inflight = (reply, refused)
            answer = method(*args)
            self._inflight = None
            reply.put(answer)
        self.cancel_pending()   # questions from calls that finished without waiting for an answer

    def cancel_pending(self):
        if self._inflight:
            reply, refused = self._inflight
            self._inflight = None
            reply.put(refused)
        while True:
            try:
                _, _, reply, refused = self._questions.get_nowait()
            except queue.Empty:
                return
            reply.put(refused)

    def _start(self, kind: str, indent: str) -> None:
        if self._open != kind:
            self.block_end()
            print(f"\n{indent}Claude: " if kind == "text" else f"\n{indent}  · ", end="")
            self._open = kind

    def text(self, delta, indent=""):
        self._start("text", indent)
        print(delta.replace("\n", "\n" + indent) if indent else delta, end="", flush=True)

    def thought(self, delta, indent=""):
        self._start("thought", indent)
        print(delta.replace("\n", "\n" + indent) if indent else delta, end="", flush=True)

    def block_end(self):
        if self._open:
            print()
            self._open = None

    def notice(self, message, indent=""):
        self.block_end()
        print(f"\n{indent}{message}")

    def usage(self, line, indent=""):
        self.block_end()
        print(f"{indent}  {line}")

    def tool_start(self, call_id, title, kind, indent="", note=""):
        self.block_end()
        print(f"\n{indent}→ {title}" + (f"   ({note})" if note else ""))

    def tool_output(self, call_id, line):
        shown = self._live.get(call_id, 0)
        if shown < self.LIVE_LINES:
            print("    │ " + line[:200], flush=True)
        elif shown == self.LIVE_LINES:
            print("    │ ... (more output hidden)", flush=True)
        self._live[call_id] = shown + 1

    def approve(self, call_id, title, reason, grantable, grant_label):
        return self._on_main_thread(self._approve, call_id, title, reason, grantable, grant_label,
                                    refused=(False, "The request was cancelled.", False))

    def _approve(self, call_id, title, reason, grantable, grant_label):
        self.block_end()
        print(f"\n  {title}")
        if reason:
            print(f"  (asking because: {reason})")
        grant = f"a = yes to all {grant_label} this session, " if grantable else ""
        answer = input(f"  allow? [y = yes, {grant}Enter = no, or type what to do instead]: ").strip()
        if answer.lower() in ("y", "yes"):
            return True, "", False
        if grantable and answer.lower() in ("a", "all"):
            return True, "", True
        return False, answer, False

    def ask(self, question, options):
        return self._on_main_thread(self._ask, question, options, refused="")

    def _ask(self, question, options):
        self.block_end()
        print(f"\n  ? {question}")
        for i, option in enumerate(options or [], 1):
            print(f"    {i}. {option}")
        hint = " (number, or type your own answer)" if options else ""
        answer = input(f"  your answer{hint}: ").strip()
        if options and answer.isdigit() and 1 <= int(answer) <= len(options):
            answer = options[int(answer) - 1]
        return answer

    def todos(self, todos):
        self.block_end()
        print("\n  To-do:")
        for todo in todos:
            print(f"    {TODO_MARKS.get(todo.get('status'), '·')} {todo.get('content')}")


class HeadlessUI(UI):
    """One-shot runs (`agent.py -p ...`): the answer on stdout, everything else on stderr."""

    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet

    def _err(self, message: str) -> None:
        if not self.quiet:
            print(message, file=sys.stderr, flush=True)

    def notice(self, message, indent=""):
        self._err(message)

    def usage(self, line, indent=""):
        self._err(line)

    def tool_start(self, call_id, title, kind, indent="", note=""):
        self._err(f"→ {title}" + (f"   ({note})" if note else ""))

    def approve(self, call_id, title, reason, grantable, grant_label):
        self._err(f"✗ needs approval, refused in a non-interactive run: {title} ({reason})")
        return False, ("This is a non-interactive run, so actions that need approval are refused. "
                       "Finish what you can and say exactly what the user should run or approve."), False
