"""Persistence for the DASCTF Agent arena integration.

Three tables, deliberately separate from ``model_configs``:

* :class:`ArenaConnection` holds one external arena endpoint plus its bearer
  token.  The token is encrypted with the same Fernet key the model API keys
  use (``app/services/crypto.py``), but it does **not** live in
  ``model_configs``: an arena credential is not a model configuration, and
  folding it in would pollute ``ModelConfigUsage`` reference counts with a row
  that no run can ever reference.

* :class:`ArenaSubmission` is the arena's own scorebook.  The platform exposes
  only a per-call verdict from ``submit_flag`` -- it keeps no history an agent
  can read back -- so any "which model solved what" claim has to be reconstructed
  from records written here.  The ``model_config_id`` / ``engine_key`` columns
  exist for exactly that attribution; without them the only reportable number is
  a total, which cannot distinguish a stronger model from a luckier one.

* :class:`ArenaSolveQueueItem` is the batch backlog.  A batch takes hours because
  the arena serves one environment at a time, so the queue has to outlive the
  process; see the class docstring for what the unique constraint is protecting.

The unique constraint on ``(connection_id, platform_challenge_id, attested_flag)``
is the duplicate-submit guard: several models may converge on the same flag for
one challenge, and a rejected duplicate is both wasted quota and, on platforms
that penalise wrong answers, a scoring risk.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDTimestampMixin


class ArenaConnection(UUIDTimestampMixin, Base):
    __tablename__ = "arena_connections"

    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    # Root of the arena's open API.  Kept as a full URL rather than a host so a
    # staging or self-hosted arena needs no code change.
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    encrypted_token: Mapped[str | None] = mapped_column(String(2048))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Local throttle.  The platform rate-limits per *agent*, not per token, and
    # publishes no number to configure against -- so the client paces itself and
    # backs off on 429 instead of trusting a remote quota it cannot read.
    requests_per_minute: Mapped[int] = mapped_column(Integer, default=60)
    # Defaults to 1 because the arena serves one running challenge environment
    # at a time.  Raising it here does not raise the platform's limit; it only
    # lets the client dispatch calls that the platform will still queue.
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_probe_ok: Mapped[bool | None] = mapped_column(Boolean)
    # Cached ``whoami`` payload (agent profile and quota).  Cache, not truth:
    # it is refreshed by an explicit probe and never read to authorise anything.
    profile_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Default runtime for a solve started from this connection.  Stored here
    # rather than in ``.env``: a model choice is solve policy, not a service
    # endpoint, and putting it on the connection keeps a future per-arena
    # override from needing a migration.
    default_model_config_id: Mapped[str | None] = mapped_column(ForeignKey("model_configs.id"))
    default_engine_type: Mapped[str | None] = mapped_column(String(40))
    default_solver_mode: Mapped[str] = mapped_column(String(30), default="muteki")
    # Muteki splits work across several Worker engines plus one Coordinator that
    # reasons over their results.  ``default_model_config_id`` above stays the
    # single-engine case; these two add the multi-engine one without replacing it,
    # so a connection that names only one model keeps working unchanged.
    #
    # ``default_worker_engines`` is a JSON list of ``{engine_type, model_config_id}``
    # rather than a child table: it is read as a whole, never queried by member,
    # and its length is bounded by the engine types the runtime supports.  A join
    # table would buy nothing and cost a migration per engine added.
    default_reason_model_config_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_configs.id")
    )
    default_worker_engines: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ArenaSubmission(UUIDTimestampMixin, Base):
    __tablename__ = "arena_submissions"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "platform_challenge_id",
            "attested_flag",
            name="uq_arena_submission_flag",
        ),
    )

    connection_id: Mapped[str] = mapped_column(
        ForeignKey("arena_connections.id"), nullable=False, index=True
    )
    platform_challenge_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    # Denormalised so the scorebook stays readable after the platform retires a
    # challenge, and so no listing call is needed to render a past attempt.
    challenge_name: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(40), default="")
    attested_flag: Mapped[str] = mapped_column(String(500), nullable=False)
    # accepted / rejected / duplicate / circuit_open
    verdict: Mapped[str] = mapped_column(String(30), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    # Attribution.  Nullable because a manual probe submission has no producer,
    # but a solved challenge normally names both.
    model_config_id: Mapped[str | None] = mapped_column(ForeignKey("model_configs.id"))
    engine_key: Mapped[str | None] = mapped_column(String(60))
    token_cost: Mapped[int | None] = mapped_column(Integer)


class ArenaSolveQueueItem(UUIDTimestampMixin, Base):
    """One challenge waiting to be solved.

    Why a table rather than an in-memory list
    -----------------------------------------
    The platform serves exactly one environment at a time, so a batch of
    challenges takes hours. A queue that lived only in process memory would be
    lost on the first restart, and -- worse -- would silently re-solve challenges
    that already passed. Persisting it means the scheduler can always answer
    "what is left, and what already failed" from the database.

    The unique constraint is what makes re-enqueueing safe: adding the same
    challenge twice hits the constraint rather than creating a duplicate that
    would be solved and submitted a second time. ``attempt_count`` is
    deliberately *not* part of it, so a failed item can be retried by touching
    that field rather than inserting a new row.
    """

    __tablename__ = "arena_solve_queue"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "practice_ground_id",
            "platform_challenge_id",
            name="uq_arena_queue_challenge",
        ),
        Index("ix_arena_queue_pick", "status", "priority", "created_at"),
    )

    connection_id: Mapped[str] = mapped_column(
        ForeignKey("arena_connections.id"), nullable=False, index=True
    )
    practice_ground_id: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_challenge_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Denormalised so the queue renders without a call to the platform, which is
    # rate-limited and would otherwise be hit once per row per page load.
    challenge_name: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(60), default="")
    difficulty: Mapped[str] = mapped_column(String(40), default="")
    #: Whether this challenge needs the single environment slot. Measured from the
    #: platform's own ``has_container``; recorded so a batch can be ordered to put
    #: offline challenges first, which keeps the scarce slot free for as long as
    #: possible.
    has_container: Mapped[bool] = mapped_column(Boolean, default=False)

    #: PENDING / RUNNING / SOLVED / UNSOLVED / FAILED / CANCELLED / SKIPPED
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    #: Lower runs first. Lets an operator push one challenge ahead of a backlog.
    priority: Mapped[int] = mapped_column(Integer, default=100)
    verdict: Mapped[str | None] = mapped_column(String(30))

    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    #: Attempts that actually reached a verdict.
    #:
    #: Separate from ``attempt_count`` because the retry budget must retire a
    #: broken challenge, not an unlucky sequence of restarts.  Incrementing one
    #: counter on claim could not tell those apart: a restart returns the item to
    #: ``PENDING``, the scheduler claims it again, and two restarts retired a
    #: challenge that had never once completed.  The budget is read from this
    #: field; ``attempt_count`` stays as the claim history.
    outcome_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    #: The Run this item used. Kept so a result can be traced back to its evidence.
    run_id: Mapped[str | None] = mapped_column(String(36))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
