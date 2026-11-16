# Pair notification branding

`pair.svg` is a vector rendition of Pair's existing `//` brand mark, as used by
the Pair FE `PairLogo` component. It does not reuse the unrelated legacy favicon.

Regenerate the embedded macOS icon on a Mac using the built-in AppKit tools:

```sh
swift scripts/build-notification-icon.swift internal/notify/assets/pair.svg internal/notify/assets/pair.icns
```

The macOS CLI embeds the icon, upstream helper executable and MIT license.
Users need neither Homebrew, Swift nor Xcode. Linux binaries do not embed these
assets. Setup builds a separate `~/Applications/Pair Notifications.app`, with
display name **Pair** and bundle identifier `sh.pair.notifications`. It retains
the upstream copyright/license and is locally ad-hoc signed using macOS's
built-in `codesign`; Gatekeeper and notification permission settings are not
disabled or bypassed. Existing Homebrew apps are never modified.

## Pinned executable provenance

- Official upstream: https://github.com/julienXX/terminal-notifier/releases/tag/3.1.0
- Archive: `terminal-notifier-3.1.0.zip`
- Archive SHA-256: `e969d4ae20287da1ba55495ae31dcedd8e9069deb8ce4eed24f6561a5fc3e4d5`
- Extracted member: `terminal-notifier.app/Contents/MacOS/terminal-notifier`
- Executable SHA-256: `64c8f37e21b2b39491c0adeb00219a92e0ba22b56355b979d8eed2bce3d1c858`
- Architectures: universal Intel x86_64 and Apple Silicon arm64.
- Upstream minimum macOS: 10.14.

Only that executable is vendored. The upstream Apple Terminal icon is **not**
included. No archive extraction or download takes place on a user's machine.
The installer verifies the pinned executable checksum before writing any app.
Changing the executable/icon/metadata triggers a managed helper refresh.

Before reusing an app, preparation signs a fresh staged copy with timestamps
disabled and compares the installed executable byte-for-byte against that
trusted signed copy, then verifies the installed app's signature. A marker or
valid ad-hoc signature alone does not establish executable integrity. This
comparison uses the signed copy because signing changes the upstream bytes.
Notification delivery selects the managed helper only after preparation confirms
it is installed or ready; failures or a busy preparation lock use the existing
external-notifier fallback instead.
