import subprocess
"""The persistent shell: state, edge cases, secret scrubbing, Ctrl+C."""
import os
import signal
import threading
import time

from harness import check, done
import shell as shell_module
from shell import Shell, process_tree

shell_module.COMMAND_TIMEOUT = 3
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-FAKE-TEST"
os.environ["GITHUB_TOKEN"] = "ghp_FAKE"
os.environ["MY_APP_PASSWORD"] = "hunter2"
s = Shell(start_dir="/tmp")


def run(name, cmd, test):
    out, err = s.run(cmd)
    check(name, test(out, err), (out, err))


run("cd persists", "cd /tmp", lambda o, e: not e)
run("  -> pwd sees it", "pwd", lambda o, e: o.startswith("/tmp\n"))
run("export persists", "export FOO=bar", lambda o, e: not e)
run("  -> echo sees it", "echo $FOO", lambda o, e: o.startswith("bar\n"))
run("function persists", "f() { echo func; }", lambda o, e: not e)
run("  -> call it", "f", lambda o, e: o.startswith("func\n"))
run("failing command reports exit code", "ls /nonexistent", lambda o, e: e and "[exit code 2]" in o)
run("no trailing newline", "printf abc", lambda o, e: o == "abc\n[exit code 0]")
run("no output", "true", lambda o, e: o == "\n[exit code 0]")
run("stdin-hungry command can't hang", "cat", lambda o, e: not e)
run("heredoc", "cat <<EOF\nhello\nEOF", lambda o, e: o.startswith("hello\n"))
run("unclosed quote = syntax error, not a hang", "echo 'oops", lambda o, e: e and "Syntax error" in o)
run("trailing backslash doesn't hang", "echo hi \\", lambda o, e: True)
run("  -> shell still alive", "echo alive", lambda o, e: o.startswith("alive\n"))
run("exit kills shell", "exit 3", lambda o, e: e and "exited with code 3" in o)
run("  -> respawns in same dir", "pwd", lambda o, e: o.startswith("/tmp\n"))
run("timeout kills command", "echo before; sleep 30", lambda o, e: e and "Timed out" in o and "before" in o)
run("  -> respawns in same dir", "pwd", lambda o, e: o.startswith("/tmp\n"))
shell_module.OUTPUT_LIMIT = 2000
run("huge output is capped while reading", "seq 1 300000",
    lambda o, e: "bytes omitted" in o and o.startswith("1\n2\n") and "300000" in o and len(o) < 3000)
shell_module.OUTPUT_LIMIT = 20_000

# secrets
run("ANTHROPIC_API_KEY not visible", "echo \"[$ANTHROPIC_API_KEY]\"", lambda o, e: o.startswith("[]"))
run("GITHUB_TOKEN / *_PASSWORD not visible", "env | grep -c -E 'GITHUB_TOKEN|MY_APP_PASSWORD' || true",
    lambda o, e: o.startswith("0"))
run("PATH and HOME still there", "echo $PATH | grep -c bin; echo $HOME", lambda o, e: o.startswith("1\n/"))
allowed = Shell(start_dir="/tmp", env_allow={"GITHUB_TOKEN"})
out, _ = allowed.run("echo $GITHUB_TOKEN")
check("shell_env_allow re-admits a chosen variable", out.startswith("ghp_FAKE"), out)
allowed.stop()

# background jobs + restart
run("background job", "sleep 100 & echo started", lambda o, e: "started" in o)
background = process_tree(s.proc.pid)[1:]
s.restart()
check("restart kills background jobs", not [p for p in background if os.path.exists(f"/proc/{p}")])

# Ctrl+C: SIGINT arrives while a command runs
s.run("cd /var")
threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
started = time.monotonic()
try:
    s.run("sleep 30")
    interrupted = False
except KeyboardInterrupt:
    interrupted = True
check("Ctrl+C interrupts a running command quickly", interrupted and time.monotonic() - started < 3)
out, _ = s.run("pwd")
check("  -> shell restarted, working directory kept", out.startswith("/var\n"), out)

s.stop()
# background jobs that escape the process tree (re-parented) are still stopped with the shell
sh = Shell()
sh.run("( sleep 314.159 & ) ; nohup sleep 271.828 >/dev/null 2>&1 &")
time.sleep(0.3)
def alive(pattern: str) -> bool:
    """A live (not zombie) process with exactly this command line."""
    out = subprocess.run(["ps", "-eo", "stat=,args="], capture_output=True, text=True).stdout
    return any(line.split(None, 1)[1].strip() == pattern for line in out.splitlines()
               if line.strip() and not line.strip().startswith("Z"))
check("re-parented background jobs exist while the shell runs", alive("sleep 314.159") and alive("sleep 271.828"))
sh.stop()
time.sleep(0.3)
check("... and are gone after stop()", not alive("sleep 314.159") and not alive("sleep 271.828"))

done()
