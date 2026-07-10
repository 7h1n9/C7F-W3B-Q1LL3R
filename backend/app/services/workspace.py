import json
from pathlib import Path

from app.core.config import get_settings
from app.core.exceptions import DomainError
from app.models.challenge import Challenge

RUN_POLICY = """# Run Policy\n\nOnly interact with the challenge target hosts recorded in challenge.json. Do not use arbitrary shell commands, public targets, persistence, or paths outside this workspace. Record evidence for material conclusions.\n"""


def create_workspace(run_id: str, challenge: Challenge) -> Path:
    root = get_settings().workspace_root.resolve()
    workspace = (root / run_id).resolve()
    if workspace.parent != root:
        raise DomainError("WORKSPACE_INVALID", "Invalid run workspace path.")
    for child in ("source", "attachments", "requests", "responses", "scripts", "evidence", "outputs", "final"):
        (workspace / child).mkdir(parents=True, exist_ok=True)
    (workspace / "challenge.json").write_text(json.dumps({"id": challenge.id, "name": challenge.name, "target_url": challenge.target_url, "allowed_hosts": challenge.allowed_hosts, "flag_pattern": challenge.flag_pattern}, indent=2), encoding="utf-8")
    (workspace / "AGENTS.md").write_text(RUN_POLICY, encoding="utf-8")
    return workspace
