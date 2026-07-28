from typing import Any

from app.schemas.multi_agent import AgentRole, AgentTaskResultContract, AgentTaskStatus


class StructuredAgent:
    role: AgentRole

    def result(self, task_id: str, *, status: AgentTaskStatus = AgentTaskStatus.COMPLETED, **values: Any) -> AgentTaskResultContract:
        """Build a bounded result; persistence remains controller-owned."""
        return AgentTaskResultContract(task_id=task_id, status=status, **values)

