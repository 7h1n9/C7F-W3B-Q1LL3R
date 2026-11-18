#!/usr/bin/env bash
# Ephemeral launcher used only by the agent session: keeps uvicorn attached to a
# managed background task so the sandbox does not reap it when a shell exits.
# Not part of the project's supported startup path (that is scripts/start-all.ps1).
set -euo pipefail
cd "$(dirname "$0")/../backend"

export APP_DATABASE_URL="mysql+asyncmy://ctf_agent:ctf_agent@127.0.0.1:3307/ctf_agent"
export APP_WORKSPACE_ROOT="../data/workspaces"
export APP_RUNNER_URL="http://192.168.236.128:8091"
export APP_RUNNER_API_TOKEN="development-runner-token"
export APP_CORS_ORIGINS="http://localhost:5173,http://127.0.0.1:5173"
export APP_ENCRYPTION_KEY="development-only-change-me"
export APP_CTFCTL_INTERNAL_ACCESS_KEY="development-ctfctl-access-key"
export APP_ALLOWED_SERVICE_CIDRS="127.0.0.0/8,192.168.56.0/24,192.168.236.0/24"
export APP_ENVIRONMENT="development"
export CTFCTL_BACKEND_URL="http://127.0.0.1:${BACKEND_PORT_OVERRIDE:-8001}"
export BACKEND_HOST="127.0.0.1"
export BACKEND_PORT="${BACKEND_PORT_OVERRIDE:-8001}"
export MUTEKI_CONTROL_BIND="0.0.0.0"
export MUTEKI_CONTROL_PORT="9100"
export MUTEKI_WORKER_IMAGE="muteki-worker:codex-0.147.0"

exec ../.venv3.11/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port "${BACKEND_PORT_OVERRIDE:-8001}"
