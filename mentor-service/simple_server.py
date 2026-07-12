#!/usr/bin/env python3
"""Zero-dependency server — same API as the FastAPI service, stdlib only.

Use this to test the extension locally when you can't/don't want to pip install
FastAPI (e.g. behind a locked-down proxy). It serves the exact endpoints the VS Code
extension calls, using only Python's standard library.

    python3 simple_server.py            # http://127.0.0.1:8756  (offline mock mode)
    OPENAI_API_KEY=... python3 simple_server.py   # with a real LLM

For production, prefer the FastAPI app (mentor/server.py). This is a convenience runner.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mentor.config import Config
from mentor.llm import LLMClient, LLMError
from mentor.learner_model import LearnerModel
from mentor.mentor import Mentor
from mentor.state import SessionStore

CONFIG = Config.from_env()
LLM = LLMClient(CONFIG)
LEARNER = LearnerModel(CONFIG.learner_db or None)
MENTOR = Mentor(LLM, LEARNER)
STORE = SessionStore()


def _last_line_no(code: str) -> int:
    lines = code.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            return i + 1
    return 1


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def log_message(self, fmt, *args):  # quieter console
        return

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, {"status": "ok", "llm_mode": CONFIG.mode,
                             "model": CONFIG.model, "providers": CONFIG.available_providers(),
                             "server": "stdlib"})
        elif parsed.path == "/models":
            try:
                available = LLM.list_models()
            except Exception:
                available = []
            self._send(200, {"models": available, "default": CONFIG.model,
                             "fast": CONFIG.fast_model, "providers": CONFIG.available_providers()})
        elif parsed.path == "/suggest-goal":
            q = parse_qs(parsed.query)
            lid = (q.get("learner_id") or ["local"])[0]
            self._send(200, {"suggestions": MENTOR.suggest_goals(lid),
                             "path": MENTOR.learning_path(lid)})
        elif parsed.path.startswith("/learner/") and parsed.path.endswith("/profile"):
            lid = parsed.path[len("/learner/"):-len("/profile")]
            p = LEARNER.profile(lid)
            self._send(200, {"mastered": p.mastered, "practiced": p.practiced,
                             "struggling": p.struggling,
                             "recurring_misconceptions": p.recurring_misconceptions})
        else:
            self._send(404, {"detail": "not found"})

    def do_POST(self):
        try:
            data = self._read_json()
            if self.path == "/session":
                session = STORE.create(data.get("goal", ""),
                                       learner_id=data.get("learner_id", "local"))
                msg = MENTOR.blueprint(session)
                self._send(200, {"session_id": session.session_id,
                                 "blueprint": msg.blueprint, "text": msg.text})
                return

            if self.path.startswith("/learner/") and self.path.endswith("/reset"):
                lid = self.path[len("/learner/"):-len("/reset")]
                LEARNER.reset(lid)
                self._send(200, {"status": "reset", "learner_id": lid})
                return

            if self.path == "/model":
                session = STORE.get(data.get("session_id", ""))
                if session is None:
                    self._send(404, {"detail": "Unknown session_id"})
                    return
                session.model_override = (data.get("model") or "").strip()
                self._send(200, {"status": "ok",
                                 "model": session.model_override or CONFIG.model})
                return

            if self.path == "/event":
                session = STORE.get(data.get("session_id", ""))
                if session is None:
                    self._send(404, {"detail": "Unknown session_id"})
                    return
                t = data.get("type")
                # Remember the latest real buffer so code-free events ("ask") have context.
                if data.get("code"):
                    session.last_code = data["code"]
                code = data.get("code", "") or session.last_code
                if t == "completed":
                    msg = MENTOR.on_completed_line(session, code)
                elif t == "error":
                    msg = MENTOR.on_error(session, code)
                elif t == "stuck":
                    msg = MENTOR.on_stuck(session, code, data.get("idle_seconds") or CONFIG.idle_seconds)
                elif t == "explain":
                    msg = MENTOR.explain(session, data.get("target", ""), code)
                elif t == "ask":
                    msg = MENTOR.ask(session, data.get("question", ""), code)
                elif t in ("why", "hover"):
                    msg = MENTOR.why(session, code, data.get("line") or _last_line_no(code),
                                     data.get("symbol"))
                else:
                    self._send(400, {"detail": f"Unknown event type: {t}"})
                    return
                self._send(200, msg.to_dict() if msg else {})
                return

            self._send(404, {"detail": "not found"})
        except LLMError as e:
            self._send(502, {"detail": str(e)})
        except Exception as e:  # keep the dev server alive on bad input
            self._send(500, {"detail": f"{type(e).__name__}: {e}"})


def main() -> None:
    addr = ("127.0.0.1", 8756)
    print(f"CodeTutor (stdlib server) on http://{addr[0]}:{addr[1]}  •  LLM mode: {CONFIG.mode}")
    print("Leave this running; press Ctrl+C to stop.")
    ThreadingHTTPServer(addr, Handler).serve_forever()


if __name__ == "__main__":
    main()
