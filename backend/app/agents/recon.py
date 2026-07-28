from app.agents.base import StructuredAgent
from app.schemas.multi_agent import AgentRole


class ReconAgent(StructuredAgent):
    role = AgentRole.RECON

    def normalize(self, task_id: str, facts: list[dict], evidence_ids: list[str], summary: str = ""):
        return self.result(task_id, new_facts=facts, evidence_ids=evidence_ids, handoff_summary=summary)

