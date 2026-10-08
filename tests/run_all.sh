#!/usr/bin/env bash
# Run every test suite. No API key needed: the model is a scripted fake (Anthropic API, or a local fake OpenRouter server).
cd "$(dirname "$0")" || exit 1
PY=../.venv/bin/python
failed=0
for suite in test_shell test_policy test_tools test_http test_mcp test_e2e test_openrouter test_features test_subagents test_acp; do
    printf '%-16s ' "$suite"
    if out=$(timeout 300 "$PY" "$suite.py" 2>&1); then
        echo "$out" | tail -1
    else
        echo "FAILED"; echo "$out" | grep -E "^FAIL|Error" | head -20; failed=1
    fi
done
# test_tty needs a real terminal: run it under `script` and type the password reply.
printf '%-16s ' "test_tty"
out=$( (sleep 2; printf 'hunter2\n') | timeout 30 script -qec "$PY test_tty.py" /dev/null 2>&1 | tr -d '\r')
if echo "$out" | grep -q "passed" && ! echo "$out" | grep -q "^FAIL"; then
    echo "$out" | grep "passed"
else
    echo "FAILED"; echo "$out" | grep -E "^FAIL" ; failed=1
fi
exit $failed
