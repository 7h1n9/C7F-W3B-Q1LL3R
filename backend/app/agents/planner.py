
from app.agents.base import StructuredAgent
from app.schemas.multi_agent import AgentRole, PlannerProposalContract, TaskBudget


class PlannerAgent(StructuredAgent):
    role = AgentRole.PLANNER

    def propose(self, *, run_id: str, proposal_id: str, current_stage: str, next_agent: AgentRole, objective: str, input_fact_ids: list[str] | None = None, allowed_tools: list[str] | None = None, success_condition: str, stop_conditions: list[str] | None = None, budget: TaskBudget | None = None, required_capabilities: list[str] | None = None, fallback: str = "RETURN_TO_ANALYSIS") -> PlannerProposalContract:
        return PlannerProposalContract(
            run_id=run_id, proposal_id=proposal_id, current_stage=current_stage, next_agent=next_agent,
            objective=objective, input_fact_ids=input_fact_ids or [], allowed_tools=allowed_tools or [],
            success_condition=success_condition, stop_conditions=stop_conditions or [], budget=budget or TaskBudget(),
            required_capabilities=required_capabilities or [], fallback=fallback,
        )

