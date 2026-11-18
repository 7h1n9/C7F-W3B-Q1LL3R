"""Request/response models for the arena integration.

The write model accepts a plaintext ``token``; the read model never returns it.
That asymmetry is the whole contract: an operator can set a credential and can see
whether one is set, but no read path -- API response, export, or log -- carries the
value back out.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class ArenaWorkerEngine(BaseModel):
    """One Worker engine plus the model it runs on.

    Mirrors ``WorkerEngineSelection`` in the Run schema.  Kept as its own model
    rather than imported so this schema stays independent of the run module --
    the two are the same shape today but are free to diverge, and a shared import
    would make the arena's request contract move whenever a Run field does.
    """

    model_config = ConfigDict(extra="ignore")

    engine_type: str = Field(min_length=1, max_length=40)
    model_config_id: str | None = None


class ArenaConnectionWrite(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    base_url: HttpUrl
    # Optional on update: a blank value means "keep the stored credential", which
    # is what lets an operator rename a connection without re-pasting the token.
    token: str | None = None
    enabled: bool = True
    requests_per_minute: int = Field(default=60, ge=1, le=600)
    # The arena serves one challenge environment at a time, so this defaults to a
    # serial dispatch. It is a local pace limit, not a request to the platform.
    max_concurrency: int = Field(default=1, ge=1, le=4)
    # Which runtime a solve started from this connection uses.  Nullable on
    # purpose: "no default chosen" is a real state and must not silently become
    # whichever model happens to be first.
    default_model_config_id: str | None = None
    default_engine_type: str | None = None
    default_solver_mode: str = Field(default="muteki", pattern="^(single_agent|muteki)$")
    # Muteki's multi-engine split.  An empty ``default_worker_engines`` means "use
    # the single ``default_model_config_id`` above", so a connection configured
    # before this field existed keeps working without being rewritten.
    default_reason_model_config_id: str | None = None
    default_worker_engines: list[ArenaWorkerEngine] = Field(default_factory=list, max_length=8)

    @field_validator("default_worker_engines", mode="before")
    @classmethod
    def coerce_null_worker_engines(cls, value: object) -> object:
        # A client sending ``null`` means "no multi-engine setup", same as an empty
        # list.  Rejecting it would make clearing the field awkward.
        return [] if value is None else value


class ArenaConnectionRead(BaseModel):
    id: str
    name: str
    base_url: str
    token_configured: bool
    enabled: bool
    requests_per_minute: int
    max_concurrency: int
    last_probe_at: datetime | None = None
    last_probe_ok: bool | None = None
    profile_json: dict = Field(default_factory=dict)
    default_model_config_id: str | None = None
    default_engine_type: str | None = None
    default_solver_mode: str = "muteki"
    default_reason_model_config_id: str | None = None
    default_worker_engines: list[ArenaWorkerEngine] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ArenaSubmissionRead(BaseModel):
    id: str
    connection_id: str
    platform_challenge_id: str
    challenge_name: str
    category: str
    verdict: str
    submitted_at: datetime
    model_config_id: str | None = None
    engine_key: str | None = None
    token_cost: int | None = None


class ArenaSolveRequest(BaseModel):
    """One challenge to solve end to end.

    The identifiers are required rather than discovered: the platform allows a
    single environment, so an endpoint that also picked the challenge would make
    it impossible to tell an operator's choice from an automatic one when
    something goes wrong.
    """

    model_config = ConfigDict(extra="ignore")

    practice_ground_id: str = Field(min_length=1)
    challenge_id: str = Field(min_length=1)


class ArenaCatalogueItem(BaseModel):
    """A reading of the MCP catalogue.  Fields mirror the platform's own."""

    model_config = ConfigDict(extra="allow")

    id: str
    friendly_id: str | None = None
    name: str | None = None
    category: str | None = None
    difficulty: str | None = None
    points: int | None = None
    has_attachments: bool = False
    has_container: bool = False
    is_solved: bool = False
    solve_count: int | None = None


class ArenaEnqueueRequest(BaseModel):
    """Compose a batch from the platform catalogue rather than naming ids.

    Filtering server-side is the point: the catalogue runs to hundreds of
    challenges, and making a caller page through it to build a list would push
    rate-limited reads onto the client for no benefit.
    """

    model_config = ConfigDict(extra="ignore")

    practice_ground_id: str = Field(min_length=1)
    category: str | None = None
    difficulty: str | None = None
    #: Sort order for the batch.  ``container_first`` is the default because a
    #: container challenge is the only kind that can be *scored*: the
    #: attachment-only entries in this catalogue are largely teaching exercises
    #: whose name carries ``[无flag]`` and which have no submittable flag at all.
    #: Running those first spent the batch on challenges that could never score.
    #: Use ``offline_first`` only when the goal is practice rather than points.
    order: str = Field(default="container_first", pattern="^(offline_first|container_first|as_listed)$")
    #: Skip challenges the platform reports as already solved by this agent.
    skip_solved: bool = True
    limit: int = Field(default=10, ge=1, le=100)


class ArenaEnqueueByIdRequest(BaseModel):
    """Queue specific challenges by id, rather than a catalogue slice.

    The catalogue's ordering is the platform's, not a usefulness ranking, and a
    slice like "first 6 challenges" is mostly practice ranges (``sqli-labs``,
    ``Juice Shop``) that exist to be learned on and have no flag to submit.
    Naming the ids is the only way to run the ones that can actually score.
    """

    model_config = ConfigDict(extra="ignore")

    practice_ground_id: str = Field(min_length=1)
    challenge_ids: list[str] = Field(min_length=1, max_length=50)
    #: Lower runs sooner; the scheduler orders by this alone.  Defaults to 0 so an
    #: explicitly chosen batch outranks the default 100 given to catalogue slices.
    priority: int = Field(default=0, ge=0, le=1000)


class ArenaQueueItemRead(BaseModel):
    id: str
    connection_id: str
    practice_ground_id: str
    platform_challenge_id: str
    challenge_name: str
    category: str
    difficulty: str
    has_container: bool
    status: str
    priority: int
    verdict: str | None = None
    #: Times this row was claimed, and times a solve actually reached a verdict.
    #: The retry budget is read from ``outcome_count``; ``attempt_count`` is the
    #: claim history, which restarts inflate without the challenge being at fault.
    attempt_count: int
    outcome_count: int = 0
    last_error: str | None = None
    run_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class ArenaQueueBulkRequest(BaseModel):
    """One action applied to an explicit set of queue rows.

    Explicit ids rather than a status filter, deliberately: a bulk action that
    re-derived its targets from "everything PENDING" would also act on rows the
    operator never saw -- whatever a batch enqueued between render and click.
    The set the operator selected is the set that changes.
    """

    model_config = ConfigDict(extra="ignore")

    item_ids: list[str] = Field(min_length=1, max_length=200)
    #: * ``retry``  -- FAILED/UNSOLVED/CANCELLED back to PENDING, attempts reset.
    #: * ``cancel`` -- stop a waiting or running item.
    #: * ``top``    -- give the item the earliest priority so it runs next.
    #: * ``delete`` -- remove finished rows from the list entirely.
    action: Literal["retry", "cancel", "top", "delete"]


class ArenaQueuePriorityRequest(BaseModel):
    """Move one item relative to the backlog."""

    model_config = ConfigDict(extra="ignore")

    #: ``top`` puts it ahead of everything already queued without renumbering the
    #: rest; ``bottom`` sends it to the end.  Named rather than numeric so the UI
    #: does not have to know what the scheduler's ordering constant is.
    position: Literal["top", "bottom"]

