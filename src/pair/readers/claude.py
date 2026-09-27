import json
import os
import re

from pair.model import Command, Edit, Message, Plan, Question, Session, Turn
from pair.util import ts

EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
PLAN_PATH = re.compile(r"(/plans?/[^/]+\.md$|(^|/)[^/]*\bplan[^/]*\.md$)", re.I)
INTERRUPT = "[Request interrupted"
COMMAND_NAME = re.compile(r"<command-name>\s*(/?[^<\s]+)\s*</command-name>")
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
# Slash commands that change session settings rather than asking for work. Their arguments never start a turn.
HOUSEKEEPING = {
    "/add-dir", "/agents", "/clear", "/compact", "/config", "/context", "/cost", "/doctor", "/effort", "/exit",
    "/export", "/fast", "/help", "/hooks", "/ide", "/login", "/logout", "/mcp", "/memory", "/model",
    "/permissions", "/plugin", "/rename", "/resume", "/rewind", "/status", "/statusline", "/theme",
    "/usage", "/usage-credits", "/vim",
}
TOOL_OUTPUT_CHARS = 4000
MESSAGE_CHARS = 2000


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _added_lines(name: str, inp: dict) -> list[str]:
    parts = [inp.get("content") or "", inp.get("new_string") or "", inp.get("new_source") or ""]
    parts += [e.get("new_string") or "" for e in inp.get("edits") or [] if isinstance(e, dict)]
    return "\n".join(p for p in parts if p).splitlines()


def _tool_summary(name: str, inp: dict) -> str:
    if name == "Bash":
        return f"Bash: {inp.get('command', '')}"
    if name in EDIT_TOOLS or name == "Read":
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        if name == "Edit":
            return f"Edit {path}: -{inp.get('old_string', '')[:300]!r} +{inp.get('new_string', '')[:600]!r}"
        return f"{name} {path}"
    if name in ("Grep", "Glob"):
        return f"{name} {inp.get('pattern', '')} {inp.get('path', '') or ''}".strip()
    return f"{name}: {json.dumps(inp, ensure_ascii=False)[:600]}"


def _command(text: str) -> tuple[str, str] | None:
    m = COMMAND_NAME.search(text)
    if not m:
        return None
    name = m[1] if m[1].startswith("/") else "/" + m[1]
    args = COMMAND_ARGS.search(text)
    return name, (args[1].strip() if args else "")


def read_claude(path: str, messages: bool = False) -> Session | None:
    turns: list[Turn] = []
    cur: Turn | None = None
    title = cwd = session_id = first_command = None
    prs: list[str] = []
    tool_uses: dict[str, tuple[str, dict]] = {}
    subagent_file = "/subagents/" in path
    saw_user = False

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
            typ = r.get("type")
            if typ == "fork-context-ref":
                session_id = r.get("parentSessionId") or session_id
                continue
            session_id = session_id or r.get("sessionId")
            cwd = cwd or r.get("cwd")
            when = ts(r["timestamp"]) if r.get("timestamp") else None

            if typ == "ai-title":
                title = r.get("aiTitle") or title
            elif typ == "pr-link":
                if r.get("prUrl"):
                    prs.append(r["prUrl"])
            elif typ == "file-history-delta":
                b = r.get("backup") or {}
                if cur and when and b.get("realParentDir") and r.get("trackingPath"):
                    p = os.path.join(b["realParentDir"], os.path.basename(r["trackingPath"]))
                    cur.edits.append(Edit(p, when))
            elif typ == "assistant" and cur and when:
                for block in (r.get("message") or {}).get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text"):
                        cur.assistant_text.append(block["text"])
                        msg("assistant", block["text"], when, r.get("uuid"))
                    elif block.get("type") == "tool_use":
                        name, inp = block.get("name") or "", block.get("input") or {}
                        tool_uses[block.get("id")] = (name, inp)
                        msg("tool", _tool_summary(name, inp), when, r.get("uuid"))
                        fpath = inp.get("file_path") or inp.get("notebook_path")
                        if name in EDIT_TOOLS and fpath:
                            cur.edits.append(Edit(fpath, when, _added_lines(name, inp)))
                        ref = f"claude:{session_id}:{r.get('uuid')}"
                        if name == "ExitPlanMode" and inp.get("plan"):
                            cur.plans.append(Plan(inp["plan"], ref))
                        if name == "Write" and fpath and PLAN_PATH.search(fpath) and inp.get("content"):
                            cur.plans.append(Plan(inp["content"], ref, fpath))
            elif typ == "user" and when:
                content = (r.get("message") or {}).get("content")
                is_results = isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
                text = _text_of(content)
                if is_results:
                    if cur:
                        _attach_results(cur, r, content, tool_uses, session_id, when, msg)
                    continue
                if INTERRUPT in text:
                    if cur:
                        cur.interrupted = True
                        msg("user", text.strip(), when, r.get("uuid"))
                    continue
                if not text or r.get("isMeta") or r.get("isCompactSummary"):
                    continue
                prompt = None
                if text.startswith("<"):
                    cmd = _command(text)
                    if cmd:
                        if not saw_user:
                            first_command = f"{cmd[0]} {cmd[1]}".strip()
                        saw_user = True
                        if cmd[1] and cmd[0] not in HOUSEKEEPING:
                            prompt = f"{cmd[0]} {cmd[1]}"
                else:
                    prompt = text
                if prompt is None:
                    continue
                if not saw_user:
                    first_command = prompt if prompt.startswith("/") else None
                saw_user = True
                cur = Turn("claude", session_id, r.get("uuid") or f"line{len(turns)}", when, prompt,
                           subagent=subagent_file or bool(r.get("isSidechain")), branch=r.get("gitBranch"))
                turns.append(cur)
                msg("user", prompt, when, r.get("uuid"))

    if session_id is None:
        return None
    return Session("claude", session_id, path, title, cwd, turns, prs, subagent_file, first_command)


def _attach_results(cur: Turn, r: dict, content: list, tool_uses: dict, session_id: str, when, msg) -> None:
    result = r.get("toolUseResult") if isinstance(r.get("toolUseResult"), dict) else {}
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        name, inp = tool_uses.get(block.get("tool_use_id"), (None, {}))
        if name == "Bash":
            out = (result.get("stdout") or "") + (result.get("stderr") or "")
            if not out:
                out = _text_of(block.get("content"))
            cur.commands.append(Command(inp.get("command", ""), when, out[:TOOL_OUTPUT_CHARS], bool(block.get("is_error"))))
            msg("tool", f"→ {out[:1500]}", when, r.get("uuid"))
            for p in (result.get("bashEditDiff") or {}).get("changedFiles") or []:
                if isinstance(p, str):
                    cur.edits.append(Edit(p, when))
        elif name == "AskUserQuestion":
            answers = result.get("answers") or {}
            for q in inp.get("questions") or []:
                if not isinstance(q, dict):
                    continue
                question = Question(q.get("question") or "", [o.get("label") for o in q.get("options") or []
                                                              if isinstance(o, dict)],
                                    answers.get(q.get("question")), f"claude:{session_id}:{r.get('uuid')}", when)
                cur.questions.append(question)
                msg("tool", f"Asked: {question.question}\nAnswer: {question.answer}", when, r.get("uuid"))
