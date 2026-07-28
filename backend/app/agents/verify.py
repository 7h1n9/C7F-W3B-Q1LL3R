from app.agents.base import StructuredAgent
from app.schemas.multi_agent import AgentRole


class VerifyAgent(StructuredAgent):
    role = AgentRole.VERIFY

    def verified(self, task_id: str, *, candidate: str, evidence_ids: list[str], fresh_reproduction: bool, summary: str = ""):
        return self.result(
            task_id, evidence_ids=evidence_ids,
            new_facts=[{"fact_key": f"verified_flag:{candidate}", "fact_type": "VERIFIED_FLAG", "value": {"candidate": candidate, "fresh_reproduction": fresh_reproduction}, "confidence": 100}],
            handoff_summary=summary or ("Fresh reproduction completed." if fresh_reproduction else "Fresh reproduction failed."),
        )
