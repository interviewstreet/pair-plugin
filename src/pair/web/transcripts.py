import re
from dataclasses import dataclass

from rank_bm25 import BM25Okapi

from pair.model import Turn
from pair.readers import parse_all

CHUNK_CHARS = 1600
EXCERPT_CHARS = 16000
TOKEN = re.compile(r"[a-z0-9_]{2,}")


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


@dataclass
class Chunk:
    ref: str
    turn_ref: str
    text: str


class TranscriptIndex:
    """The matched builder sessions of one review, parsed with messages, for search and excerpts."""

    def __init__(self, sessions: list[dict]):
        self.sessions = {s["id"]: s for s in sessions}
        paths = [p for s in sessions for p in s.get("paths", [])]
        self.turns: dict[str, Turn] = {}
        self.owner: dict[str, str] = {}
        for sess in parse_all(paths, messages=True):
            for t in sess.turns:
                if t.session_id not in self.sessions:
                    continue
                self.turns[t.ref] = t
                for m in t.messages:
                    if m.uid:
                        self.owner.setdefault(f"{t.agent}:{t.session_id}:{m.uid}", t.ref)
        self.chunks = [c for t in self.turns.values() for c in self._chunks(t)]
        self.bm25 = BM25Okapi([tokens(c.text) for c in self.chunks]) if self.chunks else None

    @staticmethod
    def _chunks(t: Turn) -> list[Chunk]:
        out, buf, first_uid = [], [], None
        size = 0
        for m in t.messages:
            line = f"{m.role}: {m.text}"
            if buf and size + len(line) > CHUNK_CHARS:
                out.append(Chunk(f"{t.agent}:{t.session_id}:{first_uid or t.turn_id}", t.ref, "\n".join(buf)))
                buf, size, first_uid = [], 0, None
            buf.append(line[:CHUNK_CHARS])
            size += len(line)
            first_uid = first_uid or m.uid
        if buf:
            out.append(Chunk(f"{t.agent}:{t.session_id}:{first_uid or t.turn_id}", t.ref, "\n".join(buf)))
        return out

    def _session_of(self, ref: str) -> dict:
        parts = ref.split(":", 2)
        return self.sessions.get(parts[1], {}) if len(parts) == 3 else {}

    def search(self, query: str, limit: int = 8) -> list[dict]:
        if not self.bm25:
            return []
        q = tokens(query)
        if not q:
            return []
        scores = self.bm25.get_scores(q)
        ranked = sorted(range(len(scores)), key=lambda i: -scores[i])
        out = []
        for i in ranked[:limit]:
            if scores[i] <= 0:
                break
            c = self.chunks[i]
            t = self.turns[c.turn_ref]
            s = self._session_of(c.ref)
            out.append({"ref": c.ref, "turn": c.turn_ref, "agent": t.agent, "session_role": s.get("role"),
                        "session_title": s.get("title"), "timestamp": t.started.isoformat(),
                        "snippet": c.text[:900], "score": round(float(scores[i]), 2)})
        return out

    def resolve(self, ref: str) -> tuple[Turn, str | None] | None:
        if ref in self.turns:
            return self.turns[ref], None
        owner = self.owner.get(ref)
        if owner:
            return self.turns[owner], ref.split(":", 2)[2]
        return None

    def excerpt(self, ref: str, max_chars: int = EXCERPT_CHARS) -> dict | None:
        """The turn a ref points into, trimmed around the referenced message."""
        hit = self.resolve(ref)
        if hit is None:
            return None
        t, uid = hit
        msgs = t.messages
        focus = next((i for i, m in enumerate(msgs) if uid and m.uid == uid), 0)
        lo = hi = focus
        size = len(msgs[focus].text) if msgs else 0
        while (lo > 0 or hi < len(msgs) - 1) and size < max_chars:
            if hi < len(msgs) - 1:
                hi += 1
                size += len(msgs[hi].text)
            if lo > 0 and size < max_chars:
                lo -= 1
                size += len(msgs[lo].text)
        picked = msgs[lo:hi + 1]
        if lo > 0 and msgs:
            picked = [msgs[0], *picked]
        s = self._session_of(t.ref)
        at = next((m.ts for m in msgs if uid and m.uid == uid), t.started)
        return {"ref": ref, "turn": t.ref, "agent": t.agent, "session_id": t.session_id,
                "session_role": s.get("role"), "session_title": s.get("title"), "timestamp": at.isoformat(),
                "truncated": lo > 1 or hi < len(msgs) - 1,
                # Messages left out between the turn's first message and the rest, and after the last one.
                "skipped": max(lo - 1, 0), "more_after": max(len(msgs) - 1 - hi, 0),
                "messages": [{"role": m.role, "text": m.text, "focus": bool(uid and m.uid == uid)} for m in picked]}
