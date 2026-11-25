#!/bin/sh
# Install Pair for the logged-in user, from an MDM that runs scripts as root
# (Jamf Pro, Kandji, Microsoft Intune for macOS).
#
# It runs Pair's installer as that user, with --no-login: nothing is uploaded
# until the user approves the Pair login, which opens at their first Claude
# Code or Codex session. Exits 1 when no user is logged in, or (from Pair's
# installer) when the user has neither Claude Code nor Codex yet, so run it as
# a recurring policy until it succeeds.
set -eu

installer_url="https://github.com/interviewstreet/pair-plugin/releases/latest/download/install.sh"

user=$(stat -f %Su /dev/console 2>/dev/null || true)
case "$user" in
"" | root | loginwindow | _mbsetupuser)
	echo "Pair: no user is logged in; try again later" >&2
	exit 1
	;;
esac
home=$(dscl . -read "/Users/$user" NFSHomeDirectory | sed 's/^NFSHomeDirectory: //')
[ -d "$home" ] || {
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
# The user's own TMPDIR, never root's: init installs from it.
sudo -u "$user" env HOME="$home" TMPDIR="$home" sh "$script" --no-login </dev/null
