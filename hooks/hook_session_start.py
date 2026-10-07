#!/usr/bin/env python3
"""Start Pair CLI installation and device login when an agent session opens."""
from __future__ import annotations

import json
import os

from hook_pr_created import onboarding_message
from paircli_onboarding import start


def main() -> int:
    if os.environ.get("PAIR_RUN") == "1" or os.environ.get("PAIR_HOOK_DISABLE") == "1":
        return 0
    harness = "codex" if os.environ.get("PLUGIN_ROOT") else "claude-code"
    result = start(harness, dry_run=os.environ.get("PAIR_HOOK_DRY_RUN") == "1")
    message = onboarding_message(result, harness)
    print(json.dumps({
        "systemMessage": message,
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": message},
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
