# Architecture

The browser communicates only with the FastAPI backend through REST and Server-Sent Events (SSE). It never connects directly to MySQL, Kali Runner, an LLM provider, or the Codex SDK.

```text
React + TypeScript
       | REST / SSE
FastAPI backend -- MySQL 8
       |             \
       |              in-process event bus (replaceable with Redis)
       +-- Kali Runner API (restricted tools)
       +-- Node Codex Bridge -- @openai/codex-sdk
```

The backend persists each `run_events` entry before publishing it to the in-process bus. A reconnecting SSE client first reads persistent history, then subscribes to live fan-out.

The Runner accepts only four named tools. It validates target allowlists and workspace boundaries again, even after FastAPI Tool Gateway validation.

For an OpenAI-compatible run, `SolveOrchestrator` owns state transitions and the loop. `ContextBuilder` sends only metadata and bounded summaries to the model. The model returns a strict `AgentAction`; the orchestrator validates the tool against the registry, routes it through Tool Gateway, persists the observation/artifact, extracts flag candidates, and produces `final/writeup.md` after a finish action. Runner job endpoints require `X-Runner-Token` and validate every permitted redirect hop.
