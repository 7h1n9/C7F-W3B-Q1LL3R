# Deployment and execution isolation

## Tool execution: container-native (default)

Tools run inside **container-native Muteki Workers**. The Worker container is the
execution boundary: it gets only the workspace and blackboard bind mounts, and
the run-scoped token handshake authorizes it. Nothing needs to be deployed beside
the backend for a solve to execute.

`Dockerfile.worker` builds that image; `MUTEKI_WORKER_IMAGE` selects it (the
default is `ghcr.io/fishcodetech/muteki-worker:latest`, which the official
Worker/Sandbox path uses).

Because of this, the **legacy Kali Runner is retired as an execution path**:

- readiness does not probe it, so an absent Runner cannot render the console
  `NOT_READY`;
- the per-Attempt tool manifest is not intersected with a Runner's advertised
  capabilities, so a missing Runner cannot empty a Run's tool set and pause it
  with `TOOL_CATALOG_DRIFT`;
- `tool_permissions` no longer re-applies that intersection on the request path.

## Legacy Kali Runner (opt-in)

A deployment that still fronts a standalone Runner can turn the old behaviour
back on:

```
APP_LEGACY_KALI_RUNNER=1
APP_RUNNER_URL=http://<host>:8091
APP_RUNNER_API_TOKEN=<token>
```

With the flag on, everything behaves as it did before: readiness emits the five
`runner*` checks and gates on them, `tool_manifest` intersects with the Runner's
capabilities and detects drift, and `tool_permissions` applies the same filter.

The two paths that *require* a Runner and cannot fall back are the legacy
`single_agent` / `multi_agent_v1` solver modes and the fresh-reproduction replay.
With the flag off, those calls now fail fast with `RUNNER_LEGACY_DISABLED` — a
configuration error naming the way out — instead of an HTTP timeout reported as
"Runner unavailable", which reads as a transient outage and invites a retry that
cannot help.

### Hardening, if you do run one

Deploy the Runner on a dedicated host or VM. Run it as a non-root service
account, expose it only to the FastAPI backend, mount only `data/workspaces`, and
apply outbound network controls appropriate to the challenge network. Do not
mount the host root, the Docker socket, SSH keys, or unrelated home directories.
Put it in an isolated network behind a process supervisor with resource limits;
the `KaliVmExecutionBackend` is a limited runner abstraction, not a container
sandbox.