#!/bin/bash
# Git-native commit-msg hook (NOT a Claude Code hook — this fires for
# every commit regardless of source, because it reads the actual final
# message file git hands it, not a Bash command string).
#
# Enforces: short, lowercase, imperative subject; no body. Long
# explanations belong in the PR description, not the commit.

set -uo pipefail
MSG_FILE="$1"
SUBJECT=$(sed -n '1p' "$MSG_FILE")

fail() {
  echo "Commit rejected: $1" >&2
  echo "  subject: $SUBJECT" >&2
  echo "  Put detail in the PR description, not the commit." >&2
  exit 1
}

[ -z "$SUBJECT" ] && fail "empty subject line"

LOWER=$(echo "$SUBJECT" | tr '[:upper:]' '[:lower:]')
[ "$SUBJECT" != "$LOWER" ] && fail "subject must be lowercase"

[ ${#SUBJECT} -gt 60 ] && fail "subject over 60 chars, keep it short"

BODY_LINES=$(sed '1,2d' "$MSG_FILE" | grep -c '[^[:space:]]' || true)
[ "$BODY_LINES" -gt 0 ] && fail "no commit body — long descriptions go in the PR"

exit 0
