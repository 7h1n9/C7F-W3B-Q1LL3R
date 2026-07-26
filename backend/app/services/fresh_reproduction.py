"""Execute the minimal solution path in a clean Runner session."""

import hashlib
import json
import re
import shutil
from pathlib import Path

from sqlalchemy import select

from app.models.run import Artifact, FlagCandidate
from app.services.reproduction_commands import reproduction_command_renderer
from app.services.runner_client import runner_client


class FreshReproductionExecutor:
    async def execute(self, session, run, challenge, steps: list | None = None) -> dict:
        root = Path(run.workspace_path).resolve()
        final = root / "final"
        final.mkdir(parents=True, exist_ok=True)
        await runner_client.clear_sessions(run.id)
        await runner_client.sync_workspace(run.id, root)
        selected = steps or []
        log: list[dict] = []
        commands: list[str] = []
        success = True
        flag_artifact = None
        fresh_flag_value = None
        for step in selected:
            tool = getattr(step, "tool_name", None) or step.get("tool_name")
            args = getattr(step, "normalized_arguments", None) or step.get("normalized_arguments") or {}
            command = reproduction_command_renderer.render(tool, args)
            commands.append(command)
            job_id = await runner_client.create_job(run.id, list(challenge.allowed_hosts or []), tool, args)
            result = await runner_client.wait_job(job_id, max_wait_seconds=600)
            log.append({"tool": tool, "command": command, "job_id": job_id, "status": result.get("status"), "result": result})
            result_text = json.dumps(result, ensure_ascii=False)
            match = re.search(str(challenge.flag_pattern), result_text)
            if match:
                flag_artifact = {"tool": tool, "job_id": job_id, "source": "fresh_runner_result", "candidate": "flag{<redacted>}"}
                fresh_flag_value = match.group(0)
            if result.get("status") != "COMPLETED":
                success = False
                break
        (final / "reproduction-commands.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + "\n".join(commands) + "\n", encoding="utf-8")
        for directory in ("requests", "scripts", "sqlmap"):
            source = root / directory
            destination = final / directory
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                destination.mkdir(parents=True, exist_ok=True)
        if flag_artifact:
            artifact_path = final / "fresh-flag-artifact.json"
            artifact_path.write_text(json.dumps({**flag_artifact, "candidate": "flag{<redacted>}", "source_value": "<redacted>"}, ensure_ascii=False, indent=2), encoding="utf-8")
            raw = artifact_path.read_bytes()
            artifact = Artifact(run_id=run.id, artifact_type="fresh_flag", file_path="final/fresh-flag-artifact.json", mime_type="application/json", size=len(raw), sha256=hashlib.sha256(raw).hexdigest(), summary="Fresh Runner flag artifact")
            session.add(artifact)
            await session.flush()
            if fresh_flag_value:
                existing = await session.scalar(select(FlagCandidate).where(FlagCandidate.run_id == run.id, FlagCandidate.candidate == fresh_flag_value))
                if existing is None:
                    session.add(FlagCandidate(run_id=run.id, candidate=fresh_flag_value, source_artifact_id=artifact.id, pattern_matched=True, verified=True, review_state="VALID"))
                else:
                    existing.source_artifact_id = artifact.id
                    existing.verified = True
                    existing.review_state = "VALID"
            await session.commit()
        validation = {"executed": bool(log), "verified": success and bool(log) and flag_artifact is not None, "fresh_session": True, "fresh_flag_artifact": flag_artifact is not None, "steps": log}
        (final / "reproduction-validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
        (final / "fresh-reproduction.log.json").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        if validation["verified"]:
            run.fresh_reproduction_verified = True
            await session.commit()
        return validation


fresh_reproduction_executor = FreshReproductionExecutor()
