import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pair.config import Config, load_config
from pair.github import GhError
from pair.web.pair_agent import PairAgent
from pair.web.reviews import Review, ReviewError, ReviewNotFound, list_reviews
from pair.web.transcripts import TranscriptIndex

WEB_OUT = Path(__file__).resolve().parents[3] / "web" / "out"
PLACEHOLDER = Path(__file__).with_name("placeholder.html")
DEV_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]


class DraftPatch(BaseModel):
    body: str | None = None
    status: str | None = None


class DraftCreate(BaseModel):
    path: str
    side: str = "additions"
    line: int
    start_line: int | None = None
    body: str


def create_app(cfg: Config | None = None, client_factory=None) -> FastAPI:
    cfg = cfg or load_config()
    dev = os.environ.get("PAIR_DEV") == "1"
    reviews: dict[str, Review] = {}
    agents: dict[str, PairAgent] = {}
    indexes: dict[str, TranscriptIndex] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        for agent in list(agents.values()):
            await agent.close()

    app = FastAPI(title="pair", lifespan=lifespan, docs_url="/api/docs" if dev else None, redoc_url=None,
                  openapi_url="/api/openapi.json")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    if dev:
        app.add_middleware(CORSMiddleware, allow_origins=DEV_ORIGINS, allow_methods=["*"], allow_headers=["*"])

    def origin_ok(origin: str | None) -> bool:
        if not origin:
            return True
        u = urlparse(origin)
        if u.hostname not in ("127.0.0.1", "localhost"):
            return False
        return u.port == cfg.port or (dev and u.port == 3000)

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and not origin_ok(request.headers.get("origin")):
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    def review(key: str) -> Review:
        try:
            r = Review(key, cfg)
        except (ValueError, ReviewNotFound):
            raise HTTPException(404, f"no brief for {key}")
        cached = reviews.get(key)
        if cached and cached.dir == r.dir:
            return cached
        reviews[key] = r
        indexes.pop(key, None)
        return r

    def index_for(r: Review) -> TranscriptIndex:
        ix = indexes.get(r.key)
        if ix is None:
            ix = indexes[r.key] = TranscriptIndex(r.evidence.get("sessions", []))
        return ix

    async def agent_for(r: Review) -> PairAgent:
        agent = agents.get(r.key)
        if agent and agent.review.dir != r.dir:
            await agent.close()
            agent = None
        if agent is None:
            kwargs = {"client_factory": client_factory} if client_factory else {}
            agent = agents[r.key] = PairAgent(r, cfg, lambda: index_for(r), **kwargs)
        return agent

    # ------------------------------------------------------------ REST

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/reviews")
    def get_reviews():
        return list_reviews()

    @app.get("/api/reviews/{key}")
    def get_review(key: str):
        return review(key).meta()

    @app.get("/api/reviews/{key}/brief")
    def get_brief(key: str):
        return review(key).brief

    @app.get("/api/reviews/{key}/diff", response_class=PlainTextResponse)
    def get_diff(key: str):
        return review(key).diff_text

    @app.get("/api/reviews/{key}/risky-hunks")
    def get_risky(key: str):
        return review(key).risky

    @app.get("/api/reviews/{key}/excerpt")
    async def get_excerpt(key: str, ref: str = Query(...)):
        r = review(key)
        if ref == "pr:body":
            return {"ref": ref, "agent": None, "session_id": None, "session_role": None, "timestamp": None,
                    "messages": [{"role": "pr", "text": r.pr.get("body") or ""}]}
        if not ref.startswith(("claude:", "codex:")):
            raise HTTPException(400, "excerpts exist for claude: and codex: refs only")
        ix = await asyncio.to_thread(index_for, r)
        ex = ix.excerpt(ref)
        if ex is None:
            raise HTTPException(404, f"unknown ref {ref}")
        return ex

    @app.get("/api/reviews/{key}/file", response_class=PlainTextResponse)
    async def get_file(key: str, side: str = Query(..., pattern="^(base|head)$"), path: str = Query(...)):
        r = review(key)
        f = r.diff_files.get(path)
        if f is None:
            raise HTTPException(404, f"{path} isn't in this PR")
        if (side == "base" and f.status == "added") or (side == "head" and f.status == "removed"):
            raise HTTPException(404, f"{path} doesn't exist on the {side} side")
        try:
            return await asyncio.to_thread(r.file_text, side, (f.old_path or path) if side == "base" else path)
        except ReviewNotFound as e:
            raise HTTPException(404, str(e))
        except ReviewError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/reviews/{key}/drafts")
    def get_drafts(key: str):
        return review(key).drafts()

    @app.post("/api/reviews/{key}/drafts", status_code=201)
    def create_draft(key: str, new: DraftCreate):
        """A comment the user wrote on the page."""
        try:
            return review(key).add_draft(new.path, new.side, new.line, new.body, new.start_line,
                                         status="approved", author="user")
        except ReviewError as e:
            raise HTTPException(400, str(e))

    @app.patch("/api/reviews/{key}/drafts/{draft_id}")
    def patch_draft(key: str, draft_id: str, patch: DraftPatch):
        try:
            return review(key).update_draft(draft_id, patch.body, patch.status)
        except ReviewNotFound:
            raise HTTPException(404, f"no draft {draft_id}")
        except ReviewError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/reviews/{key}/post")
    async def post(key: str):
        r = review(key)
        try:
            url = await asyncio.to_thread(r.post)
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except ReviewError as e:
            raise HTTPException(400, str(e))
        except GhError as e:
            raise HTTPException(502, f"GitHub refused the review: {e}")
        return {"review_url": url, "drafts": r.drafts()}

    @app.post("/api/reviews/{key}/close")
    async def close(key: str):
        r = review(key)
        agent = agents.pop(key, None)
        if agent:
            await agent.close()
        indexes.pop(key, None)
        removed = await asyncio.to_thread(r.remove_worktree)
        return {"closed": True, "worktree_removed": removed}

    # ------------------------------------------------------------ chat

    @app.websocket("/api/reviews/{key}/chat")
    async def chat(ws: WebSocket, key: str):
        if not origin_ok(ws.headers.get("origin")):
            await ws.close(code=4403)
            return
        try:
            r = review(key)
        except HTTPException:
            await ws.close(code=4404)
            return
        await ws.accept()
        agent = await agent_for(r)
        await ws.send_json({"type": "history", "messages": agent.history, "busy": agent.busy})
        agent.sockets.add(ws)
        warm = asyncio.create_task(agent.start())
        warm.add_done_callback(lambda t: t.cancelled() or t.exception())
        try:
            while True:
                msg = await ws.receive_json()
                kind = msg.get("type") if isinstance(msg, dict) else None
                if kind == "user_message":
                    text = (msg.get("text") or "").strip()
                    if not text:
                        await ws.send_json({"type": "error", "message": "empty message"})
                    elif agent.busy:
                        await ws.send_json({"type": "error", "message": "the pair agent is still answering; "
                                                                        "interrupt it or wait"})
                    else:
                        agent.ask(text, msg.get("selection"))
                elif kind == "interrupt":
                    await agent.interrupt()
                else:
                    await ws.send_json({"type": "error", "message": f"unknown message type {kind!r}"})
        except WebSocketDisconnect:
            pass
        finally:
            agent.sockets.discard(ws)

    # ------------------------------------------------------------ pages

    if WEB_OUT.is_dir():
        app.mount("/", StaticFiles(directory=WEB_OUT, html=True), name="web")
    else:
        @app.get("/", include_in_schema=False)
        def root():
            return RedirectResponse("/review/")

        @app.get("/review/", response_class=HTMLResponse, include_in_schema=False)
        def placeholder():
            return PLACEHOLDER.read_text()

    return app
