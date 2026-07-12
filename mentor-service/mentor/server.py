"""FastAPI service — exposes the mentor brain to the VS Code extension.

Sessions live in an in-memory store for this local prototype. A shared deployment needs
authentication, expiration, rate limits, and an external session store.

Endpoints
  GET  /health                      -> liveness + which LLM mode is active
  POST /session   {goal}            -> {session_id, blueprint[]}
  POST /event     {session_id, type, code, idle_seconds?}
                  type in: completed | error | stuck | hover
                                    -> MentorMessage
"""
from __future__ import annotations

from typing import Literal, Optional

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel, Field
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "FastAPI/pydantic not installed. Run: pip install -r requirements.txt"
    ) from e

from .config import Config
from .llm import LLMClient, LLMError
from .learner_model import LearnerModel
from .mentor import Mentor
from .state import SessionStore
from .curriculum_catalog import catalog_by_id, discover_catalogs, load_catalog

config = Config.from_env()
llm = LLMClient(config)
learner = LearnerModel(config.learner_db or None)
mentor = Mentor(llm, learner)
store = SessionStore()
catalog = load_catalog()

app = FastAPI(title="CodeTutor Mentor Service", version="0.1.0")


class SessionRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=500)
    learner_id: str = Field(default="local", min_length=1, max_length=100)


class EventRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    type: Literal["completed", "error", "stuck", "hover", "explain", "why", "ask"]
    code: str = Field(default="", max_length=500_000)
    idle_seconds: Optional[int] = Field(default=None, ge=1, le=3600)
    target: Optional[str] = Field(default=None, max_length=200)
    line: Optional[int] = Field(default=None, ge=1)
    symbol: Optional[str] = Field(default=None, max_length=200)
    question: Optional[str] = Field(default=None, max_length=4_000)


class ModelRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "llm_mode": config.mode, "model": config.model,
            "fast_model": config.fast_model, "providers": config.available_providers(),
            "idle_seconds": config.idle_seconds}


@app.get("/curriculum")
def get_curriculum() -> dict:
    """Versioned public pathway metadata; contains no learner data."""
    return catalog.raw


@app.get("/curricula")
def list_curricula() -> dict:
    return {"curricula": [{"id": c.raw["id"], "version": c.raw["version"],
                            "title": c.raw["title"], "audience": c.raw.get("audience", "")}
                           for c in discover_catalogs()]}


@app.get("/curricula/{catalog_id}")
def get_catalog(catalog_id: str) -> dict:
    try:
        return catalog_by_id(catalog_id).raw
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown curriculum")


@app.get("/curriculum/modules/{module_id}")
def get_curriculum_module(module_id: str) -> dict:
    try:
        return catalog.module(module_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown curriculum module")


@app.get("/models")
def models() -> dict:
    """All models available across configured providers, plus the current defaults."""
    try:
        available = llm.list_models()
    except Exception:
        available = []
    return {"models": available, "default": config.model, "fast": config.fast_model,
            "providers": config.available_providers()}


@app.post("/model")
def set_model(req: ModelRequest) -> dict:
    session = store.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session_id")
    session.model_override = req.model.strip()
    return {"status": "ok", "model": session.model_override or config.model}


@app.post("/session")
def create_session(req: SessionRequest) -> dict:
    session = store.create(req.goal, learner_id=req.learner_id)
    try:
        msg = mentor.blueprint(session)
    except LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))
    profile = learner.profile(session.learner_id)
    return {
        "session_id": session.session_id,
        "blueprint": msg.blueprint,
        "text": msg.text,
        "profile": {
            "mastered": profile.mastered,
            "practiced": profile.practiced,
            "struggling": profile.struggling,
            "recurring_misconceptions": profile.recurring_misconceptions,
        },
    }


@app.get("/suggest-goal")
def suggest_goal(learner_id: str = "local", limit: int = 3) -> dict:
    limit = max(1, min(limit, 10))
    return {"suggestions": mentor.suggest_goals(learner_id, limit),
            "path": mentor.learning_path(learner_id)}


@app.get("/learner/{learner_id}/path")
def get_path(learner_id: str) -> dict:
    return mentor.learning_path(learner_id)


@app.get("/learner/{learner_id}/profile")
def get_profile(learner_id: str) -> dict:
    p = learner.profile(learner_id)
    return {"mastered": p.mastered, "practiced": p.practiced,
            "struggling": p.struggling, "recurring_misconceptions": p.recurring_misconceptions}


@app.post("/learner/{learner_id}/reset")
def reset_profile(learner_id: str) -> dict:
    learner.reset(learner_id)
    return {"status": "reset", "learner_id": learner_id}


@app.post("/event")
def event(req: EventRequest) -> dict:
    session = store.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session_id")

    # Remember the latest real buffer so code-free events (like "ask") have context.
    if req.code:
        session.last_code = req.code
    code = req.code or session.last_code

    try:
        if req.type == "completed":
            msg = mentor.on_completed_line(session, code, target_line=req.line)
        elif req.type == "error":
            msg = mentor.on_error(session, code)
        elif req.type == "stuck":
            msg = mentor.on_stuck(session, code, req.idle_seconds or config.idle_seconds)
        elif req.type == "explain":
            if not req.target:
                raise HTTPException(status_code=400, detail="'explain' requires a target")
            msg = mentor.explain(session, req.target, code)
        elif req.type == "ask":
            if not req.question:
                raise HTTPException(status_code=400, detail="'ask' requires a question")
            msg = mentor.ask(session, req.question, code)
        elif req.type in ("why", "hover"):
            # "Why is this line here?" — needs a line number; falls back to the last line.
            line = req.line or _last_line_no(code)
            msg = mentor.why(session, code, line, req.symbol)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown event type: {req.type}")
    except LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if msg is None:
        return {}
    return msg.to_dict()


def _last_line_no(code: str) -> int:
    lines = code.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            return i + 1
    return 1
