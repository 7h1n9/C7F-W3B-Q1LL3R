# Multi-agent core loop (phase 1)

The multi-agent path is opt-in per Run:

```text
solver_mode=single_agent    # default; existing orchestrator path
solver_mode=multi_agent_v1  # structured controller path
```

`single_agent` remains unchanged and is covered by the existing regression
suite.  The multi-agent API is under `/api/v1/multi-agent` and only accepts
runs explicitly created with `multi_agent_v1`.

## Ownership and state flow

Agents return a `PlannerProposal` or `AgentTaskResult`. They do not write Run
status, invoke the Runner directly, submit a flag, or bypass a tool budget.
`DeterministicController` owns the following transition:

```text
Verified Memory
  -> Planner Proposal
  -> Analysis Review
  -> Controller validation
  -> AgentTask lease
  -> AgentTaskResult
  -> Result Normalizer / Promotion Gate
  -> Memory Snapshot + Evidence Ledger
  -> replan or next stage

INTAKE -> RECON -> ANALYSIS -> EXPLOITATION -> VERIFICATION -> REPORTING
   ^          |         |             |               |             |
   +----------+---------+-------------+---------------+-------------+
                         failure / NEED_REPLAN
```

The existing guarded Run state machine is used underneath (`PREPARING`,
`ANALYZING`, `PLANNING`, `EXECUTING`, `VERIFYING_FLAG`, `REPORTING`). Only the
controller can call the transition function for the multi-agent path.

## Role permission matrix

| Role | Reads | Tools | Structured outputs | Forbidden |
| --- | --- | --- | --- | --- |
| PLANNER | working, verified facts, hypotheses, failures | none | proposal | Runner, flag, Run status |
| RECON | working, facts, evidence | bounded recon tools | candidate recon facts, evidence | exploitation, flag, Run status |
| ANALYSIS | working, facts, hypotheses, evidence, failures | oracle/compare/evidence query | review, hypothesis, evidence | Runner, flag, Run status |
| EXPLOIT | working, facts, hypotheses, evidence | only approved task tools | artifact, evidence, candidate | replan, budget bypass, Run status |
| VERIFY | working, facts, evidence | isolated verification tools | verification result, fresh reproduction | exploitation, Run status |

Policies are persisted in `agent_role_policies` and checked before task
creation. A task also carries a bounded logical-call/internal-request/runtime
budget, lease, optimistic version, input snapshot version, timeout, cancel
flag, and retry count.

## Durable data model

Migration `0024_multi_agent_core` adds:

- `agent_tasks`, `agent_task_results`, `agent_role_policies`
- `planner_proposals`, `analysis_reviews`
- `verified_facts`, `evidence_ledger`
- `solution_chain_nodes`, `failure_signatures`, `memory_snapshots`

Long-term records are compact summaries. Raw agent conversation history is not
used as the Memory Center source of truth. Protected evidence must retain the
chain `Fact -> Evidence -> Artifact -> ToolCall -> AgentTask -> Run`.

## Promotion and terminal gate

The Promotion Gate returns `VERIFIED`, `CANDIDATE`, `DUPLICATE`,
`SUPERSEDED`, or `NO_VALUE`. Output with no new evidence, or repeated facts,
does not grow long-term memory. A Solution Chain node is accepted only when
its task is completed and its result facts, capability, and evidence all
exist in the same Run.

The controller promotes a flag only when the candidate is non-empty, pattern
matched, linked to a real source Artifact and producing ToolCall, linked to an
isolated Verify task, and fresh reproduction succeeds. Only then may it move
the Run through `REPORTING` to `COMPLETED_SOLVED`.

## Phase 2 data governance and Web Research

Migration `0025_data_governance_web_research` adds the bounded runtime model:

```text
workspace/
  runtime/{agents,web-research,tool-subrequests,streams,runner-jobs,
           pending-promotion,cleanup-manifests}/
  evidence/       # protected, never a janitor target
  outputs/        # formal aggregates and final outputs
  final/          # fresh reproduction and report outputs
  archive/temporary/*.jsonl.gz
```

`TemporaryDataJanitor` only operates below `workspace/runtime`. Task cleanup
is delayed by TTL, failed/debug data is archived with a JSONL.GZ manifest,
SHA-256 and row count, and terminal cleanup is protected by a database
idempotency key. Active tasks, live leases, protected runtime prefixes, and
formal evidence are preserved. Terminal cleanup also writes an
`EvidenceSnapshot` and records the manifest ID on the Run.

`ToolScheduler` fingerprints normalized tool, arguments, target, stage and
evidence version. `ToolSubrequestAggregator` stores 200+ internal requests as
compressed JSONL and emits one durable aggregate artifact plus one
`ToolBatchSummary`; repeated logical requests are rejected as duplicates.

`WebResearchService` supports Codex and OpenAI-compatible adapters. Queries
are classified as LOW/MEDIUM/HIGH/BLOCKED. Research is ephemeral by default;
only Analysis can promote a safe summary into verified facts. The answer-leak
guard removes flag-like answers, challenge-specific content and local-source
paths before any result can enter durable evidence.

Assistance is recorded as `AUTONOMOUS`, `HINT_GUIDED`, `EVIDENCE_GUIDED`, or
`ANSWER_GUIDED`. Every flag candidate has a `FlagProvenance` row preserving
the first-seen source and the fresh-reproduction verification source.

The comparison endpoint is:

```text
GET /api/v1/multi-agent/compare?single_run_id=...&multi_run_id=...
```

The database-backed asset-warranty acceptance endpoint is:

```text
GET /api/v1/multi-agent/runs/{run_id}/acceptance
```

It returns `ASSET_WARRANTY_MULTI_AGENT_SOLVE=NOT_READY` until a real
`codex_sdk` run provides all required Planner/Analysis/Exploit/Verify,
solution-chain, batch-tool, autonomous provenance, and fresh-reproduction
evidence. It never reads the challenge-source directory or fabricates a solve.
