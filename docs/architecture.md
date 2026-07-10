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
