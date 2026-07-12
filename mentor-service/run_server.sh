#!/usr/bin/env bash
# Start the mentor HTTP service. The VS Code extension talks to this.
set -euo pipefail
cd "$(dirname "$0")"

# Load .env if present.
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

exec uvicorn mentor.server:app --host 127.0.0.1 --port 8756 --reload
