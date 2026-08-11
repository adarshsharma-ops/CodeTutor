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
from .state import Session, SessionStore
from .lesson_progress import (LessonProgress, LessonProgressStore, evaluate_project,
                              fingerprint, infer_project)
from .curriculum_catalog import catalog_by_id, discover_catalogs, load_catalog

config = Config.from_env()
llm = LLMClient(config)
learner = LearnerModel(config.learner_db or None)
mentor = Mentor(llm, learner)
store = SessionStore()
catalog = load_catalog()
progress_store = LessonProgressStore(config.learner_db or learner.db_path)

app = FastAPI(title="CodeTutor Mentor Service", version="0.1.0")

_PROVIDER_ENV_KEYS = (
    "MENTOR_LLM_MODE", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "MENTOR_MODEL", "MENTOR_MODEL_FAST",
    "MENTOR_FAILOVER", "MENTOR_FALLBACK_MODEL", "MENTOR_FALLBACK_FAST",
)


class SessionRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=500)
    learner_id: str = Field(default="local", min_length=1, max_length=100)
    learner_level: Literal["beginner", "intermediate", "advanced"] = "beginner"
    pathway_id: str = Field(default="python-foundations", min_length=1, max_length=100)
    module_id: str = Field(default="", max_length=100)


class EventRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    type: Literal["completed", "error", "stuck", "hover", "explain", "why", "ask", "fix"]
    code: str = Field(default="", max_length=500_000)
    idle_seconds: Optional[int] = Field(default=None, ge=1, le=3600)
    target: Optional[str] = Field(default=None, max_length=200)
    line: Optional[int] = Field(default=None, ge=1)
    symbol: Optional[str] = Field(default=None, max_length=200)
    question: Optional[str] = Field(default=None, max_length=4_000)


class ModelRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)


class LevelRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    learner_level: Literal["beginner", "intermediate", "advanced"]


class CheckRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    code: str = Field(default="", max_length=500_000)
    run_passed: bool = False
    file_uri: str = Field(default="", max_length=2_000)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "llm_mode": config.mode, "model": config.model,
            "fast_model": config.fast_model, "providers": config.available_providers(),
            "idle_seconds": config.idle_seconds, "local_model": config.local_openai,
            "tutoring_quality": "experimental" if config.local_openai else "provider-dependent"}


@app.post("/provider/reload")
def reload_provider() -> dict:
    """Reload provider configuration from the local .env without restarting Uvicorn.

    The endpoint accepts no key or configuration payload. It can only reread the
    mentor-service/.env file already present on this computer.
    """
    import os
    global config, llm, mentor
    for key in _PROVIDER_ENV_KEYS:
        os.environ.pop(key, None)
    config = Config.from_env()
    llm = LLMClient(config)
    mentor = Mentor(llm, learner)
    return health()


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
    try:
        selected_catalog = catalog_by_id(req.pathway_id)
    except KeyError:
        raise HTTPException(status_code=400, detail="Unknown pathway_id")
    module_id = req.module_id or _entry_module(selected_catalog.raw, req.learner_level)
    try:
        selected_catalog.module(module_id)
    except KeyError:
        raise HTTPException(status_code=400, detail="Unknown module_id for pathway")
    session = store.create(req.goal, learner_id=req.learner_id,
                           learner_level=req.learner_level, pathway_id=req.pathway_id,
                           module_id=module_id)
    try:
        msg = mentor.blueprint(session)
    except LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))
    profile = learner.profile(session.learner_id)
    progress_store.save(LessonProgress(
        learner_id=session.learner_id, goal=session.goal,
        learner_level=session.learner_level, blueprint=session.blueprint,
        current_step=session.current_step, pathway_id=session.pathway_id,
        pathway_version=selected_catalog.raw["version"], module_id=session.module_id,
        project_id=infer_project(session.goal)))
    return {
        "session_id": session.session_id,
        "blueprint": msg.blueprint,
        "text": msg.text,
        "learner_level": session.learner_level,
        "pathway_id": session.pathway_id,
        "module_id": session.module_id,
        "profile": {
            "mastered": profile.mastered,
            "practiced": profile.practiced,
            "struggling": profile.struggling,
            "recurring_misconceptions": profile.recurring_misconceptions,
        },
    }


@app.post("/level")
def set_level(req: LevelRequest) -> dict:
    session = store.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session_id")
    session.learner_level = req.learner_level
    session.hint_levels.clear()
    session.current_step = 0
    msg = mentor.blueprint(session)
    _save_session_progress(session)
    return {"status": "ok", "learner_level": session.learner_level,
            "blueprint": msg.blueprint or []}


@app.get("/suggest-goal")
def suggest_goal(learner_id: str = "local", limit: int = 3,
                 pathway_id: str = "python-foundations",
                 learner_level: Literal["beginner", "intermediate", "advanced"] = "beginner") -> dict:
    limit = max(1, min(limit, 10))
    if pathway_id != "python-foundations":
        try:
            selected = catalog_by_id(pathway_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown pathway")
        saved = progress_store.get(learner_id)
        current_id = (saved.module_id if saved and saved.pathway_id == pathway_id
                      else _entry_module(selected.raw, learner_level))
        module = selected.module(current_id)
        rationale = (f"You are entering ‘{selected.raw['title']}’ at ‘{module['title']}’. "
                     f"This project builds the evidence required for that stage.")
        return {
            "suggestions": [{"goal": goal, "rationale": rationale, "module_id": current_id}
                            for goal in module["projects"][:limit]],
            "path": _catalog_path(selected.raw, current_id, _entry_module(selected.raw, learner_level)),
            "pathway_id": pathway_id,
        }
    return {"suggestions": mentor.suggest_goals(learner_id, limit),
            "path": mentor.learning_path(learner_id), "pathway_id": pathway_id}


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
    progress_store.clear(learner_id)
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
        elif req.type == "fix":
            if not req.line:
                raise HTTPException(status_code=400, detail="'fix' requires a line")
            msg = mentor.fix_line(session, code, req.line)
        elif req.type in ("why", "hover"):
            # "Why is this line here?" — needs a line number; falls back to the last line.
            line = req.line or _last_line_no(code)
            msg = mentor.why(session, code, line, req.symbol)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown event type: {req.type}")
    except LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))

    progress = _save_session_progress(session, code=code)
    if msg is None:
        return {}
    response = msg.to_dict()
    response["lesson_progress"] = {
        "current_step": progress.current_step,
        "completed_steps": progress.completed_steps or [],
        "status": progress.status,
    }
    if code and req.type in {"completed", "stuck", "ask"}:
        readiness = evaluate_project(session.goal, code, run_passed=False)
        response["lesson_readiness"] = readiness
        failed_checks = [check for check in readiness["checks"] if not check["passed"]]
        failed = {check["id"] for check in failed_checks}
        completion_claim = any(
            phrase in response.get("text", "").lower()
            for phrase in ("looks complete", "whole pipeline", "program is complete",
                           "you've completed", "you have completed", "finished the program")
        )
        if completion_claim and failed:
            structural = [check for check in failed_checks if check["id"] != "run"]
            response["kind"] = "next_step"
            response["via"] = "verified lesson check"
            if "fallback" in failed:
                response["headline"] = "Add a result for unmatched input"
                response["text"] = (
                    "You are close, but a deterministic lesson check still fails: the function "
                    "can reach its end and return `None` when nothing matches. Add an explicit "
                    "fallback return after its matching logic, then test one matching and one "
                    "non-matching input."
                )
            elif "return_paths" in failed:
                detail = next((check.get("detail", "") for check in structural
                               if check["id"] == "return_paths"), "")
                response["headline"] = "Cover every return path"
                response["text"] = (
                    "You are close, but the program is not complete yet. A function whose result "
                    f"is used can still return `None`. {detail}. Add an explicit result for its "
                    "unhandled path, then test both the usual case and the edge case."
                )
            elif structural:
                response["headline"] = "One lesson requirement remains"
                response["text"] = (
                    "The code may be ready for another step, but it is not complete yet. The "
                    f"lesson check still needs: {structural[0]['label']}. Address that item, then "
                    "run the lesson check again."
                )
            else:
                response["headline"] = "Ready to test—not complete yet"
                response["text"] = (
                    "The structure looks ready to test, but CodeTutor will not mark it complete "
                    "until it runs successfully. Try a normal input and an edge case, then use "
                    "Run lesson check."
                )
    return response


@app.get("/learner/{learner_id}/resume")
def resume_lesson(learner_id: str) -> dict:
    progress = progress_store.get(learner_id)
    if progress is None:
        return {}
    session = Session(goal=progress.goal, learner_level=progress.learner_level,
                      learner_id=progress.learner_id, blueprint=progress.blueprint,
                      current_step=progress.current_step, last_code="",
                      pathway_id=progress.pathway_id, module_id=progress.module_id)
    store.restore(session)
    return {"session_id": session.session_id, **progress.to_dict(),
            "next_step": progress.blueprint[min(progress.current_step, len(progress.blueprint) - 1)]
                         if progress.blueprint else "",
            "next": _next_recommendation(progress.goal, progress.pathway_id)}


@app.post("/lesson/check")
def check_lesson(req: CheckRequest) -> dict:
    session = store.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session_id")
    result = evaluate_project(session.goal, req.code or session.last_code, req.run_passed)
    session.last_code = req.code or session.last_code
    if result["passed"]:
        session.current_step = len(session.blueprint)
    progress = _save_session_progress(session, code=session.last_code,
                                      checks=result["checks"],
                                      status="completed" if result["passed"] else "in_progress",
                                      file_uri=req.file_uri)
    return {**result, "progress": progress.to_dict(),
            "next": _next_recommendation(session.goal, session.pathway_id)}


def _save_session_progress(session: Session, code: str = "", checks: list[dict] | None = None,
                           status: str | None = None, file_uri: str = "") -> LessonProgress:
    previous = progress_store.get(session.learner_id)
    value = LessonProgress(
        learner_id=session.learner_id, goal=session.goal,
        learner_level=session.learner_level, blueprint=session.blueprint,
        current_step=session.current_step,
        completed_steps=list(range(min(session.current_step, len(session.blueprint)))),
        status=status or (previous.status if previous else "in_progress"),
        pathway_id=session.pathway_id,
        pathway_version=catalog_by_id(session.pathway_id).raw["version"],
        module_id=session.module_id,
        project_id=infer_project(session.goal),
        file_uri=file_uri or (previous.file_uri if previous else ""),
        code_fingerprint=fingerprint(code) or (previous.code_fingerprint if previous else ""),
        checks=checks if checks is not None else (previous.checks if previous else []))
    return progress_store.save(value)


def _next_recommendation(goal: str, pathway_id: str = "python-foundations") -> dict | None:
    selected = catalog_by_id(pathway_id)
    modules = selected.modules
    for module_index, module in enumerate(modules):
        projects = module.get("projects", [])
        for project_index, project in enumerate(projects):
            if project.lower() in goal.lower() or goal.lower() in project.lower():
                if project_index + 1 < len(projects):
                    return {"module_id": module["id"], "module_title": module["title"],
                            "goal": projects[project_index + 1]}
                if module_index + 1 < len(modules):
                    nxt = modules[module_index + 1]
                    return {"module_id": nxt["id"], "module_title": nxt["title"],
                            "goal": nxt["projects"][0]}
                return None
    first = modules[0]
    return {"module_id": first["id"], "module_title": first["title"],
            "goal": first["projects"][0]}


def _entry_module(raw: dict, learner_level: str) -> str:
    entries = raw.get("entry_modules", {})
    requested = entries.get(learner_level)
    if requested:
        return requested
    return raw["modules"][0]["id"]


def _catalog_path(raw: dict, current_id: str, entry_id: str = "") -> dict:
    levels = []
    current_index = next((i for i, m in enumerate(raw["modules"]) if m["id"] == current_id), 0)
    entry_index = next((i for i, m in enumerate(raw["modules"]) if m["id"] == entry_id), 0)
    for index, module in enumerate(raw["modules"]):
        skipped_for_placement = index < entry_index
        completed = entry_index <= index < current_index
        levels.append({
            "key": module["id"], "title": module["title"],
            "mastered": len(module["projects"]) if completed else 0,
            "total": len(module["projects"]), "done": completed,
            "skipped": skipped_for_placement,
            "blurb": module["mental_model"],
        })
    return {"current_level": current_id, "levels": levels, "title": raw["title"]}


def _last_line_no(code: str) -> int:
    lines = code.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            return i + 1
    return 1
