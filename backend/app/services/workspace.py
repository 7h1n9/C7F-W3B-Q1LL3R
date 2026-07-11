import json
import shutil
from pathlib import Path

from app.core.config import get_settings
from app.core.exceptions import DomainError
from app.models.challenge import Challenge

RUN_POLICY = """# Run Policy\n\nOnly interact with the challenge target hosts recorded in challenge.json. Do not use arbitrary shell commands, public targets, persistence, or paths outside this workspace. Record evidence for material conclusions.\n"""


def create_workspace(
    run_id: str, challenge: Challenge, attachments: list[object] | None = None
) -> Path:
    root = get_settings().workspace_root.resolve()
    workspace = (root / run_id).resolve()
    if workspace.parent != root:
        raise DomainError("WORKSPACE_INVALID", "Invalid run workspace path.")
    for child in (
        "source",
        "attachments",
        "requests",
        "responses",
        "scripts",
        "evidence",
        "outputs",
        "final",
    ):
        (workspace / child).mkdir(parents=True, exist_ok=True)
    manifest = []
    repository_root = Path(__file__).resolve().parents[3]
    attachment_root = (repository_root / "data" / "challenges" / challenge.id / "attachments").resolve()
    for item in attachments or []:
        raw_source = repository_root / item.relative_path
        source = raw_source.resolve()
        target = workspace / "attachments" / item.stored_name
        if source.is_file() and source.parent == attachment_root and not raw_source.is_symlink():
            shutil.copyfile(source, target)
            manifest.append(
                {
                    "path": f"attachments/{item.stored_name}",
                    "sha256": item.sha256,
                    "size": item.size,
                    "primary": item.is_primary,
                }
            )
    primary = next((entry["path"] for entry in manifest if entry["primary"]), None)
    payload = {
        "id": challenge.id,
        "name": challenge.name,
        "challenge_type": challenge.challenge_type,
        "target_url": challenge.target_url,
        "allowed_hosts": challenge.allowed_hosts,
        "flag_pattern": challenge.flag_pattern,
        "primary_attachment": primary,
        "attachments": manifest,
        "metadata": challenge.metadata_json,
    }
    (workspace / "challenge.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (workspace / "AGENTS.md").write_text(RUN_POLICY, encoding="utf-8")
    return workspace
