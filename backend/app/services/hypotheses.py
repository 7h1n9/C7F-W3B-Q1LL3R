from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.run import Hypothesis


class HypothesisService:
    async def upsert_from_action(
        self,
        session: AsyncSession,
        run_id: str,
        *,
        phase: str | None,
        objective: str | None,
        hypothesis_text: str | None,
        evidence: dict | None = None,
        confidence: int = 20,
    ) -> tuple[Hypothesis, bool]:
        title = (hypothesis_text or objective or phase or "Untitled hypothesis").strip()
        item = await session.scalar(
            select(Hypothesis).where(Hypothesis.run_id == run_id, Hypothesis.title == title)
        )
        created = item is None
        if item is None:
            item = Hypothesis(
                run_id=run_id,
                category=(phase or "GENERAL").upper(),
                title=title,
                description=(objective or "")[:4000],
                confidence=confidence,
                priority=max(confidence, 20),
                status="TESTING",
                evidence_json=evidence or {},
                attempt_count=1,
            )
            session.add(item)
        else:
            item.description = (objective or item.description)[:4000]
            item.confidence = max(item.confidence, confidence)
            item.status = "TESTING"
            item.evidence_json = {**(item.evidence_json or {}), **(evidence or {})}
            item.attempt_count += 1
        await session.commit()
        await session.refresh(item)
        return item, created

    async def mark_result(
        self,
        session: AsyncSession,
        hypothesis_id: str | None,
        *,
        result_status: str,
        observation: dict | None = None,
        evidence: dict | None = None,
    ) -> Hypothesis | None:
        if not hypothesis_id:
            return None
        item = await session.get(Hypothesis, hypothesis_id)
        if not item:
            return None
        if result_status == "COMPLETED":
            item.status = "SUPPORTED"
            item.confidence = min(100, item.confidence + 25)
        elif result_status in {"FAILED", "REJECTED"}:
            item.status = "REJECTED"
            item.confidence = max(0, item.confidence - 10)
        item.evidence_json = {
            **(item.evidence_json or {}),
            **(evidence or {}),
            **({"observation": observation} if observation else {}),
        }
        await session.commit()
        await session.refresh(item)
        return item


hypothesis_service = HypothesisService()
