#!/usr/bin/env bash
# Start the mentor HTTP service. The VS Code extension talks to this.
set -euo pipefail
cd "$(dirname "$0")"

# Load .env if present.
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

PYTHON=python3
if [ -x .venv/bin/python ]; then
  PYTHON=.venv/bin/python
fi

if ! "$PYTHON" -c "import uvicorn" >/dev/null 2>&1; then
  cat >&2 <<'EOF'
CodeTutor's Python dependencies are not installed in the active environment.

Run:
  python3 -m venv .venv
  source .venv/bin/activate
  python -m pip install -r requirements.txt
  bash run_server.sh
EOF
  exit 1
fi

exec "$PYTHON" -m uvicorn mentor.server:app --host 127.0.0.1 --port 8756 --reload
