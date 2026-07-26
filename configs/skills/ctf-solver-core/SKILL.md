---
name: ctf-solver-core
display_name: CTF Solver Core
description: Core methodology for single-agent authorized CTF solving.
skill_kind: CORE
activation_mode: ALWAYS
challenge_types: [WEB_TARGET, TRAFFIC_ANALYSIS]
required_tools: [file_read, file_search, http_request, python_run, pcap_metadata, pcap_protocols, pcap_query]
recommended_tools: [file_read, file_search, http_request, python_run, pcap_metadata, pcap_protocols, pcap_query]
ctf_phases: [INFRASTRUCTURE_VALIDATION, DISCOVERY, CONFIRMATION, EXPLOITATION_PLANNING, AUTOMATED_EXPLOITATION, EXTRACTION, VERIFICATION, REPORTING]
---

# CTF Solver Core

Use this skill for every authorized single-agent CTF run.

## Methodology 4.3 stages

Always validate the Backend, MCP bridge, Runner capabilities, workspace and
target allowlist before target work. Discovery and Confirmation are bounded
by three manual probes. Once a Boolean differential is confirmed, the fixed
chain is `sql_boolean_compare -> request_capture -> sqlmap_detect ->
sqlmap_run -> generated script`; ordinary HTTP extraction is not an allowed
shortcut. Extraction stops at the smallest evidence set that proves the
challenge result, then Verification and Reporting preserve request, result,
provenance and artifact hashes.

## Method

1. Establish the challenge type and the current phase.
2. Confirm a baseline before chasing a hypothesis.
3. Keep each tool call tied to one hypothesis and one expected evidence item.
4. Record what changed, what was rejected, and what still needs proof.
5. Refuse to repeat identical actions without a new reason.
6. Finish only after the finish gate is satisfied.

## When blocked

- If a hypothesis does not produce new evidence, stop repeating it and record the blocker.
- Prefer a phase change or a different evidence source over “one more try”.
- If the obstacle is missing context, return to baseline and map what is still unknown.
- If the obstacle requires user-supplied information, pause and ask for that information instead of guessing.
- Before invoking a specialized tool, confirm its input contract; if a tool rejects the call shape, treat that as a tooling blocker rather than evidence about the target.
- If a tool repeatedly fails with the same contract or execution error, stop issuing the same call shape and switch to another evidence source or workflow.
