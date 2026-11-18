# Muteki integration checklist

- [x] Canonical graph remains append-only and durable in a per-run SQLite
  workspace.
- [x] Intent payloads survive graph projection rebuilds.
- [x] Tool requests cross the existing GatewayWorker/ToolGateway boundary.
- [x] Runner adapter uses the existing `create_job`/`wait_job` contract.
- [x] Gateway-created Artifact/Observation/EvidenceLedger references are
  preserved in Muteki facts.
- [x] Evidence references are validated through an injected authority when
  available.
- [x] Canonical events are bridged to the existing `run_events`/SSE service.
- [x] Bridge deduplicates canonical sequence numbers and preserves order.
- [x] `solver_mode=muteki` is opt-in; legacy and `solver_v2` defaults remain.
- [x] Canonical flag candidates pass through the existing Muteki Flag Gate.
- [x] Adapter/core regression tests pass.

## Pending live validation

- [ ] Run an actual database-backed `solver_mode=muteki` run against a live
  Runner and target. This requires the configured DB and Runner service to be
  available; unit tests use injected fakes.
- [ ] Verify the frontend consumes generic run events without a format change.
