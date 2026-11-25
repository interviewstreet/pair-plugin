#!/bin/sh
# Install Pair for one user from a fleet tool that runs scripts as root.
#
#   install-pair.sh <user>
#
# It runs Pair's installer as that user, with --no-login: nothing is uploaded
# until the user approves the Pair login, which opens at their first Claude
# Code or Codex session. Fails while the user has neither Claude Code nor Codex,
# so run it again until it succeeds.
set -eu

installer_url="https://github.com/interviewstreet/pair-plugin/releases/latest/download/install.sh"

user=${1:-}
[ -n "$user" ] || {
	echo "usage: $0 <user>" >&2
	exit 2
}
home=$(getent passwd "$user" | cut -d: -f6)
[ -n "$home" ] && [ -d "$home" ] || {
	echo "Pair: no home directory for $user" >&2
	exit 1
}

# The wrapper Pair's managed hooks call (mdm/templates/pair-hook.sh); unused
# unless the managed-hook templates are deployed.
managed_dir=/usr/local/share/pair
mkdir -p "$managed_dir"
chmod 755 "$managed_dir"
# Written beside it and renamed, so a hook running now never reads half a file.
cat >"$managed_dir/.pair-hook.sh.$$" <<'WRAPPER'
#!/bin/sh
# Pair's managed hooks call this, so the hook command is an absolute path
# under the managed directory. It runs the user's own Pair CLI, and does
# nothing for a user who doesn't have it yet.
pair="$HOME/.paircli/bin/paircli"
[ -x "$pair" ] || exit 0
exec "$pair" "$@"
WRAPPER
chmod 755 "$managed_dir/.pair-hook.sh.$$"
mv -f "$managed_dir/.pair-hook.sh.$$" "$managed_dir/pair-hook.sh"

# In /tmp, which the user can read; root's own TMPDIR may be private.
script=$(mktemp /tmp/pair-install-sh.XXXXXX)
trap 'rm -f "$script"' EXIT
curl -fsSL --proto =https --proto-redir =https -o "$script" "$installer_url"
chmod 644 "$script"
if [ "$(id -un)" = "$user" ]; then
	HOME="$home" sh "$script" --no-login </dev/null
else
	# The user's own TMPDIR, never root's: init installs from it.
	runuser -u "$user" -- env HOME="$home" TMPDIR="$home" sh "$script" --no-login </dev/null
fi
