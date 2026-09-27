import json

from pair.model import Command, Edit, Message, Session, Turn
from pair.util import ts

TOOL_OUTPUT_CHARS = 4000
MESSAGE_CHARS = 2000


def _item_text(item: dict) -> str:
    return "".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict))


def _added(change: dict) -> list[str]:
    diff = change.get("unified_diff") or ""
    added = [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    return added + (change.get("content") or "").splitlines()


def read_codex(path: str, messages: bool = False) -> Session | None:
    """Parse a Codex rollout. Returns None for imported copies of Claude sessions."""
    meta: dict | None = None
    turns: list[Turn] = []
    cur: Turn | None = None
    first_command = None

    def msg(role: str, text: str, when, uid=None):
        if messages and cur is not None and text:
            cur.messages.append(Message(role, text[:MESSAGE_CHARS], when, uid))

    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(r, dict):
                continue
            p = r.get("payload") or {}
            if r.get("type") == "session_meta":
                meta = meta or p
                continue
            if r.get("type") != "event_msg" or not isinstance(p, dict):
                continue
            ptype = p.get("type")
            if ptype == "task_started" and str(p.get("turn_id", "")).startswith("external-import-turn"):
                return None
            if ptype == "turn_aborted":
                if cur:
                    cur.interrupted = True
                continue
            if ptype != "item_completed" or meta is None or not r.get("timestamp"):
                continue
            item, when = p.get("item") or {}, ts(r["timestamp"])
            itype = item.get("type")
            if itype == "UserMessage":
                text = _item_text(item)
                if not text or text.startswith("<") or text.startswith("# AGENTS.md"):
                    continue
                if not turns:
                    first_command = text if text.startswith("/") else None
                cur = Turn("codex", meta["id"], p.get("turn_id") or item.get("id") or f"turn{len(turns)}", when,
                           text, branch=(meta.get("git") or {}).get("branch"))
                turns.append(cur)
                msg("user", text, when, item.get("id"))
            elif cur is None:
                continue
            elif itype == "AgentMessage":
                text = _item_text(item)
                if text:
                    cur.assistant_text.append(text)
                    msg("assistant", text, when, item.get("id"))
            elif itype == "CommandExecution":
                cmd = item.get("command") or [""]
                shell = cmd[-1] if isinstance(cmd, list) and cmd else str(cmd)
                out = (item.get("aggregated_output") or "")[:TOOL_OUTPUT_CHARS]
                cur.commands.append(Command(shell, when, out, item.get("exit_code") not in (0, None)))
                msg("tool", f"Bash: {shell}\n→ {out[:1500]}", when, item.get("id"))
            elif itype == "FileChange":
                for abs_path, change in (item.get("changes") or {}).items():
                    change = change if isinstance(change, dict) else {}
                    cur.edits.append(Edit(abs_path, when, _added(change)))
                    msg("tool", f"Edit {abs_path}:\n{(change.get('unified_diff') or '')[:1500]}", when, item.get("id"))
    if not meta or not meta.get("id"):
        return None
    return Session("codex", meta["id"], path, None, meta.get("cwd"), turns, [], False, first_command)
