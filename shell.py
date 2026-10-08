"""A persistent bash session for the agent: state carries between commands like in a terminal."""
import os
import re
import select
import signal
import subprocess
import threading
import time
import uuid

COMMAND_TIMEOUT = 300      # seconds before a command is killed (config: command_timeout_seconds)
OUTPUT_LIMIT = 20_000      # characters of command output sent back per call

# Environment variables that look like secrets are removed before the shell starts, so no
# command - or anything a command downloads and runs - can read them. A few names match the
# pattern but only point at sockets or files that tools need to work, so they stay.
SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|PRIVATE|AUTH|SESSION|COOKIE", re.I)
SAFE_ENV = {"SSH_AUTH_SOCK", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_SESSION_DESKTOP",
            "XDG_SESSION_CLASS", "XDG_SESSION_ID", "DESKTOP_SESSION", "GNOME_DESKTOP_SESSION_ID",
            "SESSION_MANAGER", "DBUS_SESSION_BUS_ADDRESS"}


def clean_env(extra_allowed: set[str] = frozenset()) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k in SAFE_ENV or k in extra_allowed or not SECRET_ENV.search(k)}
    env["PAGER"] = "cat"
    return env


def process_tree(pid: int) -> list[int]:
    """A process and all its descendants (/proc on Linux, pgrep elsewhere, e.g. macOS)."""
    pids = [pid]
    for p in pids:  # the list grows while we walk it
        try:
            with open(f"/proc/{p}/task/{p}/children") as f:
                pids.extend(int(child) for child in f.read().split())
        except FileNotFoundError:
            found = subprocess.run(["pgrep", "-P", str(p)], capture_output=True, text=True)
            pids.extend(int(child) for child in found.stdout.split())
        except OSError:
            pass
    return pids


TAG_VAR = "AGENT_SHELL_TAG"


def tagged_processes(tag: str) -> list[int]:
    """Processes whose environment carries this shell's tag: everything started from the shell,
    including background jobs that were re-parented away from it (`cmd &`, nohup, daemons)."""
    needle = f"{TAG_VAR}={tag}".encode()
    found = []
    if os.path.isdir("/proc/self"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) == os.getpid():
                continue
            try:
                with open(f"/proc/{entry}/environ", "rb") as f:
                    if needle in f.read().split(b"\0"):
                        found.append(int(entry))
            except OSError:     # gone, or not ours
                pass
        return found
    out = subprocess.run(["ps", "-Eww", "-axo", "pid=,command="], capture_output=True, text=True).stdout  # macOS
    return [int(line.split()[0]) for line in out.splitlines() if needle.decode() in line.split()
            and int(line.split()[0]) != os.getpid()]


class Shell:
    """One long-lived bash process, so state carries between commands like in a terminal.

    Each command is followed by a printf of a random marker plus the exit code and working
    directory; reading output until that marker appears is how we know the command finished.
    """

    def __init__(self, start_dir: str | None = None, env_allow: set[str] = frozenset(),
                 cancel: threading.Event | None = None) -> None:
        self.start_dir = start_dir or os.getcwd()
        self.cwd = self.start_dir
        self.env_allow = env_allow
        self.marker = f"__AGENT_DONE_{uuid.uuid4().hex}__"
        self.tag = uuid.uuid4().hex     # in the environment of everything this shell starts
        self.proc: subprocess.Popen | None = None
        # Set by whoever cancels (before calling stop()). Starting a command and stop() hold the
        # same lock, so a cancel from another thread either stops the command or prevents it.
        self.cancel = cancel
        self._lock = threading.Lock()

    def start(self, cwd: str) -> None:
        # Deliberately NOT a new session or process group: the shell keeps your terminal as
        # its controlling terminal, so sudo can prompt you for a password on it, and Ctrl+C
        # reaches whatever command is running.
        self.proc = subprocess.Popen(
            ["bash", "--noprofile", "--norc"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd if os.path.isdir(cwd) else self.start_dir,
            env={**clean_env(self.env_allow), TAG_VAR: self.tag},
        )

    def stop(self) -> None:
        with self._lock:
            pids = process_tree(self.proc.pid) if self.proc and self.proc.poll() is None else []
            for pid in dict.fromkeys(pids + tagged_processes(self.tag)):   # nothing it started outlives it
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):  # gone already, or a root process
                    pass
            if self.proc:
                self.proc.wait()
            self.proc = None

    def restart(self) -> str:
        self.stop()
        self.cwd = self.start_dir
        self.start(self.cwd)
        return f"Shell restarted: fresh session in {self.cwd}."

    def run(self, command: str, on_line=None) -> tuple[str, bool]:
        """Run a command. `on_line` receives each line of output live (for showing progress)."""
        # Group the command and give it no stdin, so nothing it runs can swallow the marker
        # line we send after it. Syntax-check first: an unclosed quote or heredoc would
        # otherwise make bash wait for more input forever.
        script = "{\n" + command + "\n} < /dev/null"
        check = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
        if check.returncode != 0:
            return f"Syntax error - command not run:\n{check.stderr.strip()}", True

        status = f'__rc=$?; printf "\\n{self.marker} %s %s\\n" "$__rc" "$PWD"\n'
        with self._lock:
            if self.cancel is not None and self.cancel.is_set():
                return "Cancelled before it ran.", True
            if self.proc is None or self.proc.poll() is not None:
                self.start(self.cwd)
            proc = self.proc   # local handle: another thread may call stop() to cancel
            try:
                proc.stdin.write(f"{script}\n{status}".encode())
                proc.stdin.flush()
                broken = False
            except BrokenPipeError:
                broken = True
        if broken:
            self.stop()
            self.start(self.cwd)
            return "The shell had exited; started a new one. Run the command again.", True

        end = f"\n{self.marker} ".encode()
        keep = OUTPUT_LIMIT // 2
        fd = proc.stdout.fileno()
        deadline = time.monotonic() + COMMAND_TIMEOUT
        head: bytes | None = None   # first `keep` bytes, once output gets too long to hold
        tail = bytearray()          # everything after the head, capped at the last `keep` bytes
        dropped = 0
        pending = b""               # live output: the incomplete last line

        def collected(upto: int | None = None) -> str:
            note = f"\n... [{dropped:,} bytes omitted] ...\n".encode() if dropped else b""
            return ((head or b"") + note + bytes(tail[:upto])).decode(errors="replace")

        def show_live(chunk: bytes) -> None:
            nonlocal pending
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                if not line.startswith(self.marker.encode()):
                    on_line(line.decode(errors="replace"))

        try:
            while True:
                pos = tail.find(end)
                if pos != -1 and tail.find(b"\n", pos + 1) != -1:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.stop()
                    self.start(self.cwd)
                    return (f"{collected()}\n[Timed out after {COMMAND_TIMEOUT}s and was killed. A new "
                            f"shell started in {self.cwd}; variables set earlier are gone.]"), True
                ready, _, _ = select.select([fd], [], [], remaining)
                if not ready:
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:  # EOF: the command ended the shell (`exit`) or it was cancelled
                    code = proc.wait()
                    if self.proc is proc:
                        self.proc = None
                    return (f"{collected()}\n[The shell exited with code {code}. A new one starts "
                            f"with the next command, in {self.cwd}.]"), True
                if on_line:
                    show_live(chunk)
                tail += chunk
                if len(tail) > 2 * keep and tail.find(end) == -1:
                    cut = 0
                    if head is None:
                        head, cut = bytes(tail[:keep]), keep
                    dropped += len(tail) - keep - cut
                    del tail[:-keep]
        except KeyboardInterrupt:
            # Ctrl+C: make sure the command and everything it started are gone, then let the
            # agent cancel the turn. The new shell keeps the working directory.
            self.stop()
            self.start(self.cwd)
            raise

        newline = tail.find(b"\n", pos + 1)
        _, code, self.cwd = tail[pos + 1:newline].decode(errors="replace").split(" ", 2)
        return f"{collected(pos)}\n[exit code {code}]", code != "0"
