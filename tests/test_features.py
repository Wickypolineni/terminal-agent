"""Hooks, skills, and the command line (headless -p / --json / --continue), on the OpenRouter provider."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

work = Path(tempfile.mkdtemp())
os.environ["SYSADMIN_AGENT_DATA"] = str(work / "data")
os.environ["SYSADMIN_AGENT_CONFIG"] = str(work / "no-config.json")
os.environ["OPENROUTER_API_KEY"] = "test-key"

from harness import ROOT, check, done  # noqa: E402
from fake_openrouter import FakeOpenRouter, config, text_reply, tool_reply  # noqa: E402
import engine  # noqa: E402
import providers  # noqa: E402
from ui import HeadlessUI  # noqa: E402

providers.RETRY_DELAYS = [0, 0, 0]
os.chdir(work)
fake = FakeOpenRouter()


def reset(*script):
    fake.requests.clear()
    fake.script[:] = list(script)


def text_of(message: dict) -> str:
    content = message["content"]
    return content if isinstance(content, str) else "".join(p.get("text", "") for p in content)


def script(name: str, body: str) -> str:
    path = work / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return str(path)


# ---- 1. hooks: UserPromptSubmit context, PreToolUse block, PostToolUse context, Stop asks for more
hooks = {
    "UserPromptSubmit": [{"command": script("prompt.sh", "echo 'today is test-day'\n")}],
    "PreToolUse": [{"matcher": "bash", "command": script("pre.sh",
                    "if grep -q 'rm -rf'; then echo 'no deleting here' >&2; exit 2; fi\n"
                    "echo '{\"additionalContext\": \"pre-checked\"}'\n")}],
    "PostToolUse": [{"matcher": "*", "command": script("post.sh", "cat > /dev/null; echo '{\"additionalContext\": \"post-note\"}'\n")}],
    "Stop": [{"command": script("stop.sh", f"if [ ! -e {work}/stopped ]; then touch {work}/stopped; "
                                "echo 'run the tests first' >&2; exit 2; fi\n")}],
}
reset(tool_reply([("h1", "bash", {"command": "rm -rf build"})]),
      tool_reply([("h2", "bash", {"command": "ls"})]),
      text_reply("done"),
      text_reply("tests pass"))
s = engine.Session(config(fake.url, hooks=hooks), HeadlessUI(quiet=True), cwd=str(work))
final, stop = engine.run_turn(s, "clean up")
b = fake.bodies()
check("1. UserPromptSubmit output added to the prompt", "today is test-day" in text_of(b[0]["messages"][-1]))
blocked = text_of(b[1]["messages"][-1])
check("1. PreToolUse exit 2 blocks the call with its reason", "PreToolUse hook blocked it" in blocked and "no deleting here" in blocked, blocked)
ran = text_of(b[2]["messages"][-1])
check("1. Pre/PostToolUse context appended to the result", "pre-checked" in ran and "post-note" in ran, ran)
check("1. Stop hook sent the agent back to work", len(b) == 4 and "run the tests first" in text_of(b[3]["messages"][-1]))
check("1. final answer is the one after the Stop hook", (final, stop) == ("tests pass", "end_turn"), (final, stop))
s.close()

reset(text_reply("should not be asked"))
s = engine.Session(config(fake.url, hooks={"UserPromptSubmit": [{"command": script("deny.sh", "echo 'not today' >&2; exit 2\n")}]}),
                   HeadlessUI(quiet=True), cwd=str(work))
check("1. UserPromptSubmit exit 2 blocks the request", engine.run_turn(s, "hi") == ("", "blocked") and not fake.requests)
s.close()

# ---- 2. skills: listed in the system prompt, loaded on demand with their files
skill = work / ".agent/skills/deploy"
skill.mkdir(parents=True)
(skill / "SKILL.md").write_text("---\nname: deploy\ndescription: Deploy the app to staging\n---\n# Deploy\nRun ./ship.sh then check health.\n")
(skill / "ship.sh").write_text("echo shipped\n")
reset(tool_reply([("k1", "skill", {"name": "deploy"})]), text_reply("ok"))
s = engine.Session(config(fake.url), HeadlessUI(quiet=True), cwd=str(work))
engine.run_turn(s, "deploy it")
system = text_of(fake.bodies()[0]["messages"][0])
check("2. skill name + description in the system prompt", "- deploy: Deploy the app to staging" in system)
loaded = text_of(fake.bodies()[1]["messages"][-1])
check("2. skill tool returns the instructions and its files",
      "Run ./ship.sh then check health." in loaded and str(skill / "ship.sh") in loaded and "description:" not in loaded, loaded)
s.close()

# ---- 3. the command line: -p, --json, --continue, exit codes, missing key
cfg_file = work / "cli-config.json"
cfg_file.write_text(json.dumps({"openrouter": {"base_url": fake.url, "api_key_file": str(work / "no.key"),
                                                "env_file": str(work / "no.env")}}))
env = {**os.environ, "SYSADMIN_AGENT_CONFIG": str(cfg_file), "SYSADMIN_AGENT_DATA": str(work / "cli-data")}


def cli(*args, env=env):
    return subprocess.run([sys.executable, str(ROOT / "agent.py"), *args], capture_output=True, text=True,
                          env=env, cwd=work, timeout=120)


reset(text_reply("hello from headless"))
out = cli("-p", "say hello", "--json")
result = json.loads(out.stdout) if out.stdout.strip().startswith("{") else {}
check("3. -p --json prints one JSON result", result.get("result") == "hello from headless"
      and result.get("stop_reason") == "end_turn" and out.returncode == 0, (out.stdout, out.stderr[-500:]))
reset(text_reply("second answer"))
out = cli("-c", "-p", "and again")
check("3. -p prints just the final answer", out.stdout.strip() == "second answer" and out.returncode == 0, out.stdout)
sent = fake.bodies()[0]["messages"]
check("3. --continue resumes the last session's history",
      [m["role"] for m in sent] == ["system", "user", "assistant", "user"] and text_of(sent[1]) == "say hello", [m["role"] for m in sent])
reset(tool_reply([("x1", "bash", {"command": "make build"})]), text_reply("needs approval: run make build"))
out = cli("-p", "build it")
check("3. headless refuses approvals and reports on stderr", "needs approval, refused" in out.stderr and out.returncode == 0, out.stderr[-300:])
reset()
out = cli("-p", "hi", env={k: v for k, v in env.items() if k != "OPENROUTER_API_KEY"})
check("3. no API key: clear message, exit 1", out.returncode == 1 and "No OpenRouter API key" in out.stdout and not fake.requests,
      (out.returncode, out.stdout, out.stderr[-300:]))

fake.close()
done()
