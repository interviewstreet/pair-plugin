#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
URL="https://github.com/julienXX/terminal-notifier/releases/download/3.1.0/terminal-notifier-3.1.0.zip"
ARCHIVE_SHA="e969d4ae20287da1ba55495ae31dcedd8e9069deb8ce4eed24f6561a5fc3e4d5"
BINARY_SHA="64c8f37e21b2b39491c0adeb00219a92e0ba22b56355b979d8eed2bce3d1c858"

curl --fail --location --proto '=https' --proto-redir '=https' --max-time 60 \
  --max-filesize 1048576 --output "$SCRATCH/helper.zip" "$URL"
printf '%s  %s\n' "$ARCHIVE_SHA" "$SCRATCH/helper.zip" | shasum -a 256 -c -
unzip -p "$SCRATCH/helper.zip" terminal-notifier.app/Contents/MacOS/terminal-notifier > "$SCRATCH/helper"
printf '%s  %s\n' "$BINARY_SHA" "$SCRATCH/helper" | shasum -a 256 -c -
install -m 644 "$SCRATCH/helper" "$ROOT/internal/notify/assets/terminal-notifier-macos"
