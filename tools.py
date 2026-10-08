"""Client-side tools: files, search and images.

Whether a call may run at all is decided by policy.py before it gets here. File changes
receive a `confirm` callback that shows the diff (or approves silently, if policy allowed it).
"""
import base64
import difflib
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from policy import is_sensitive
from shell import OUTPUT_LIMIT, Shell

READ_LINES = 2000          # default lines returned by read_file
READ_CHAR_LIMIT = 40_000   # cap on one read_file result
MAX_LINE_CHARS = 2000      # longer lines are cut
GLOB_LIMIT = 200
DIFF_PREVIEW_LINES = 80
IMAGE_MAX_EDGE = 2000      # px; also keeps long sessions under the many-image limit
IMAGE_MAX_BYTES = 7_000_000  # raw bytes - the API limit is 10 MB once base64-encoded
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
BACKUP_ROOT = Path.home() / ".local/share/sysadmin-agent/backups"

Confirm = Callable[[str], tuple[bool, str]]   # (preview) -> (approved, user feedback)


# ---------------------------------------------------------------------------------------------
# Input validation. Tool inputs stream unvalidated (eager_input_streaming), so check them here.

_TYPES = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
          "array": (list,), "object": (dict,), "null": (type(None),)}


def _is_type(value, kind: str) -> bool:
    if kind not in _TYPES:
        return True   # unsupported schema keyword: be lenient
    if kind in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[kind])


def validate(value, schema: dict, where: str = "input") -> str | None:
    """Return an error message if `value` doesn't match a (simple) JSON schema, else None."""
    kind = schema.get("type")
    if kind:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_is_type(value, k) for k in kinds):
            return f"{where} should be {' or '.join(kinds)}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{where} must be one of {schema['enum']}"
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                return f"{where}.{key} is required"
        for key, sub in schema.get("properties", {}).items():
            if key in value and isinstance(sub, dict) and (error := validate(value[key], sub, f"{where}.{key}")):
                return error
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            if error := validate(item, schema["items"], f"{where}[{i}]"):
                return error
    return None


def truncate(text: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit:,} characters omitted] ...\n{text[-half:]}"


def declined(feedback: str) -> str:
    return "The user declined." + (f" They said: {feedback}" if feedback else "")


# ---------------------------------------------------------------------------------------------
# Images


def image_block(path: Path) -> dict:
    """Load an image as an API content block, shrinking big ones (screenshots) to JPEG.

    Raises ValueError with a readable message if it can't be sent.
    """
    media_type = IMAGE_TYPES[path.suffix.lower()]
    data = path.read_bytes()
    width = height = 0
    if shutil.which("identify"):
        probe = subprocess.run(["identify", "-format", "%w %h", f"{path}[0]"],
                               capture_output=True, text=True, timeout=30)
        if probe.returncode == 0:
            width, height = (int(n) for n in probe.stdout.split()[:2])
    too_big = max(width, height) > IMAGE_MAX_EDGE or len(data) > IMAGE_MAX_BYTES or not width
    if too_big and shutil.which("convert"):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "image.jpg"
            subprocess.run(["convert", f"{path}[0]", "-resize", f"{IMAGE_MAX_EDGE}x{IMAGE_MAX_EDGE}>",
                            "-quality", "85", str(out)], check=True, capture_output=True, timeout=60)
            data, media_type = out.read_bytes(), "image/jpeg"
    if len(data) > IMAGE_MAX_BYTES:
        raise ValueError(f"{path} is too large to send ({len(data):,} bytes); install ImageMagick "
                         "so it can be resized, or resize it first.")
    return {"type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()}}


# ---------------------------------------------------------------------------------------------
# Files and search


class FileTools:
    """read_file / write_file / edit_file / glob / grep.

    Like a careful human: a file must be read before it is changed, and a change is refused
    if the file was modified behind the agent's back since that read.
    """

    def __init__(self, shell: Shell, ui=None) -> None:
        self.shell = shell
        self.ui = ui
        self.seen: dict[str, int] = {}     # resolved path -> mtime_ns when last read or written
        self.backed_up: dict[str, Path] = {}
        self.backup_dir = BACKUP_ROOT / time.strftime("%Y-%m-%d_%H%M%S")

    def resolve(self, path: str) -> Path:
        # Relative paths follow the shell's current directory, so `cd` affects these tools too.
        return Path(self.shell.cwd, os.path.expanduser(path)).resolve()

    # -- reading -------------------------------------------------------------------------------

    def read_file(self, path: str, offset: int = 1, limit: int = READ_LINES):
        target = self.resolve(path)
        if not target.exists():
            return f"{target} does not exist.", True
        if target.is_dir():
            return f"{target} is a directory - use glob or `ls` instead.", True
        try:
            if target.suffix.lower() in IMAGE_TYPES:
                block = image_block(target)
                self.seen[str(target)] = target.stat().st_mtime_ns
                return [{"type": "text", "text": f"Image file {target}:"}, block], False
            raw = target.read_bytes()
        except PermissionError:
            return f"Permission denied: {target}. For a root-only file, use bash with sudo (e.g. sudo cat).", True
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            return f"Could not read {target}: {e}", True
        if b"\0" in raw[:8192]:
            return f"{target} is a binary file - inspect it with bash (file, strings, xxd).", True

        self.seen[str(target)] = target.stat().st_mtime_ns
        lines = raw.decode(errors="replace").splitlines()
        if not lines:
            return f"{target} is empty.", False
        start = max(offset, 1)
        chosen = lines[start - 1:start - 1 + max(limit, 1)]
        body = "\n".join(f"{n:>6}\t{line[:MAX_LINE_CHARS]}" for n, line in enumerate(chosen, start))
        end = start + len(chosen) - 1
        if len(body) > READ_CHAR_LIMIT:
            body = body[:READ_CHAR_LIMIT] + "\n... [cut - read a smaller range with offset/limit]"
        header = "" if (start == 1 and end == len(lines)) else f"[lines {start}-{end} of {len(lines)}]\n"
        return header + body, False

    # -- writing -------------------------------------------------------------------------------

    def _check_fresh(self, target: Path) -> str | None:
        key = str(target)
        if key not in self.seen:
            return f"Read {target} with read_file before changing it."
        if target.stat().st_mtime_ns != self.seen[key]:
            return f"{target} changed since you last read it - read it again first."
        return None

    def _diff(self, target: Path, old: str, new: str) -> str:
        diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                         f"{target} (current)", f"{target} (proposed)", lineterm="", n=2))
        if len(diff) > DIFF_PREVIEW_LINES:
            diff = diff[:DIFF_PREVIEW_LINES] + [f"... [{len(diff) - DIFF_PREVIEW_LINES} more diff lines]"]
        return "\n".join("    " + line for line in diff)

    def _backup(self, target: Path) -> Path | None:
        key = str(target)
        if key in self.backed_up:
            return self.backed_up[key]
        if not target.exists():
            return None
        dest = self.backup_dir / key.lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, dest)
        self.backed_up[key] = dest
        return dest

    def _write(self, target: Path, text: str) -> str | None:
        """Write the file; return an error message or None."""
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            return None
        except PermissionError:
            pass
        # Not writable as you (e.g. /etc): stage the text and copy it into place with sudo,
        # which prompts for your password on the terminal. `cp` onto an existing file keeps
        # that file's owner and permissions.
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".agent-edit") as tmp:
            tmp.write(text)
        try:
            if self.ui:
                self.ui.notice("  (needs root - sudo will ask for your password in the terminal)")
            out, failed = self.shell.run(f"sudo mkdir -p -- {shlex.quote(str(target.parent))} && "
                                         f"sudo cp -- {shlex.quote(tmp.name)} {shlex.quote(str(target))}")
        finally:
            os.unlink(tmp.name)
        return f"Writing with sudo failed:\n{out}" if failed else None

    def _apply(self, target: Path, old_text: str | None, new_text: str, verb: str, confirm: Confirm):
        if old_text is None:
            preview = f"create {target} ({len(new_text.splitlines())} lines)"
        else:
            preview = f"{verb} {target}:\n{self._diff(target, old_text, new_text)}"
        ok, feedback = confirm(preview)
        if not ok:
            return declined(feedback), True
        try:
            backup = self._backup(target)
        except OSError as e:
            return f"Could not back up {target} first, so nothing was changed: {e}", True
        if error := self._write(target, new_text):
            return error, True
        self.seen[str(target)] = target.stat().st_mtime_ns
        note = f" Original backed up to {backup}." if backup else ""
        return f"{'Updated' if old_text is not None else 'Created'} {target}.{note}", False

    def write_file(self, path: str, content: str, confirm: Confirm):
        target = self.resolve(path)
        if target.is_dir():
            return f"{target} is a directory.", True
        old_text = None
        if target.exists():
            if error := self._check_fresh(target):
                return error, True
            old_text = target.read_text(errors="replace")
        return self._apply(target, old_text, content, "overwrite", confirm)

    def edit_file(self, path: str, old_string: str, new_string: str, confirm: Confirm, replace_all: bool = False):
        target = self.resolve(path)
        if not target.is_file():
            return f"{target} does not exist.", True
        if error := self._check_fresh(target):
            return error, True
        if old_string == new_string:
            return "old_string and new_string are identical - nothing to change.", True
        text = target.read_text(errors="replace")
        count = text.count(old_string)
        if count == 0:
            return ("old_string was not found. It must match the file exactly, including "
                    "whitespace and indentation - read the file again and copy it."), True
        if count > 1 and not replace_all:
            return (f"old_string appears {count} times. Include more surrounding lines to make it "
                    "unique, or set replace_all to change every occurrence."), True
        new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        return self._apply(target, text, new_text, "edit", confirm)

    # -- searching -----------------------------------------------------------------------------

    def glob(self, pattern: str, path: str = "."):
        base = self.resolve(path)
        if not base.is_dir():
            return f"{base} is not a directory.", True
        if shutil.which("rg"):
            run = subprocess.run(["rg", "--files", "--hidden", "--no-ignore", "--glob", pattern, str(base)],
                                 capture_output=True, text=True, timeout=60)
            found = run.stdout.splitlines()
        else:
            found = [str(p) for p in base.glob(pattern) if p.is_file()]

        def mtime(p: str) -> float:
            try:
                return os.stat(p).st_mtime
            except OSError:
                return 0
        found.sort(key=mtime, reverse=True)   # most recently modified first
        if not found:
            return "No files matched.", False
        more = f"\n... and {len(found) - GLOB_LIMIT} more - narrow the pattern" if len(found) > GLOB_LIMIT else ""
        return "\n".join(found[:GLOB_LIMIT]) + more, False

    def grep(self, pattern: str, path: str = ".", glob: str | None = None,
             ignore_case: bool = False, files_only: bool = False):
        base = self.resolve(path)
        if shutil.which("rg"):
            cmd = ["rg", "--line-number", "--no-heading", "--color=never", "--hidden",
                   "--glob", "!.git", "--max-columns", "400", "--max-columns-preview"]
            if glob:
                cmd += ["--glob", glob]
        else:
            cmd = ["grep", "-rnI"] + ([f"--include={glob}"] if glob else [])
        cmd += (["-i"] if ignore_case else []) + (["-l"] if files_only else []) + ["--", pattern, str(base)]
        try:
            run = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=120)
        except subprocess.TimeoutExpired:
            return "Search timed out after 120s - narrow the path or add a glob filter.", True
        if run.returncode == 1:
            return "No matches.", False
        if run.returncode not in (0, 1) and not run.stdout:
            return f"Search failed: {run.stderr.strip()}", True

        # Never pass along matches from secret-holding files, even when searching a parent
        # folder (searching ~ would otherwise surface lines from ~/.aws/credentials).
        kept, withheld = [], 0
        for line in run.stdout.splitlines():
            file_path = line if files_only else line.split(":", 1)[0]
            if is_sensitive(file_path):
                withheld += 1
            else:
                kept.append(line)
        note = (f"\n[{withheld} match(es) in secret-holding files withheld - read those with "
                "read_file if truly needed; the user will be asked]") if withheld else ""
        if not kept:
            return ("No matches outside secret-holding files." + note).strip(), False
        return truncate("\n".join(kept)) + note, False
