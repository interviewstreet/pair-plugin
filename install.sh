#!/bin/sh
# Pair installer, for macOS and Linux (on Windows, use WSL):
#
#   curl -fsSL https://github.com/interviewstreet/pair-plugin/releases/latest/download/install.sh | sh
#
# It downloads the paircli release for this machine and checks it against the
# release manifest the Pair plugin's bootstrap uses (schema, revoked list,
# exact release URL, SHA-256, size cap), then hands over to the verified
# binary: `paircli init` installs it to ~/.paircli/bin, sets up hooks in your
# coding agents, approves them in Codex and logs you in. Arguments are passed
# to init, e.g. `sh -s -- --no-login`.
#
# Everything is inside main, called on the last line, so a partial download
# runs nothing.

main() {
	set -eu
	manifest_url="https://github.com/interviewstreet/pair-plugin/releases/latest/download/latest.json"
	release_prefix="https://github.com/interviewstreet/pair-plugin/releases/download"
	# The marker paircli and the plugin's bootstrap.py write after installing.
	marker_url="$manifest_url"
	max_bytes=104857600
	proto="=https"
	# Test-only overrides, refused unless PAIR_INSTALL_TEST=1.
	if [ -n "${PAIR_INSTALL_MANIFEST_URL:-}${PAIR_INSTALL_RELEASE_PREFIX:-}" ]; then
		[ "${PAIR_INSTALL_TEST:-}" = 1 ] || fail "PAIR_INSTALL_MANIFEST_URL and PAIR_INSTALL_RELEASE_PREFIX are for tests only"
		manifest_url="${PAIR_INSTALL_MANIFEST_URL:-$manifest_url}"
		release_prefix="${PAIR_INSTALL_RELEASE_PREFIX:-$release_prefix}"
		proto="=http,https"
	fi

	need curl
	need uname
	need mktemp
	command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 ||
		fail "sha256sum or shasum is required to install Pair"
	key=$(platform)

	tmp=$(mktemp -d "${TMPDIR:-/tmp}/pair-install.XXXXXX")
	trap 'rm -rf "$tmp"' EXIT
	trap 'exit 1' HUP INT TERM

	fetch "$manifest_url" "$tmp/latest.json" 1048576
	manifest=$(tr -d '\n\r\t' <"$tmp/latest.json")
	[ "$(json_string "$manifest" schema)" = "paircli-release/v1" ] ||
		fail "the paircli release has an unsupported manifest schema"
	version=$(json_string "$manifest" version)
	printf '%s\n' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.]+)?$' ||
		fail "the paircli release has an invalid version"
	revoked=$(printf '%s' "$manifest" | sed -n 's/.*"revoked"[[:space:]]*:[[:space:]]*\[\([^]]*\)\].*/\1/p')
	# Like bootstrap.py: a revoked field that is not a list is an invalid release.
	if [ -z "$revoked" ] && printf '%s' "$manifest" | grep -q '"revoked"' &&
		! printf '%s' "$manifest" | grep -Eq '"revoked"[[:space:]]*:[[:space:]]*\[[[:space:]]*\]'; then
		fail "the paircli release has an invalid revoked list"
	fi
	! is_revoked "$version" || fail "paircli release $version is revoked"
	artifact=$(printf '%s' "$manifest" | sed -n "s/.*\"$key\"[[:space:]]*:[[:space:]]*{\([^}]*\)}.*/\1/p")
	[ -n "$artifact" ] || fail "paircli $version is not available for $key"
	url=$(json_string "$artifact" url)
	sha=$(json_string "$artifact" sha256 | tr 'A-F' 'a-f')
	[ "$url" = "$release_prefix/v$version/paircli-$key" ] ||
		fail "the paircli binary URL for $key must reference this release and platform"
	printf '%s\n' "$sha" | grep -Eq '^[0-9a-f]{64}$' || fail "the paircli release has an invalid sha256 for $key"

	bin="$HOME/.paircli/bin/paircli"
	if keep_installed; then
		rm -rf "$tmp"
		trap - EXIT HUP INT TERM
		exec "$bin" init --enroll --grant-codex-trust "$@"
	fi

	say "downloading paircli $version for $key"
	fetch "$url" "$tmp/paircli" "$max_bytes"
	size=$(wc -c <"$tmp/paircli" | tr -d ' ')
	[ "$size" -gt 0 ] && [ "$size" -le "$max_bytes" ] || fail "the paircli binary has an invalid size"
	[ "$(sha256 "$tmp/paircli")" = "$sha" ] || fail "the paircli binary sha256 does not match the release manifest"
	chmod 755 "$tmp/paircli"
	"$tmp/paircli" version >/dev/null 2>&1 ||
		fail "cannot run programs from ${TMPDIR:-/tmp} (mounted noexec?); run the installer again with TMPDIR=\$HOME"
	# init installs the verified binary under ~/.paircli/bin/.bootstrap.lock and
	# removes $tmp; from here on it owns the directory.
	trap - EXIT HUP INT TERM
	exec "$tmp/paircli" init --install-self "$tmp" --enroll --grant-codex-trust "$@"
}

say() { printf 'Pair: %s\n' "$*" >&2; }

fail() {
	say "$*"
	exit 1
}

need() { command -v "$1" >/dev/null 2>&1 || fail "$1 is required to install Pair"; }

platform() {
	case "$(uname -s)" in
	Darwin) os=darwin ;;
	Linux) os=linux ;;
	*) fail "Pair supports macOS and Linux only; on Windows, use WSL" ;;
	esac
	case "$(uname -m)" in
	arm64 | aarch64) arch=arm64 ;;
	x86_64 | amd64) arch=amd64 ;;
	*) fail "Pair does not support $(uname -m)" ;;
	esac
	# A shell under Rosetta reports x86_64 on Apple silicon.
	if [ "$os" = darwin ] && [ "$arch" = amd64 ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ]; then
		arch=arm64
	fi
	printf '%s-%s\n' "$os" "$arch"
}

fetch() {
	curl -fsSL --proto "$proto" --proto-redir "$proto" --max-time 300 --max-filesize "$3" -o "$2" "$1" ||
		fail "could not download $1"
}

# json_string DOC KEY prints the string value of KEY in the flat JSON object
# text DOC. Every value read this way is validated after, so a value this
# cannot read fails closed.
json_string() {
	printf '%s' "$1" | sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p"
}

is_revoked() {
	case ",$(printf '%s' "$revoked" | tr -d ' "')," in
	*",$1,"*) return 0 ;;
	esac
	return 1
}

sha256() {
	if command -v sha256sum >/dev/null 2>&1; then
		sha256sum "$1" | cut -d ' ' -f 1
	elif command -v shasum >/dev/null 2>&1; then
		shasum -a 256 "$1" | cut -d ' ' -f 1
	else
		fail "sha256sum or shasum is required to install Pair"
	fi
}

# keep_installed succeeds when ~/.paircli/bin/paircli came from this release
# source (the migration marker) and is this release or newer, and is not
# revoked: then nothing is downloaded and the installed binary runs init.
keep_installed() {
	[ -x "$bin" ] || return 1
	grep -Eq "\"manifest_url\"[[:space:]]*:[[:space:]]*\"$marker_url\"" "$HOME/.paircli/bin/release-source.json" 2>/dev/null || return 1
	installed=$("$bin" version 2>/dev/null | sed -n 's/^paircli //p')
	[ -n "$installed" ] || return 1
	! is_revoked "$installed" || return 1
	[ "$installed" = "$version" ] || newer "$installed" "$version"
}

# newer A B succeeds when release A is newer than B (major.minor.patch).
newer() {
	awk -v a="$1" -v b="$2" 'BEGIN {
		split(a, x, /[.+-]/); split(b, y, /[.+-]/)
		for (i = 1; i <= 3; i++) {
			if (x[i] + 0 > y[i] + 0) exit 0
			if (x[i] + 0 < y[i] + 0) exit 1
		}
		exit 1
	}'
}

main "$@"
