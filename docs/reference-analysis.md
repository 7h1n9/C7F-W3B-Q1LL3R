# Reference analysis

## CyberStrikeAI (Apache-2.0)

Reviewed repository layout and documentation only. This project adopts the *idea* of declarative YAML Role, Skill, and Tool definitions, role tool allowlists, streamed audit events, and persisting large outputs as artifacts rather than injecting all output into model context. It does not reuse Go code, prompts, security tooling, C2 functionality, or broad scanning/exploitation recipes. The equivalent original files are `configs/roles/`, `configs/skills/`, `configs/tools/`, and the backend event/artifact models.

## DeepAudit (AGPL-3.0)

Reviewed public repository documentation only. This project adopts the architecture-level separation of a FastAPI backend, React frontend, and orchestration responsibilities. It uses original names, schemas, UI components, and implementations. No DeepAudit source code, database model, agent implementation, or frontend component is copied or linked; therefore this repository is not a derivative of its AGPL code.

## OpenAI Codex SDK

The bridge follows the official server-side TypeScript SDK pattern: install `@openai/codex-sdk`, create a `Codex` instance, call `startThread()`, call `thread.run()` to continue, and retain an ID for later continuation. The bridge intentionally has no database access and returns structured events/errors. `CODEX_MOCK_MODE=true` supports development when a live Codex runtime is unavailable.

Sources reviewed on 2026-07-10:

- https://github.com/Ed1s0nZ/CyberStrikeAI
- https://github.com/lintsinghua/DeepAudit
- https://developers.openai.com/codex/sdk
