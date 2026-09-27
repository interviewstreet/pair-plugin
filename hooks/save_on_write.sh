#!/bin/sh
# PostToolUse hook on Write. This runs after every Write in every session, so it only starts Python
# for brief output files (<pair home>/briefs/<key>/<sha>/out/<section>.json).
input=$(cat)
case "$input" in
  *'/briefs/'*'/out/'*'.json'*) ;;
  *) exit 0 ;;
esac
printf '%s' "$input" | exec "$(dirname "$0")/../bin/pair-brief" hook-save
