"""Commands in the persistent shell can prompt on your terminal (how sudo asks for a password),
and what you type there never reaches the command output sent to Claude.

Needs a real terminal - run_all.sh runs it under `script` and types the reply for it."""
from harness import check, done
import shell as shell_module
from shell import Shell

shell_module.COMMAND_TIMEOUT = 10
s = Shell()
out, err = s.run('printf "password: " > /dev/tty; read -rs x < /dev/tty; echo "got ${#x} characters"')
check("a command can prompt on the terminal and read the reply", not err and "got 7 characters" in out, out)
check("the typed reply is not in the output sent to Claude", "hunter2" not in out, out)
s.stop()
done()
