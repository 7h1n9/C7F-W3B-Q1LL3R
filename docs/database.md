# Database

MySQL 8 uses `utf8mb4` and stores JSON fields as MySQL JSON. The initial Alembic migration creates `challenges`, `solve_runs`, `run_events`, `tool_calls`, `artifacts`, `observations`, `hypotheses`, `flag_candidates`, and `model_configs`.

Run IDs and other primary keys are UUID strings. Timestamps are generated in UTC and serialized as ISO 8601. API keys are represented by `encrypted_api_key`; the first release does not expose the field to the frontend.

Migration `0002_agent_loop` adds persisted agent/tool/runtime limits, counters, durable event sequencing, and error-detail fields. It also adds run-local query indexes for events and tool calls.
