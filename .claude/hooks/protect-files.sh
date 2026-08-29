#!/bin/bash
# Blocks edits to secrets, local state, and the music library itself.

set -uo pipefail

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
[ -z "$FILE_PATH" ] && exit 0

FILE_PATH="${FILE_PATH//\\//}"

PROTECTED=(".env" ".git/" "music_match.db" ".sqlite")
for pattern in "${PROTECTED[@]}"; do
  if [[ "$FILE_PATH" == *"$pattern"* ]]; then
    echo "Blocked: $FILE_PATH matches protected pattern '$pattern'" >&2
    exit 2
  fi
done

# Never write to audio files through the editor. Tag changes go through
# mutagen in the pipeline, never a direct file edit.
case "$FILE_PATH" in
  *.wav|*.aiff|*.aif|*.m4a|*.mp3|*.flac)
    echo "Blocked: $FILE_PATH is an audio file. Tags are written via mutagen, not edits." >&2
    exit 2
    ;;
esac

exit 0
