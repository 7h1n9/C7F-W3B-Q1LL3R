# Phase 2.4 Validation Plan

Status: COMPLETED_WITH_LIMITS

## Code validation

- Focused Solver and Runtime regression tests.
- Phase 2.4 integration tests under `backend/tests/phase24/`.
- Compile, lint, and diff checks.
- Verify both the legacy path and the explicit `solver_v2` path remain
  isolated and functional.
- Assert central RunEvent coverage for run, action, tool, observation, and
  completion transitions.
- Assert that a completion-looking fact without a valid EvidenceLedger row
  cannot produce `COMPLETED_SOLVED`.

## Real target validation

Target: `http://192.168.236.1:28346/`

The initial task supplied to the system must contain only the target name,
target URL, and the request to perform authorized security testing and return
the evidence-backed result. No manually supplied vulnerability location or
answer is allowed.

Required artifacts per run:

- Run ID
- Recon report
- Action trace
- Evidence references
- Completion decision
- Final answer
- Failure/recovery information

The genuine run gate was satisfied by `4b9236c2-1b27-4afc-8b0b-9a7b522eb5b2`; fresh reproduction was independently verified on `244d0056-636d-4b5f-92e7-beb432ca757a` and `da0da023-8a0d-41fb-9bb9-6b8d2f76b5ac`.

## Current audit gate

The explicit `solver_v2` route is now eligible and measured. The sample contains 13 real runs: 11 `COMPLETED_SOLVED` and 2 controlled `COMPLETED_UNSOLVED/SOLVER_NO_ACTION` outcomes during eight-way concurrent load. Both unsolved runs lacked a verified Finding and did not cross the Completion Gate; subsequent sequential and final post-validation runs succeeded with fresh reproduction.
