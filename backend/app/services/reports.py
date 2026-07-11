import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import Artifact, FlagCandidate, Hypothesis, Observation, SolveRun, ToolCall
from app.services.events import event_service


class ReportService:
    async def generate(
        self,
        session: AsyncSession,
        run: SolveRun,
        challenge: Challenge,
        result: str,
        failure_reason: str = "",
    ) -> Artifact:
        await event_service.append(session, run.id, "report.started", {})
        calls = list(
            (await session.scalars(select(ToolCall).where(ToolCall.run_id == run.id))).all()
        )
        observations = list(
            (await session.scalars(select(Observation).where(Observation.run_id == run.id))).all()
        )
        hypotheses = list(
            (await session.scalars(select(Hypothesis).where(Hypothesis.run_id == run.id))).all()
        )
        flags = list(
            (
                await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run.id))
            ).all()
        )
        lines = [
            f"# {challenge.name}",
            "",
            "## Run",
            f"- Engine: {run.engine_type}",
            f"- Result: {result}",
            "",
            "## Agent steps",
            f"- Steps: {run.agent_step_count}",
            "",
            "## Tool calls",
        ]
        lines += [f"- {item.tool_name}: {item.status}" for item in calls] or ["- None"]
        lines += ["", "## Observations"] + [f"- {item.summary}" for item in observations]
        lines += ["", "## Hypotheses"] + [f"- {item.title} ({item.status})" for item in hypotheses]
        lines += ["", "## Flag candidates"] + [
            f"- {item.candidate}: {'verified' if item.verified else 'unverified'}" for item in flags
        ]
        lines += ["", "## Failure reason", failure_reason or "None"]
        raw = "\n".join(lines).encode()
        root = Path(run.workspace_path).resolve()
        path = root / "final" / "writeup.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        artifact = Artifact(
            run_id=run.id,
            artifact_type="report",
            file_path="final/writeup.md",
            mime_type="text/markdown",
            size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            summary="Generated run writeup",
        )
        session.add(artifact)
        await session.commit()
        await event_service.append(
            session,
            run.id,
            "artifact.created",
            {"artifact_id": artifact.id, "path": artifact.file_path},
        )
        await event_service.append(
            session, run.id, "report.completed", {"artifact_id": artifact.id}
        )
        return artifact


report_service = ReportService()
