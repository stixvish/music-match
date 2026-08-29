#!/bin/bash
# Auto-formats Python files with yapf after Claude edits them, so the
# commit gate rarely trips on formatting alone.

set -uo pipefail
cd "$CLAUDE_PROJECT_DIR" || exit 0

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
[ -z "$FILE_PATH" ] && exit 0

case "$FILE_PATH" in
  *.py) uv run yapf -i "$FILE_PATH" >/dev/null 2>&1 ;;
esac

exit 0
