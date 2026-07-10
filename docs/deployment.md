# Deployment and runner isolation

Deploy Kali Runner on a dedicated Kali VM or a separately managed hardened host. Run it as a non-root service account, expose it only to the FastAPI backend, mount only `data/workspaces`, and set outbound network controls appropriate to the event's challenge network. Do not mount the host root, Docker socket, SSH keys, or unrelated user home directories.

The current `KaliVmExecutionBackend` is a limited runner abstraction, not a container sandbox. `DockerExecutionBackend` is an explicit future placeholder. For production-like use, place the Runner in an isolated network and use a process supervisor with resource limits.
