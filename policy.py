"""The policy engine: every tool call is checked here before it runs.

Each call gets one of three answers - allow (run it), ask (show it to the user first) or deny
(refuse) - from, in order:
  1. your rules in config.json (deny beats ask beats allow)
  2. built-in hard denies (wiping disks, cloud metadata addresses)
  3. the permission mode (read-only / ask / edits / auto)
  4. escalations that always ask: secret-holding paths, risky commands, system files,
     requests that send data out
  5. "yes to all" grants you gave earlier in this session
"""
import fnmatch
import os
import re
import shlex
from dataclasses import dataclass

MODES = {
    "read-only": "reads only - every change is refused",
    "ask": "reads run automatically; every change asks first",
    "edits": "file edits in your home folder are automatic; commands and system files still ask",
    "auto": "runs unattended - only risky commands, sudo, secrets, system files and data sent out still ask",
}

# --- secrets ----------------------------------------------------------------------------------

SENSITIVE_DIRS = (
    "/.ssh/", "/.gnupg/", "/.aws/", "/.azure/", "/.kube/", "/.docker/", "/.config/gh/",
    "/.config/gcloud/", "/.password-store/", "/.local/share/keyrings/", "/.mozilla/",
    "/.config/google-chrome/", "/.config/chromium/", "/.config/BraveSoftware/", "/snap/brave/",
)
SENSITIVE_FILES = ("/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/etc/ssh/ssh_host_")
SENSITIVE_NAMES = {
    ".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".pgpass", ".vault-token",
    ".my.cnf", "credentials", "credentials.json", "credentials.db", "kubeconfig", "secrets.json",
}
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".kdbx")


def is_sensitive(path: str) -> bool:
    p = os.path.normpath(os.path.expanduser(path))
    name = os.path.basename(p)
    return (any(d in p + "/" for d in SENSITIVE_DIRS)
            or p.startswith(SENSITIVE_FILES)
            or name in SENSITIVE_NAMES
            or name.startswith(".env.")
            or name.endswith(SENSITIVE_SUFFIXES)
            or (name.startswith("id_") and not name.endswith(".pub")))


def command_paths(command: str, cwd: str) -> list[str]:
    """Every argument of a command, resolved against the shell's current directory.

    Resolving matters: after `cd ~/.aws`, the argument `config` really means ~/.aws/config.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        words = command.split()
    paths = [cwd]  # running anything inside a secret folder counts as touching it
    for word in words:
        if word.startswith("-") or set(word) <= set(";&|<>()"):
            continue
        value = word.split("=", 1)[1] if word.startswith("--") and "=" in word else word
        paths.append(os.path.join(cwd, os.path.expanduser(value)))
    return paths


# --- commands ---------------------------------------------------------------------------------

SHELL_OPERATORS = set(";&|<>`$\n\\(){}")

# Never run, whatever the mode or rules say - ask the user to do these themselves.
CATASTROPHIC = [
    (re.compile(r"\brm\s+(-\S*\s+)*(/|/\*|~|~/\*|\$HOME)(\s|$)"), "deletes the root or home directory"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "formats a filesystem"),
    (re.compile(r"\bdd\b.*\bof=/dev/(sd|nvme|hd|vd|mmcblk|disk)"), "writes raw data over a disk"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|vd|mmcblk)"), "overwrites a disk device"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"), "fork bomb"),
    (re.compile(r"\bchmod\s+(-\S+\s+)*-R\s+\S*7\S*\s+/(\s|$)"), "opens permissions on the whole system"),
]

# Always ask, even in auto mode.
RISKY = [
    (re.compile(r"\b(sudo|su|pkexec|doas)\b"), "runs as root"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b|\b(ba|z)?sh\s+<\(\s*(curl|wget)"),
     "runs code downloaded from the internet"),
    (re.compile(r"\brm\s+(-\S*[rRf]\S*\s+)"), "deletes files recursively or forcefully"),
    (re.compile(r"\b(reboot|shutdown|poweroff|halt)\b|\bsystemctl\s+(reboot|poweroff|suspend|hibernate)"),
     "restarts or powers off the machine"),
    (re.compile(r"\bsystemctl\s+(start|stop|restart|reload|enable|disable|mask|unmask|daemon-reload)\b"),
     "changes system services"),
    (re.compile(r"\b(apt|apt-get|dpkg|snap|flatpak|dnf|yum|pacman|brew)\b.*\b(install|remove|purge|upgrade|dist-upgrade|autoremove|-i|-r)\b"),
     "installs or removes software"),
    (re.compile(r"\b(pip3?|npm|pnpm|yarn|cargo|gem)\s+(install|uninstall|remove|add)\b|\buv\s+pip\s+install\b"),
     "installs packages"),
    (re.compile(r"\bgit\s+(push\b.*(-f|--force)|reset\s+--hard|clean\s+-\S*f|branch\s+-D)"),
     "rewrites or discards git history/changes"),
    (re.compile(r"\b(chmod|chown|chgrp)\s+(-\S*R)"), "changes permissions recursively"),
    (re.compile(r"\b(kill|pkill|killall)\b"), "kills processes"),
    (re.compile(r"\b(crontab|at)\b|\b(iptables|nft|ufw)\b|\b(useradd|userdel|usermod|passwd|chpasswd)\b"),
     "changes scheduled jobs, firewall or users"),
    (re.compile(r"\b(mount|umount|fdisk|parted|wipefs|sgdisk|cryptsetup|swapoff)\b"), "changes disks"),
    (re.compile(r"\bdocker\s+(rm|rmi|system\s+prune|volume\s+rm)\b"), "deletes containers or images"),
    (re.compile(r"\b(scp|rsync|sftp)\b.*\S+@\S+:|\bssh\s+\S+@"), "connects to another machine"),
]

# Programs whose "yes to all" would effectively allow anything.
NEVER_GRANT = {"sudo", "su", "pkexec", "doas", "rm", "dd", "bash", "sh", "zsh", "eval", "exec",
               "python", "python3", "node", "perl", "ruby", "curl", "wget", "ssh", "xargs", "env"}


# --- read-only commands ---------------------------------------------------------------------
# A command runs without asking (even in read-only mode, and in research subagents) only if every
# part of it - pipelines and ; && || chains included - is a read-only program used in a read-only
# way. Anything not understood here is simply not read-only: the normal rules apply to it.

PLAIN_READ_ONLY = {
    "cd", "pwd", "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "zgrep", "zcat", "wc", "stat",
    "df", "du", "free", "uptime", "uname", "arch", "lsblk", "lspci", "lsusb", "lsmod", "lscpu", "lsmem",
    "ps", "pgrep", "pidof", "whoami", "id", "groups", "who", "w", "last", "which", "type", "echo",
    "printf", "nproc", "getconf", "locale", "lsb_release", "printenv", "true", "false", "test", "[",
    "sleep", "seq", "cut", "tr", "nl", "fold", "rev", "tac", "comm", "join", "paste", "column", "expand",
    "numfmt", "basename", "dirname", "readlink", "realpath", "md5sum", "sha1sum", "sha256sum",
    "sha512sum", "cksum", "diff", "cmp", "strings", "xxd", "hexdump", "od", "jq", "findmnt", "lsns",
    "lslocks", "lsof", "ss", "netstat", "vmstat", "iostat", "mpstat", "pidstat", "lsattr", "getfacl",
    "blkid", "inxi", "glxinfo", "vulkaninfo", "upower", "loginctl", "coredumpctl", "resolvectl",
    "localectl", "uniq", "sort", "tree", "file", "date", "hostname", "dmesg", "nvidia-smi", "sensors",
    "swapon", "top", "journalctl", "systemctl", "systemd-analyze", "timedatectl", "hostnamectl",
    "ip", "nmcli", "git", "snap", "apt", "apt-cache", "dpkg", "dpkg-query", "flatpak", "pip", "pip3",
    "npm", "docker", "gsettings", "dconf", "xrandr", "find", "awk", "rg", "env", "ulimit", "sed",
}
SUBCOMMANDS = {   # programs that are read-only only with these subcommands (first non-flag word)
    "systemctl": {"status", "show", "cat", "list-units", "list-unit-files", "list-timers", "list-sockets",
                  "list-dependencies", "list-jobs", "is-active", "is-enabled", "is-failed", "get-default", None},
    "git": {"status", "log", "diff", "show", "blame", "rev-parse", "ls-files", "ls-tree", "describe",
            "shortlog", "reflog", "grep", "remote", "branch", "tag", "stash", "config", None},
    "snap": {"list", "info", "version", "changes", "connections", "services", "find"},
    "apt": {"list", "show", "policy", "search", "depends", "rdepends"},
    "apt-cache": {"policy", "show", "search", "depends", "rdepends", "showpkg", "stats"},
    "flatpak": {"list", "info", "remotes", "history"},
    "pip": {"list", "show", "freeze", "check"}, "pip3": {"list", "show", "freeze", "check"},
    "npm": {"ls", "list", "view", "outdated", "config"},
    "docker": {"ps", "images", "inspect", "logs", "version", "info", "stats", "top", "port", "system"},
    "gsettings": {"get", "list-schemas", "list-keys", "list-recursively", "range", "describe"},
    "dconf": {"read", "list", "dump"},
    "nmcli": {"general", "g", "device", "d", "dev", "connection", "c", "con", "radio", "networking", "n", None},
    "ip": {"addr", "a", "address", "link", "l", "route", "r", "neigh", "n", "rule", "maddr", "netns", None},
    "loginctl": {"list-sessions", "list-users", "list-seats", "show-session", "show-user", "session-status",
                 "user-status", "seat-status", None},
    "coredumpctl": {"list", "info", None},
    "resolvectl": {"status", "query", "statistics", "dns", "domain", None},
    "localectl": {"status", "list-locales", "list-keymaps", None},
    "timedatectl": {"status", "show", "list-timezones", "timesync-status", "show-timesync", None},
    "hostnamectl": {"status", None},
    "systemd-analyze": {"time", "blame", "critical-chain", "plot", "dot", "dump", "verify", "security",
                        "calendar", "timespan", "timestamp", "unit-paths", "condition", None},
    "dpkg": {"-l", "--list", "-L", "--listfiles", "-s", "--status", "-S", "--search", "-p", "--print-avail",
             "--get-selections", "--print-architecture"},
}
NON_FLAG_ACTIONS = {"ip", "nmcli"}   # `ip addr show` ok, `ip addr add` not: these words must not appear
CHANGING_WORDS = {"add", "del", "delete", "set", "change", "replace", "flush", "append", "modify", "up", "down",
                  "connect", "disconnect", "reload", "load", "edit", "clone", "import", "export", "rename", "on", "off"}
FORBIDDEN_FLAGS = {   # flags that make an otherwise read-only program change something (or run code)
    "find": ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"),
    "sort": ("-o", "--output"), "tree": ("-o",), "file": ("-C", "--compile"), "rg": ("--pre",),
    "date": ("-s", "--set"), "dmesg": ("-C", "-c", "--clear", "--read-clear", "-D", "-E", "-n", "--console-level"),
    "journalctl": ("--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate", "--flush", "--sync",
                   "--relinquish-var", "--smart-relinquish-var", "--setup-keys", "--update-catalog"),
    "nvidia-smi": ("-r", "--gpu-reset", "-pm", "--persistence-mode", "-pl", "--power-limit", "-c",
                   "--compute-mode", "-e", "--ecc-config", "-ac", "-rac", "-lgc", "-rgc", "-lmc", "-rmc",
                   "-am", "-mig", "--applications-clocks", "--reset-applications-clocks"),
    "sensors": ("-s", "--set"), "git": ("--output", "-o"), "upower": ("--monitor",),
    "lsof": ("+r", "-r"), "top": (), "swapon": ("-a", "--all", "-e"), "hostname": ("-F", "--file", "-b", "--boot"),
    "branch": ("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "--set-upstream-to", "-u",
               "--unset-upstream", "--edit-description", "-f", "--force"),
}
SAFE_REDIRECTS = re.compile(r"\s*(?:\d?>&[12]-?|&?\d?>\s*/dev/null|<\s*/dev/null)(?=\s|$|[;|&])")
WRAPPERS = {"timeout", "nice", "command", "time"}
SED_SIMPLE = set("pdqQnN=lPDhHgGxz")      # sed commands that only affect what is printed


def _sed_script_safe(script: str) -> bool:
    """A sed script that only prints: no w/W (write files), e (run commands), r/R (read other files)."""
    i, n = 0, len(script)
    while i < n:
        c = script[i]
        if c in " \t\n;{}!,$+~" or c.isdigit():
            i += 1
        elif c == "/":                                   # an address regex
            i += 1
            while i < n and script[i] != "/":
                i += 2 if script[i] == "\\" else 1
            i += 1
        elif c in "sy" and i + 1 < n:                    # s/re/repl/flags, y/abc/xyz/
            delim, i, parts = script[i + 1], i + 2, 0
            while i < n and parts < 2:
                if script[i] == "\\":
                    i += 2
                    continue
                parts += script[i] == delim
                i += 1
            while i < n and script[i] in "gpiImM0123456789":
                i += 1
            if i < n and script[i] in "weW":
                return False
        elif c in SED_SIMPLE:
            i += 1
        elif c in "aic":                                 # append/insert/change text: output only
            while i < n and script[i] != "\n":
                i += 1
        else:
            return False
    return True


def _flag_hit(words: list[str], flags) -> bool:
    return any(w == f or (f.startswith("--") and w.startswith(f + "=")) or
               (len(f) == 2 and f[0] == "-" and len(w) > 2 and w[0] == "-" and w[1] != "-" and w.startswith(f))
               for w in words for f in flags)


def _simple_read_only(words: list[str]) -> bool:
    if words[:1] == ["command"] and len(words) > 1 and words[1] in ("-v", "-V"):
        return True                                    # `command -v X` only looks X up
    while words and words[0] in WRAPPERS:              # timeout 5 cmd, nice cmd, command cmd
        words = words[1:]
        while words and (words[0].startswith("-") or words[0][:1].isdigit()):
            words = words[1:]
    if not words or "=" in words[0]:                   # FOO=bar cmd: an assignment, not read-only
        return False
    name, args = words[0], words[1:]
    if name not in PLAIN_READ_ONLY or _flag_hit(args, FORBIDDEN_FLAGS.get(name, ())):
        return False
    operands = [a for a in args if not a.startswith("-")]
    if name in SUBCOMMANDS:
        first = operands[0] if operands else None
        if name == "dpkg":
            return bool(args) and args[0].split("=")[0] in SUBCOMMANDS["dpkg"]
        if first not in SUBCOMMANDS[name]:
            return False
        if name in NON_FLAG_ACTIONS and CHANGING_WORDS & set(operands[1:]):
            return False
        if name == "git":
            if first in ("branch", "tag") and (_flag_hit(args, FORBIDDEN_FLAGS["branch"]) or len(operands) > 1):
                return False
            if first == "stash" and operands[1:2] not in ([], ["list"], ["show"]):
                return False
            if first == "remote" and operands[1:2] not in ([], ["show"], ["get-url"]):
                return False
            if first == "config" and not _flag_hit(args, ("--get", "--get-all", "--list", "-l", "--get-regexp")):
                return False
        if name == "systemctl" and first is None and not _flag_hit(args, ("--failed", "--state", "--type", "-t", "--all", "-a")):
            return False
        if name == "docker" and first == "stats" and "--no-stream" not in args:
            return False
        if name == "docker" and first == "system" and operands[1:2] != ["df"]:
            return False
        return True
    if name == "env":
        return not args                                # `env CMD` runs CMD
    if name == "awk":
        program = " ".join(args)
        return not any(x in program for x in ("system", ">", "|", "getline", "close("))
    if name == "sed":
        if _flag_hit(args, ("-i", "--in-place", "-f", "--file", "-s", "--separate")):
            return False
        scripts, rest, i = [], [], 0
        while i < len(args):
            if args[i] in ("-e", "--expression") and i + 1 < len(args):
                scripts.append(args[i + 1])
                i += 2
                continue
            if args[i].startswith("--expression="):
                scripts.append(args[i].split("=", 1)[1])
            elif not args[i].startswith("-"):
                rest.append(args[i])
            i += 1
        if not scripts and rest:
            scripts = [rest[0]]
        return bool(scripts) and all(_sed_script_safe(x) for x in scripts)
    if name == "uniq":
        return len(operands) <= 1                      # a second operand is an output file
    if name == "top":
        return "-b" in args or any(a.startswith("-b") for a in args)
    if name == "swapon":
        return not operands
    if name == "hostname":
        return not operands
    if name == "date":
        return all(o.startswith("+") for o in operands)    # date +FORMAT; anything else may set the clock
    if name == "ulimit":
        return all(a.startswith("-") for a in args) and len(args) <= 1 and args[:1] in ([], ["-a"])
    if name == "xrandr":
        return all(a in ("-q", "--query", "--listmonitors", "--listactivemonitors", "--verbose", "--current") for a in args)
    return True


def is_read_only(command: str) -> bool:
    """True when every part of the command only reads (see PLAIN_READ_ONLY and the rules above)."""
    if any(x in command for x in ("`", "$(", "<(", ">(", "<<")):
        return False
    text = SAFE_REDIRECTS.sub(" ", command).replace("\n", " ; ")
    text = text.replace("\\(", " _LPAREN_ ").replace("\\)", " _RPAREN_ ")   # find's escaped grouping
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    parts, current = [], []
    for token in tokens:
        if token in ("|", "||", "&&", ";"):
            parts.append(current)
            current = []
        elif set(token) <= set("();<>&|{}"):           # other redirections, subshells, background, groups
            return False
        else:
            current.append(token)
    parts.append(current)
    parts = [p for p in parts if p]
    return bool(parts) and all(_simple_read_only(p) for p in parts)


def first_match(patterns, text: str) -> str | None:
    for pattern, why in patterns:
        if pattern.search(text):
            return why
    return None


# --- the engine -------------------------------------------------------------------------------

SYSTEM_DIRS = ("/etc/", "/boot/", "/usr/", "/var/", "/opt/", "/lib", "/bin/", "/sbin/", "/srv/", "/root/", "/sys/", "/proc/")


@dataclass
class Decision:
    action: str            # "allow" | "ask" | "deny"
    reason: str = ""
    grantable: bool = False
    grant_label: str = ""


@dataclass
class Call:
    """What the policy needs to know about one tool call."""
    tool: str
    risk: str                       # read | write | exec | network | mcp | memory | meta
    subject: str = ""               # what rules match against: command, path, "GET url", ...
    paths: tuple[str, ...] = ()     # resolved paths the call touches
    method: str = ""                # network: HTTP method
    host: str = ""                  # network: host name
    private: bool = False           # network: resolves to a private/loopback address
    metadata: bool = False          # network: a cloud metadata address
    hints: dict | None = None       # mcp: readOnlyHint / destructiveHint / openWorldHint


class Policy:
    def __init__(self, mode: str = "ask", rules: dict | None = None) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; choose from {', '.join(MODES)}")
        self.mode = mode
        rules = rules or {}
        self.rules = {kind: list(rules.get(kind, [])) for kind in ("deny", "ask", "allow")}
        self.grants: set[tuple[str, str]] = set()

    # -- rules and grants --

    def _rule(self, call: Call) -> str | None:
        for kind in ("deny", "ask", "allow"):
            for rule in self.rules[kind]:
                tool_pattern, _, subject_pattern = rule.partition(":")
                if not fnmatch.fnmatchcase(call.tool, tool_pattern):
                    continue
                if not subject_pattern or fnmatch.fnmatchcase(call.subject, os.path.expanduser(subject_pattern)):
                    return kind
        return None

    def _grant_key(self, call: Call) -> tuple[str, str] | None:
        if call.risk == "exec":
            if any(ch in SHELL_OPERATORS for ch in call.subject):
                return None
            program = (call.subject.split() or [""])[0]
            return None if program in NEVER_GRANT else ("exec", program)
        if call.risk == "write":
            return ("write", "*")
        if call.risk == "network":
            return ("network", f"{call.method} {call.host}")
        if call.risk == "mcp":
            return ("mcp", call.tool)
        return None

    def grant(self, call: Call) -> None:
        if key := self._grant_key(call):
            self.grants.add(key)

    def _grant_label(self, call: Call) -> str:
        if call.risk == "exec":
            return f"`{call.subject.split()[0]}` commands"
        return {"write": "file changes", "network": f"{call.method} requests to {call.host}",
                "mcp": f"{call.tool}"}.get(call.risk, "this")

    # -- decision --

    def evaluate(self, call: Call, subagent: bool = False) -> Decision:
        rule = self._rule(call)
        if rule == "deny":
            return Decision("deny", "blocked by a deny rule in your config")
        if call.risk == "exec" and (why := first_match(CATASTROPHIC, call.subject)):
            return Decision("deny", f"never run automatically ({why}) - ask the user to run it themselves")
        if call.risk == "network" and call.metadata:
            return Decision("deny", "cloud metadata addresses are blocked")

        decision = self._base(call)
        if rule == "ask" and decision.action == "allow":
            decision = Decision("ask", "an ask rule in your config")
        elif rule == "allow" and decision.action != "deny":
            decision = Decision("allow", "an allow rule in your config")
        elif decision.action == "ask" and decision.grantable and self._grant_key(call) in self.grants:
            decision = Decision("allow", "you said yes to all of these this session")

        if subagent and decision.action != "allow":
            return Decision("deny", "subagents can only take actions that need no approval - "
                                    "say in your report what should be done instead")
        return decision

    def _base(self, call: Call) -> Decision:
        mode = self.mode
        secret = next((p for p in call.paths if is_sensitive(p)), None)
        if secret:
            return Decision("ask", f"touches a secret-holding path ({secret})")

        if call.risk in ("read", "meta", "memory"):
            return Decision("allow")

        if call.risk == "exec":
            if is_read_only(call.subject):
                return Decision("allow", "read-only command")
            if mode == "read-only":
                return Decision("deny", "read-only mode: commands that can change things are refused")
            if why := first_match(RISKY, call.subject):
                return Decision("ask", why)
            if mode == "auto":
                return Decision("allow", "auto mode")
            return Decision("ask", f"{mode} mode", grantable=True, grant_label=self._grant_label(call))

        if call.risk == "write":
            if mode == "read-only":
                return Decision("deny", "read-only mode: file changes are refused")
            home = os.path.expanduser("~") + "/"
            if any(not p.startswith(home) or p.startswith(SYSTEM_DIRS) for p in call.paths):
                return Decision("ask", "changes a file outside your home folder",
                                grantable=False)
            if mode in ("edits", "auto"):
                return Decision("allow", f"{mode} mode")
            return Decision("ask", "ask mode", grantable=True, grant_label="file changes")

        if call.risk == "network":
            if call.method in ("GET", "HEAD"):
                if call.private:
                    return Decision("ask", "a request to your own machine or local network")
                return Decision("allow", "reading a public URL")
            if mode == "read-only":
                return Decision("deny", "read-only mode: requests that send data are refused")
            return Decision("ask", f"{call.method} sends data out", grantable=True,
                            grant_label=self._grant_label(call))

        if call.risk == "mcp":
            hints = call.hints or {}
            if hints.get("readOnlyHint"):
                return Decision("allow", "the tool declares itself read-only")
            if mode == "read-only":
                return Decision("deny", "read-only mode: this tool may change things")
            if hints.get("destructiveHint") or hints.get("openWorldHint"):
                return Decision("ask", "the tool may delete data or act outside this machine",
                                grantable=True, grant_label=self._grant_label(call))
            if mode == "auto":
                return Decision("allow", "auto mode")
            return Decision("ask", f"{mode} mode", grantable=True, grant_label=self._grant_label(call))

        return Decision("ask", f"unknown risk class {call.risk!r}")
