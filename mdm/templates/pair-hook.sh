#!/bin/sh
# Pair's managed hooks call this, so the hook command is an absolute path
# under the managed directory. It runs the user's own Pair CLI, and does
# nothing for a user who doesn't have it yet.
pair="$HOME/.paircli/bin/paircli"
[ -x "$pair" ] || exit 0
exec "$pair" "$@"
