---
name: muteki-blackboard
description: Shared canonical Muteki graph interface for facts, intents, resources, and PoCs.
---

# Muteki Blackboard Worker Contract

Workers communicate through the shared SQLite graph only. Before a destructive
or exclusive action, run `read-resource-locks`, then `claim-resource`, and
release the resource after the action completes. A lost claim means the worker
must not perform the conflicting action.

Use `write-fact` only for observations from real execution. Use `mark-deadend`
to suppress a route that has been disproven. PoCs are shared artifacts: write
them with `write-poc`, inspect them with `read-pocs`/`read-poc`, and include the
PoC id in an `EXPLOIT_WITH_POC` intent rather than copying hidden state between
workers.
