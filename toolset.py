"""Every tool the agent has, defined once in provider-neutral form. Add a tool here."""
from anthropic.lib.tools import ToolError

from http_tool import classify
from ledger import preview
from policy import Call, command_paths, is_read_only
from providers import BASH_SCHEMA, MEMORY_SCHEMA
from registry import Context, Registry, Tool
from tools import READ_LINES, FileTools

STRING = {"type": "string"}


def obj(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


def build_registry(session, shell, files: FileTools, subagent: bool = False, worker: bool = False,
                   label: str = "", cancel=None, interrupt=None) -> Registry:
    """The registry for the main agent, or (subagent=True) for a subagent with its own shell and
    cancel switch. A research subagent may only take actions that need no approval; a worker
    subagent also gets the file-editing tools, and its approvals go to the user like the main agent's."""
    reg = Registry(session.policy, session.ledger, session.ui, session.hooks, cancel or session.cancel,
                   interrupt=interrupt or session.interrupt, label=label, subagent_approvals=worker)
    resolve = lambda p: str(files.resolve(p))   # noqa: E731

    def path_call(tool: str, risk: str, default: str = "."):
        return lambda a, cwd: Call(tool, risk, subject=resolve(a.get("path", default)), paths=(resolve(a.get("path", default)),))

    # -- shell
    def bash_describe(a, cwd):
        command = a.get("command")
        if a.get("restart") or not isinstance(command, str):
            return Call("bash", "meta")
        return Call("bash", "exec", subject=command, paths=tuple(command_paths(command, cwd)))

    def bash_run(a, ctx: Context):
        if a.get("restart"):
            return shell.restart(), False
        command = a.get("command")
        if not isinstance(command, str) or not command.strip():
            return "Invalid input: expected a non-empty 'command' string.", True
        return shell.run(command, on_line=None if is_read_only(command) else ctx.on_output)

    reg.add(Tool("bash", "Run a command in the persistent shell.", BASH_SCHEMA, bash_run, "exec",
                 native="bash", acp_kind="execute", describe=bash_describe, subagent=True,
                 summary=lambda a: "restart shell" if a.get("restart") else f"$ {a.get('command')}"))

    # -- files and search
    reg.add(Tool(
        "read_file",
        "Read a text file with line numbers, or view an image (png, jpg, gif, webp). Relative paths start "
        "from the shell's current directory. For large files, use offset and limit.",
        obj({"path": STRING, "offset": {"type": "integer", "description": "1-based line number to start from"},
             "limit": {"type": "integer", "description": f"Number of lines (default {READ_LINES})"}}, ["path"]),
        lambda a, ctx: files.read_file(a["path"], a.get("offset", 1), a.get("limit", READ_LINES)),
        "read", acp_kind="read", describe=path_call("read_file", "read"),
        summary=lambda a: f"read {a['path']}", subagent=True, parallel_safe=True))
    reg.add(Tool(
        "edit_file",
        "Replace an exact string in a file you've read. old_string must match exactly, including whitespace, "
        "and be unique unless replace_all is true. The user sees a diff before it's written (unless their "
        "permission mode auto-approves); the original is backed up.",
        obj({"path": STRING, "old_string": STRING, "new_string": STRING,
             "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"}},
            ["path", "old_string", "new_string"]),
        lambda a, ctx: files.edit_file(a["path"], a["old_string"], a["new_string"], ctx.confirm, a.get("replace_all", False)),
        "write", acp_kind="edit", describe=path_call("edit_file", "write"), confirms_itself=True, subagent=worker))
    reg.add(Tool(
        "write_file",
        "Create a file, or replace an existing file's entire contents (read it first). The user sees a diff "
        "before it's written (unless their permission mode auto-approves); the original is backed up.",
        obj({"path": STRING, "content": {"type": "string", "description": "The complete new file contents"}},
            ["path", "content"]),
        lambda a, ctx: files.write_file(a["path"], a["content"], ctx.confirm),
        "write", acp_kind="edit", describe=path_call("write_file", "write"), confirms_itself=True, subagent=worker))
    reg.add(Tool(
        "glob", "Find files by name pattern, such as '*.conf' or '**/nginx/*.conf', newest first.",
        obj({"pattern": STRING, "path": {"type": "string", "description": "Directory to search (default: current)"}}, ["pattern"]),
        lambda a, ctx: files.glob(a["pattern"], a.get("path", ".")),
        "read", acp_kind="search", describe=path_call("glob", "read"),
        summary=lambda a: f"glob {a['pattern']} in {a.get('path', '.')}", subagent=True, parallel_safe=True))
    reg.add(Tool(
        "grep", "Search file contents with a regular expression (ripgrep). Returns matching lines with line "
                "numbers, or only file names with files_only.",
        obj({"pattern": STRING, "path": {"type": "string", "description": "File or directory to search (default: current)"},
             "glob": {"type": "string", "description": "Only search files matching this pattern, e.g. '*.log'"},
             "ignore_case": {"type": "boolean"}, "files_only": {"type": "boolean"}}, ["pattern"]),
        lambda a, ctx: files.grep(a["pattern"], a.get("path", "."), a.get("glob"), a.get("ignore_case", False), a.get("files_only", False)),
        "read", acp_kind="search", describe=path_call("grep", "read"),
        summary=lambda a: f"grep {a['pattern']!r} in {a.get('path', '.')}", subagent=True, parallel_safe=True))

    # -- http
    def http_describe(a, cwd):
        method = str(a.get("method", "GET")).upper()
        facts = classify(a.get("url", ""))
        return Call("http_request", "network", subject=f"{method} {a.get('url', '')}", method=method,
                    host=facts["host"], private=facts["private"], metadata=facts["metadata"])

    reg.add(Tool(
        "http_request",
        "Make an HTTP request and get the status, key headers and body (JSON is pretty-printed). To "
        "authenticate, give the name of a configured credential - its value is attached for you and never "
        "shown. Use bash + curl only for downloads or unusual protocols.",
        obj({"method": {"type": "string", "enum": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]}, "url": STRING,
             "headers": {"type": "object", "description": "Extra request headers"},
             "query": {"type": "object", "description": "Query-string parameters"},
             "json": {"description": "A JSON request body"},
             "body": {"type": "string", "description": "A raw text request body (instead of json)"},
             "credential": {"type": "string", "description": "Name of a configured credential"},
             "timeout": {"type": "integer", "description": "Seconds (default 30)"}}, ["method", "url"]),
        lambda a, ctx: session.http.request(a["method"], a["url"], a.get("headers"), a.get("query"), a.get("json"),
                                            a.get("body"), a.get("credential"), a.get("timeout", 30)),
        "network", acp_kind="fetch", describe=http_describe, subagent=True, parallel_safe=True,
        summary=lambda a: f"{a.get('method', 'GET')} {a.get('url')}" + (f"  [credential: {a['credential']}]" if a.get("credential") else "")))

    # -- MCP servers
    for t in session.mcp.tools:
        reg.add(Tool(
            t.api_name, t.description, t.input_schema, (lambda t: lambda a, ctx: session.mcp.call(t, a))(t), "mcp",
            describe=(lambda t: lambda a, cwd: Call(t.api_name, "mcp", subject=t.api_name, hints=t.hints))(t),
            summary=(lambda t: lambda a: f"{t.api_name} {preview(a)[:160]}")(t),
            subagent=True, parallel_safe=bool(t.hints.get("readOnlyHint"))))

    # -- web: executed by the model provider (OpenRouter or Anthropic), no client code
    reg.add_server_tool("web_search")
    reg.add_server_tool("web_fetch")

    reg.add(Tool(
        "skill", "Load the full instructions of a skill listed in the system prompt.",
        obj({"name": STRING}, ["name"]), lambda a, ctx: session.skills.load(a["name"]), "meta",
        acp_kind="read", summary=lambda a: f"load skill {a['name']}", subagent=True, parallel_safe=True))
    if subagent:
        return reg

    # -- main agent only
    def memory_run(a, ctx):
        try:
            result = session.memory.call(a)
        except ToolError as e:
            return str(e), True
        if a.get("command") != "view":
            session.ui.notice(f"  [memory: {a.get('command')} {a.get('path') or a.get('old_path')}]")
        return (result if isinstance(result, str) else list(result)), False

    reg.add(Tool("memory", "Persistent memory across sessions.", MEMORY_SCHEMA, memory_run, "memory", native="memory"))
    reg.add(Tool(
        "delegate",
        "Hand a self-contained task to a subagent with its own shell, and get its report back. It can't see "
        "this conversation, so include everything it needs to know. Several delegate calls in one response "
        "run at the same time - split independent work (check logs, run the tests, inspect config) across "
        "them. A subagent only investigates unless can_change is true; then it may run commands and edit "
        "files, with the user approving like they do for you. background: true returns at once so you can "
        "keep working; the report arrives automatically later (with a tool result, or before you finish).",
        obj({"task": STRING,
             "name": {"type": "string", "description": "A short label shown to the user, e.g. 'tests' or 'logs'"},
             "agent": {"type": "string", "description": "A subagent type from the system prompt (optional)"},
             "can_change": {"type": "boolean", "description": "Allow commands/edits that need approval (default false)"},
             "background": {"type": "boolean", "description": "Run without waiting for it (default false)"}},
            ["task"]),
        lambda a, ctx: session.subagents.delegate(a["task"], a.get("name", ""), a.get("agent", ""),
                                                  a.get("can_change"), a.get("background", False)),
        "meta", acp_kind="think", parallel_safe=True,
        summary=lambda a: f"subagent [{a.get('name') or a.get('agent') or 'sub'}]"
                          f"{' (can change things)' if a.get('can_change') else ''}"
                          f"{' (background)' if a.get('background') else ''}: {a['task'].strip().splitlines()[0][:100]}"))
    reg.add(Tool(
        "subagents",
        "Manage subagents: list (status, time, cost of each), wait (block until the given background "
        "subagents finish and get their reports), stop (end the given ones early; their partial reports follow).",
        obj({"action": {"type": "string", "enum": ["list", "wait", "stop"]},
             "ids": {"type": "array", "items": STRING, "description": "Subagent ids or names (default: all)"}},
            ["action"]),
        lambda a, ctx: session.subagents.manage(a["action"], a.get("ids")), "meta", acp_kind="think",
        summary=lambda a: f"subagents {a['action']} {' '.join(a.get('ids') or [])}".strip()))

    def ask_run(a, ctx):
        answer = session.ui.ask(a["question"], a.get("options"))
        if not answer:
            return ("No answer is available here (the user didn't answer, or this interface can't ask). Make a "
                    "reasonable choice and state the assumption you made."), False
        return f"The user answered: {answer}", False

    reg.add(Tool(
        "ask_user", "Ask the user a question and wait for the answer. Offer options when there are clear choices.",
        obj({"question": STRING, "options": {"type": "array", "items": STRING}}, ["question"]), ask_run, "meta"))
    reg.add(Tool(
        "update_todos", "Replace the to-do list shown to the user. Send the whole list every time.",
        obj({"todos": {"type": "array", "items": obj(
            {"content": STRING, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
            ["content", "status"])}}, ["todos"]),
        lambda a, ctx: (setattr(session, "todos", a["todos"]), session.ui.todos(a["todos"]), ("To-do list updated.", False))[2],
        "meta", acp_kind="think"))
    return reg
