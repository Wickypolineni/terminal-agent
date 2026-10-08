"""File, search, image and validation tools."""
import base64
import os
import subprocess
import tempfile
import time
from pathlib import Path

from harness import check, done
import tools
from shell import Shell

tmp = Path(tempfile.mkdtemp())
tools.BACKUP_ROOT = tmp / "backups"
sh = Shell(start_dir=str(tmp))
ft = tools.FileTools(sh)
yes = lambda preview: (True, "")
no = lambda preview: (False, "use port 9090 instead")
(tmp / "app.conf").write_text("port = 80\nhost = localhost\nport = 80\n")

# --- read_file
out, err = ft.read_file("app.conf")
check("read: line numbers", "     1\tport = 80" in out and not err, out)
out, err = ft.read_file("app.conf", offset=2, limit=1)
check("read: offset/limit header", out.startswith("[lines 2-2 of 3]") and "host" in out, out)
check("read: missing file", ft.read_file("nope.txt")[1])
check("read: directory refused", "directory" in ft.read_file(".")[0])
(tmp / "bin.dat").write_bytes(b"\x00\x01\x02")
check("read: binary refused", "binary" in ft.read_file("bin.dat")[0])
sh.run("mkdir -p sub && cd sub")
(tmp / "sub" / "x.txt").write_text("in sub\n")
check("read: relative paths follow the shell's cd", "in sub" in ft.read_file("x.txt")[0])
sh.run(f"cd {tmp}")

# --- edit_file
(tmp / "fresh.txt").write_text("a\n")
out, err = ft.edit_file("fresh.txt", "a", "b", yes)
check("edit: must read first", err and "Read" in out, out)
check("edit: old_string not found", "not found" in ft.edit_file("app.conf", "missing", "x", yes)[0])
check("edit: ambiguous match refused", "2 times" in ft.edit_file("app.conf", "port = 80", "port = 8080", yes)[0])
out, err = ft.edit_file("app.conf", "host = localhost", "host = 0.0.0.0", no)
check("edit: decline leaves the file alone and passes feedback",
      err and "port 9090" in out and "localhost" in (tmp / "app.conf").read_text(), out)
out, err = ft.edit_file("app.conf", "port = 80", "port = 8080", yes, replace_all=True)
check("edit: replace_all applied", not err and (tmp / "app.conf").read_text().count("8080") == 2, out)
backup = Path(out.split("backed up to ")[1].rstrip("."))
check("edit: original backed up", backup.read_text() == "port = 80\nhost = localhost\nport = 80\n")
time.sleep(0.01)
(tmp / "app.conf").write_text("changed by someone else\n")
check("edit: stale file refused", "changed since" in ft.edit_file("app.conf", "changed", "x", yes)[0])

# --- write_file
out, err = ft.write_file("new/dir/created.txt", "hello\n", yes)
check("write: new file + parent dirs", not err and (tmp / "new/dir/created.txt").read_text() == "hello\n", out)
check("write: existing file needs a read first", "Read" in ft.write_file("fresh.txt", "x\n", yes)[0])
ft.read_file("fresh.txt")
check("write: overwrite after read", not ft.write_file("fresh.txt", "x\n", yes)[1] and (tmp / "fresh.txt").read_text() == "x\n")
locked = tmp / "locked"
locked.mkdir()
(locked / "f.txt").write_text("x\n")
ft.read_file(str(locked / "f.txt"))
os.chmod(locked / "f.txt", 0o444)
out, err = ft.write_file(str(locked / "f.txt"), "y\n", yes)
check("write: unwritable file falls back to sudo (fails here: no terminal for a password)", err and "sudo" in out, out)

# --- glob / grep
(tmp / "logs").mkdir()
(tmp / "logs/old.log").write_text("ERROR disk full\n")
time.sleep(0.02)
(tmp / "logs/new.log").write_text("ok\nERROR timeout\n")
check("glob: newest first", ft.glob("*.log")[0].splitlines()[0].endswith("new.log"))
out, _ = ft.grep("ERROR", "logs")
check("grep: lines with numbers", "new.log:2:ERROR timeout" in out, out)
check("grep: files_only + ignore_case", len(ft.grep("error", "logs", files_only=True, ignore_case=True)[0].splitlines()) == 2)
check("grep: no matches", ft.grep("zzz_nothing", "logs")[0] == "No matches.")

# --- leak 3: grep must not return secret file contents
(tmp / ".aws").mkdir()
(tmp / ".aws/credentials").write_text("aws_secret_access_key = AKIAFAKESECRET\n")
(tmp / "notes.txt").write_text("my key is in AKIA format\n")
out, _ = ft.grep("AKIA", str(tmp))
check("leak 3: grep withholds matches from secret files", "AKIAFAKESECRET" not in out and "withheld" in out, out)
check("  -> but still returns normal matches", "notes.txt" in out, out)

# --- images
subprocess.run(["convert", "-size", "3072x1728", "gradient:blue-white", str(tmp / "screen.png")], check=True)
out, err = ft.read_file("screen.png")
data = base64.b64decode(out[1]["source"]["data"])
(tmp / "sent.jpg").write_bytes(data)
w, h = map(int, subprocess.run(["identify", "-format", "%w %h", str(tmp / "sent.jpg")],
                               capture_output=True, text=True).stdout.split())
check("image: screenshot shrunk to <=2000px JPEG", out[1]["source"]["media_type"] == "image/jpeg" and max(w, h) <= 2000, (w, h))

# --- validation
schema = {"type": "object", "required": ["a"], "properties": {
    "a": {"type": "string"}, "n": {"type": ["integer", "null"]}, "x": {"type": "number"},
    "l": {"type": "array", "items": {"type": "object", "required": ["s"],
                                     "properties": {"s": {"enum": ["p", "q"]}}}}}}
check("validate: ok", tools.validate({"a": "x", "n": None, "x": 1.5, "l": [{"s": "p"}]}, schema) is None)
check("validate: missing field", tools.validate({}, schema) == "input.a is required")
check("validate: wrong type", "should be string" in tools.validate({"a": 5}, schema))
check("validate: bool is not an integer", "should be" in tools.validate({"a": "x", "n": True}, schema))
check("validate: nested enum", "must be one of" in tools.validate({"a": "x", "l": [{"s": "z"}]}, schema))
check("validate: non-object input", "should be object" in tools.validate("oops", schema))

sh.stop()
done()
