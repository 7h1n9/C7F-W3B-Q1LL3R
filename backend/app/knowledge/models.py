"""Frozen data models for curated security knowledge."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: str
    source_ref: str
    source_url: str | None = None
    notes: str | None = None


class KnowledgeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["technique", "case", "poc", "negative"]
    title: str
    technique: str
    summary: str
    preconditions: list[str] = Field(default_factory=list)
    target_stack: list[str] = Field(default_factory=list)
    payload_template: str | None = None
    verification_method: list[str] = Field(default_factory=list)
    known_failures: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    # What to do after the initial exploit lands.  A CVE box whose flag is not
    # readable by the exploited account needs a second phase; without an explicit
    # ordering the Worker either stops or re-runs the same exploit.
    post_exploitation: list[str] = Field(default_factory=list)
    provenance: KnowledgeProvenance
    published_at: date | None = None
    trust_level: Literal["reviewed", "curated", "unverified"]
    # Did this recipe actually produce an accepted flag on the live arena?
    #
    # ``trust_level`` says who wrote the entry and how carefully; it does not say
    # whether the described request works on a real box.  Those came apart in
    # practice: the ``/ajax/example5`` S2-009 recipe was copied from vulhub, was
    # internally consistent, passed every schema check -- and was wrong for the
    # instance, because that endpoint renders ``name`` with ``<s:property>`` and
    # never evaluates it.  It still occupied the goal's FIRST ACTION slot, which
    # is the one instruction a Worker cannot skim past, so a weak model would
    # have been led into the dead end by the very hint meant to help it.
    #
    # So the slot is now reserved for recipes with a measured solve behind them.
    # Everything else still reaches ``AGENTS.md`` as reading material; it just
    # does not get to issue orders.
    verified_solve: bool = False
    # Literal names of the box this recipe is *about*, e.g.
    # ``["hitcon 2016 leaking"]``.
    #
    # The two token axes need a product word and a weakness word in the title.
    # Some boxes carry neither: ``[HITCON 2016]Leaking`` names a CTF and a verb,
    # so every axis missed it for seven rounds while the box was in fact a
    # textbook Node.js heap read.  A recipe that names the box outright is not a
    # heuristic -- it is an identification -- so it sorts ahead of both axes.
    box_names: list[str] = Field(default_factory=list)


class RetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: KnowledgeItem
    score: float
    matched_terms: list[str]


class RetrievalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    metadata: dict
    top_k: int
    results: list[RetrievalResult]
    searched_at: datetime
