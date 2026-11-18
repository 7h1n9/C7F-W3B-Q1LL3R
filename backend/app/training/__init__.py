"""Self-evolution training data: turning run history into transitions.

This package is the v0.1 of the strategy-layer self-evolution loop.  It does
NOT train weights.  It distils the project's own run history into structured
(state, action, observation, label) transitions that later stages can:

* replay and audit (every line carries provenance back to a run id),
* split by challenge / CVE family so evaluation never leaks the answer,
* feed candidate-vs-baseline strategy A/B through the existing
  ``ExperimentRunner`` harness.

Hard rules carried over from the existing design (see
``scripts/build_learned_playbook.py`` and ``docs/archive/2026-09-10/solver-capability-todo.md``):

* Nothing is invented.  Every transition is quoted from a recorded event.
* A run that never produced a command, died on infrastructure, or was
  cancelled is excluded with an explicit reason -- never silently dropped,
  never silently kept.
* Flags, secrets, URLs, hosts and paths are redacted before anything leaves
  the database host, reusing ``SensitiveValueSanitizer`` so the redaction
  rules stay in exactly one place.
"""
