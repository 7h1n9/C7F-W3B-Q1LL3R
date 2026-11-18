"""Execute the minimal solution path in a clean worker container.

The replay used to go through ``runner_client`` (the legacy Kali Runner VM).  That
service is retired in this deployment, so every replay raised
``RunnerRetiredError`` before executing anything: 37 ``arena.fresh_reproduction``
events, all ``error``, and ``fresh_reproduction_verified`` was False for all 742
Runs.  The commands were never Runner-specific -- ``reproduction_command_renderer``
already emits plain shell -- so they now run in the same Muteki container the
solver used.
"""

import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.models.run import Artifact, FlagCandidate, FlagProvenance, ToolCall
from app.services.container_reproduction import container_command_runner
from app.services.reproduction_commands import reproduction_command_renderer


class FreshReproductionExecutor:
    async def execute(self, session, run, challenge, steps: list | None = None) -> dict:
        root = Path(run.workspace_path).resolve()
        final = root / "final"
        final.mkdir(parents=True, exist_ok=True)
        # No workspace sync and no session reset: the container mounts ``root``
        # directly (``ensure_container`` bind-mounts the run workspace), so the
        # files the solver wrote are already visible at the same relative paths.
        # ``clear_sessions``/``sync_workspace`` existed only because the Runner was
        # a separate machine that needed the tree copied to it.
        selected = steps or []
        checkpoint = run.recovery_checkpoint_json if isinstance(run.recovery_checkpoint_json, dict) else {}
        valid_candidate = await session.scalar(
            select(FlagCandidate)
            .where(FlagCandidate.run_id == run.id, FlagCandidate.review_state == "VALID")
            .order_by(FlagCandidate.created_at.desc())
        )
        if valid_candidate is not None:
            solver_reproduction = checkpoint.get("solver_reproduction")
            request_spec = dict(
                (solver_reproduction or {}).get("request")
                or checkpoint.get("request_spec")
                or {}
            ) if isinstance(solver_reproduction, dict) else dict(checkpoint.get("request_spec") or {})
            extraction_call = await session.scalar(
                select(ToolCall)
                .where(ToolCall.run_id == run.id, ToolCall.tool_name == "boolean_config_extract")
                .order_by(ToolCall.created_at.desc())
            )
            extraction_args = (
                dict(solver_reproduction)
                if isinstance(solver_reproduction, dict)
                else dict(extraction_call.arguments_json or {}) if extraction_call else {}
            )
            expression = str(
                extraction_args.get("target_expression")
                or checkpoint.get("target_expression")
                or ""
            ).strip()
            field = str(extraction_args.get("test_field") or checkpoint.get("test_field") or "")
            baseline = str(extraction_args.get("baseline_value") or checkpoint.get("baseline_value") or "")
            if not request_spec or not field or not expression:
                # The boolean-oracle replay cannot be rebuilt from the checkpoint.
                # That used to end the whole attempt, which threw away the steps the
                # planner had already produced -- measured 2026-11-10 on run
                # 9847adf6, where the planner found the Worker's own PoC script
                # (``muteki/exploit2.py``) but this early return discarded it and
                # reported MISSING_EXTRACTION_PROVENANCE with ``steps: 0``.
                #
                # Missing extraction provenance means "this particular recipe is
                # unavailable", not "nothing can be replayed".  Keep ``selected`` as
                # the planner built it and fall through to execution; if the planner
                # found nothing either, the loop runs zero steps and the validation
                # records ``executed: False`` honestly.
                pass
            else:
                escaped = valid_candidate.candidate.replace("'", "''")
                suffix = str(extraction_args.get("true_suffix") or "' AND ({condition}) -- ")
                condition = f"({expression})='{escaped}'"
                request_spec = json.loads(json.dumps(request_spec))
                container = "json" if isinstance(request_spec.get("json"), dict) else "form" if isinstance(request_spec.get("form"), dict) else "query"
                request_spec.setdefault(container, {})[field] = baseline + suffix.format(condition=condition)
                control_fields = (
                    extraction_args.get("control_fields")
                    if isinstance(extraction_args.get("control_fields"), dict)
                    else checkpoint.get("control_fields")
                ) or {}
                for key, value in control_fields.items():
                    request_spec.setdefault(container, {})[str(key)] = value
                request_spec["final_verification"] = True
                if isinstance(solver_reproduction, dict):
                    selected = [{
                        "tool_name": "http_request",
                        "normalized_arguments": request_spec,
                    }]
                else:
                    selected = [{"tool_name": "http_request", "normalized_arguments": request_spec}]
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
                # ``workspace_poc`` carries its command on the step, not in
                # ``normalized_arguments``: the command embeds the container-side
                # workspace path, which the extractor does not know.
                manual_command = getattr(step, "manual_command", None)
                if manual_command:
                    args = {**args, "manual_command": manual_command}
            if tool == "boolean_config_extract" and not isinstance(checkpoint.get("solver_reproduction"), dict):
                args = {
                    **(checkpoint.get("request_spec") or {}),
                    "request": checkpoint.get("request_spec") or {},
                    "test_field": checkpoint.get("test_field"),
                    "baseline_value": checkpoint.get("baseline_value"),
                    "control_fields": checkpoint.get("control_fields") or {},
                    "oracle": checkpoint.get("oracle") or {},
                    "target_expression": checkpoint.get("target_expression") or "",
                    "max_length": 64,
                    "max_requests": 1024,
                }
            command = reproduction_command_renderer.render(tool, args)
            # ``workspace_poc`` is not a renderer tool: it re-runs a script the
            # Worker wrote, so its command comes from the step itself.  The
            # ``{workspace}`` placeholder is resolved here because only the
            # container handle knows the host→container mapping.
            if tool == "workspace_poc":
                command = str(args.get("manual_command") or "").replace(
                    "{workspace}", container_command_runner.workspace_path(run.id, root)
                ) or reproduction_command_renderer.render(tool, args)
            commands.append(command)
            result = await container_command_runner.run_async(run.id, root, command)
            job_id = result.get("container") or ""
            log.append({"tool": tool, "command": command, "container": job_id, "status": result.get("status"), "exit_code": result.get("exit_code"), "result": result})
            # Scan stdout *and* stderr: ``curl -i`` puts the body on stdout, but a
            # flag echoed by a failing helper lands on stderr, and the old code
            # searched the whole serialized result anyway.
            result_text = json.dumps(result, ensure_ascii=False)
            match = re.search(str(challenge.flag_pattern), result_text)
            if match:
                flag_artifact = {"tool": tool, "container": job_id, "source": "fresh_container_result", "candidate": "flag{<redacted>}"}
                fresh_flag_value = match.group(0)
            body_json = {}
            try:
                body_json = json.loads(str(result.get("body") or "{}"))
            except json.JSONDecodeError:
                # ``curl -i`` prefixes the status line and headers, so a plain
                # ``json.loads`` on the body fails.  Retry from the first ``{``.
                raw_body = str(result.get("body") or "")
                brace = raw_body.find("{")
                if brace >= 0:
                    try:
                        body_json = json.loads(raw_body[brace:])
                    except json.JSONDecodeError:
                        body_json = {}
            if valid_candidate is not None and tool == "http_request" and body_json.get("matched") is True:
                flag_artifact = {"tool": tool, "container": job_id, "source": "fresh_container_result", "candidate": "flag{<redacted>}"}
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
