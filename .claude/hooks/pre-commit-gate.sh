#!/bin/bash
# Blocks Claude-initiated commits unless the full quality gate passes.
# Pairs with the git-native pre-commit hook, which catches commits from
# any other source (terminal, VS Code git panel).
#
# IMPORTANT: this hook is registered on ALL Bash calls (Claude Code's
# hook schema can't match on command content, only tool name), so this
# script itself must filter down to git-commit invocations and exit 0
# immediately for everything else. Do not add slow checks above this
# filter, or every `ls`/`cat`/etc. Claude runs pays the full-suite cost.

set -uo pipefail
cd "$CLAUDE_PROJECT_DIR" || exit 0

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

case "$COMMAND" in
  *"git commit"*) ;;
  *) exit 0 ;;
esac

fail() {
  echo "Commit blocked: $1" >&2
  exit 2
}

uv run yapf -d -r src/ tests/ >/dev/null 2>&1 \
  || fail "yapf found unformatted files. Run: uv run yapf -i -r src/ tests/"

uv run pylint src/ tests/ >/dev/null 2>&1 \
  || fail "pylint failed. Run: uv run pylint src/ tests/"

uv run mypy src/ >/dev/null 2>&1 \
  || fail "mypy failed. Run: uv run mypy src/"

uv run pytest tests/unit/ -q >/dev/null 2>&1 \
  || fail "unit tests failed. Run: uv run pytest tests/unit/"

exit 0
