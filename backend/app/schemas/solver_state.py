from pydantic import BaseModel


class SolverStateRead(BaseModel):
    id: str
    run_id: str
    current_phase: str
    confirmed_facts_json: list[dict]
    rejected_paths_json: list[dict]
    active_hypotheses_json: list[dict]
    action_fingerprints_json: dict
    active_skill_ids_json: list[str]
    skill_recommendations_json: list[dict]
    run_plan_json: dict = {}
    capability_ledger_json: dict = {}
    read_files_json: list[str] = []
    read_ranges_json: list[dict] = []
    content_hashes_json: dict = {}
    last_decision_card_json: dict = {}
    last_experiment_json: dict = {}
    no_progress_count: int
    last_progress_at: str | None
    created_at: str
    updated_at: str
    model_config = {"from_attributes": True}
