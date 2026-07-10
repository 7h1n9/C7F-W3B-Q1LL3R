# CTF Web Agent (initial foundation)

An authorization-bound monorepo for a CTF Web solving workflow. It is designed only for local practice ranges, CTF competitions, and explicitly authorized testing. It does **not** enable arbitrary shell commands, public-target automation, persistence, automatic exploitation, broad scanning, or automated payload libraries.

## Components

- `backend/`: FastAPI, SQLAlchemy 2 async data access, Alembic, state machine, Tool Gateway, SSE persistence.
- `frontend/`: Vite + React + TypeScript + Ant Design pages for dashboard, challenges, runs, workspace, and settings.
- `codex-bridge/`: server-only Fastify bridge to `@openai/codex-sdk`, with a deterministic mock mode.
- `kali-runner/`: separate FastAPI service for restricted HTTP, file-read, file-search, and existing Python-script execution.
- `configs/`: original YAML definitions for one safe web-CTF role, four tools, and one skill.

## Requirements

- Python 3.11+
- Node.js 20+ (the Codex SDK itself supports Node 18+, but this project targets Node 20+)
- MySQL 8 with `utf8mb4` (Docker Compose is supplied)
- A separate Kali Linux VM/service for Runner in real environments

## Start from a clean environment

```bash
copy backend\.env.example backend\.env
copy kali-runner\.env.example kali-runner\.env
copy codex-bridge\.env.example codex-bridge\.env
copy frontend\.env.example frontend\.env
docker compose up -d mysql
cd backend && pip install -e ".[dev]" && alembic upgrade head
cd ..\kali-runner && pip install -e ".[dev]"
cd ..\frontend && npm install
cd ..\codex-bridge && npm install
```

Start each service in a separate terminal:

```bash
cd backend && uvicorn app.main:app --reload --port 8000
cd kali-runner && uvicorn app.main:app --port 8091
cd codex-bridge && set CODEX_MOCK_MODE=true && npm run dev
cd frontend && npm run dev
```

Open the Vite URL (normally `http://localhost:5173`). The frontend calls FastAPI only.

## Migration lifecycle

```bash
cd backend
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

`APP_DATABASE_URL` defaults to `mysql+asyncmy`. For local tests it can point to an async SQLite URL; production use remains MySQL 8.

## Mock modes

- `engine_type=mock` emits a harmless analysis/plan/report event sequence and completes as unsolved.
- `CODEX_MOCK_MODE=true` provides local thread IDs and structured mock events without a live Codex runtime.
- Runner remains real and restricted; no generic command execution endpoint exists.

## Implemented

- Challenge CRUD with `target_url` host / `allowed_hosts` validation.
- Solve-run creation with a unique workspace, `challenge.json`, run `AGENTS.md`, and a durable `run.created` event.
- Explicit state machine; controllers cannot assign arbitrary statuses.
- Durable sequenced events and SSE replay/live fan-out with heartbeats.
- MySQL/Alembic schema, OpenAI-compatible engine skeleton, Codex SDK bridge, YAML loading, and tool-audit models.
- Workspace isolation, path traversal checks, target allowlisting, subprocess argument vectors, timeouts, and output caps.

## Deliberately not implemented

Automatic SQL injection, command execution, file upload, exploit payload libraries, broad scanners, multi-agent autonomy, RAG, Docker sandboxing, auth, WebSockets, `codex app-server`, and a distributed queue. Those remain future design items, not hidden capabilities.

## Verification

```bash
cd backend && ruff check . && pytest
cd kali-runner && ruff check . && pytest
cd frontend && npm run build
cd codex-bridge && npm run build
```

See `docs/architecture.md`, `docs/api.md`, `docs/database.md`, `docs/deployment.md`, and `docs/reference-analysis.md` for further details.
