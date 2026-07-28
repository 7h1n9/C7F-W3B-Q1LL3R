from typing import Any

from pydantic import BaseModel, Field


class WebResearchRequest(BaseModel):
    task_id: str | None = None
    query: str = Field(min_length=3, max_length=1000)
    query_type: str = Field(default="GENERAL_TECHNIQUE", max_length=40)
    requested_by: str = Field(default="ANALYSIS", pattern="^(PLANNER|ANALYSIS)$")


class WebResearchPromotion(BaseModel):
    record_id: str
    fact_ids: list[str] = Field(min_length=1, max_length=100)


class WebResearchResult(BaseModel):
    record_id: str | None = None
    status: str
    risk_level: str
    answer_leak_risk: str
    summary: str = ""
    source_urls: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)

