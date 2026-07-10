# API

All successful FastAPI responses use `{ "data": ... }`. Errors use `{ "code", "message", "details" }`.

| Endpoint | Purpose |
| --- | --- |
| `GET/POST /api/v1/challenges` | List or create challenges |
| `GET/PUT/DELETE /api/v1/challenges/{id}` | Challenge detail and lifecycle |
| `POST /api/v1/challenges/{id}/runs` | Create an isolated solve run |
| `GET /api/v1/runs`, `GET /api/v1/runs/{id}` | Read runs |
| `POST /api/v1/runs/{id}/start`, `/cancel` | State-machine mediated run control |
| `GET /api/v1/runs/{id}/events` | Persistent SSE stream |
| `POST /api/v1/runs/{id}/tools` | Tool Gateway entry point |

Runner exposes `/health`, `/api/v1/jobs`, `/api/v1/jobs/{id}`, `/cancel`, and `/events`. Codex Bridge exposes `/health`, `/threads`, `/threads/{id}/run`, `/resume`, and `/cancel`.
