"""Start and track a benchmark matrix over HTTP (todo §11.1).

Kept out of ``knowledge.py`` on purpose: that module **reads** finished
experiments (items, stats, summaries), while everything here **starts** a
long-running one.  Keeping them apart also means a matrix can be launched
without touching the read contract the audit pages already depend on, hence the
distinct ``/knowledge/experiment-matrix`` prefix instead of re-declaring
``/knowledge/experiments`` routes.

The POST is non-blocking: it validates, spawns the child process and returns the
initial state.  Progress is read back from the experiment directory, so this
endpoint holds no state of its own and a backend restart cannot orphan a run.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.benchmark_matrix import (
    build_request,
    list_experiments,
    read_experiment,
    start,
)

router = APIRouter(prefix="/knowledge/experiment-matrix", tags=["knowledge"])


class MatrixLaunchRequest(BaseModel):
    """Body of a launch request; every field is optional except the cases."""

    experiment_id: str | None = Field(
        default=None,
        description="Directory name under backend/data/experiments; defaults to a UTC timestamp.",
    )
    case_dir: str | None = Field(default=None, description="Defaults to app/benchmark/cases.")
    output_dir: str | None = Field(
        default=None,
        description="Must stay inside backend/data/experiments.",
    )
    arms: list[str] | str | None = None
    repeats: int | None = Field(default=None, ge=1, le=50)
    randomize_range: str | None = Field(
        default=None,
        description="never | run | repeat — rotates the range port and flag.",
    )
    worker_model_config_id: str | None = None
    reason_model_config_id: str | None = None
    resume: bool = False
    dry_run: bool = False


@router.post("", status_code=201)
async def launch_matrix(payload: MatrixLaunchRequest) -> dict[str, Any]:
    request = build_request(payload.model_dump(exclude_none=True))
    return {"data": start(request)}


@router.get("")
async def list_matrices() -> dict[str, Any]:
    return {"data": list_experiments()}


@router.get("/{experiment_id}")
async def matrix_status(experiment_id: str) -> dict[str, Any]:
    return {"data": read_experiment(experiment_id)}
