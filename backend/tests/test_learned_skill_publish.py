"""Publishing an approved learned-skill candidate into the recall corpus (CAP-15)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.exceptions import DomainError
from app.knowledge.audit import audit_published_at
from app.knowledge.loader import load_knowledge_items
from app.models import Base
from app.models.challenge import Challenge
from app.models.learned_skill import LearnedSkillCandidate
from app.models.run import SolveRun
from app.services import learned_skills as learned_skills_module
from app.services.learned_skills import (
    LEARNED_CORPUS_FILENAME,
    LearnedSkillService,
    PublicationCuration,
    _steps_from_markdown,
)

FINISHED = datetime(2026, 3, 4, 11, 22, tzinfo=UTC)
MARKDOWN = (
    "# Generic Console 通用解题经验\n"
    "\n"
    "适用范围：授权 Web CTF。\n"
    "\n"
    "## 已验证步骤\n"
    "1. 建立基线：先记录一次控制请求\n"
    "2. 探测注入：只改变单引号\n"
    "\n"
    "## 安全约束\n"
    "仅通过 Tool Gateway 执行工具。\n"
)


@pytest.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as opened:
        yield opened
    await engine.dispose()


async def _candidate(
    session,
    *,
    status: str = "APPROVED",
    scan: dict | None = None,
    content: str = MARKDOWN,
    name: str = "learned-generic-console",
) -> LearnedSkillCandidate:
    challenge = Challenge(
        name="Generic Console",
        challenge_type="WEB_TARGET",
        target_url="http://target.test/",
        allowed_hosts=["target.test"],
        metadata_json={},
    )
    session.add(challenge)
    await session.flush()
    run = SolveRun(
        challenge_id=challenge.id,
        workspace_path=".",
        solver_mode="muteki",
        status="COMPLETED_SOLVED",
        finished_at=FINISHED,
    )
    session.add(run)
    await session.flush()
    candidate = LearnedSkillCandidate(
        name=name,
        display_name="待审核：Generic Console",
        description="Derived summary.",
        status=status,
        content_markdown=MARKDOWN,
        sanitized_content=content,
        source_run_id=run.id,
        source_artifact_ids=[],
        source_observation_ids=[],
        metadata_json={},
        security_scan_json={"passed": True} if scan is None else scan,
        generalization_score=70,
    )
    session.add(candidate)
    await session.flush()
    return candidate


def _curation(**overrides) -> PublicationCuration:
    values = {
        "technique": "sql-injection",
        "known_failures": ("A WAF normalizes the payload.",),
        "verification_method": ("Compare the control request with the candidate request.",),
        "target_stack": ("Flask",),
    }
    values.update(overrides)
    return PublicationCuration(**values)


def _read_corpus(root) -> list[dict]:
    path = root / LEARNED_CORPUS_FILENAME
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _expect(code: str):
    return pytest.raises(DomainError, match=code)


# --------------------------------------------------------------------- happy path


async def test_publish_writes_a_curated_case_entry(session, tmp_path) -> None:
    candidate = await _candidate(session)

    item = await LearnedSkillService().publish(session, candidate.id, _curation(), root=tmp_path)

    assert item.kind == "case"
    assert item.trust_level == "curated"
    assert item.id == "learned-generic-console"
    assert item.provenance.source_type == "learned"
    assert item.provenance.source_url is None
    assert item.payload_template is None
    assert item.known_failures == ["A WAF normalizes the payload."]

    written = _read_corpus(tmp_path)
    assert len(written) == 1
    assert written[0]["id"] == item.id
    # The file must satisfy the same loader the retriever uses.
    loaded = load_knowledge_items(tmp_path)
    assert [entry.id for entry in loaded] == [item.id]
    assert loaded[0].known_failures


async def test_publish_dates_the_entry_by_the_source_run(session, tmp_path) -> None:
    candidate = await _candidate(session)

    item = await LearnedSkillService().publish(session, candidate.id, _curation(), root=tmp_path)

    assert item.published_at == FINISHED.date()
    assert _read_corpus(tmp_path)[0]["published_at"] == "2026-03-04"


async def test_publish_recovers_steps_from_the_candidate_markdown(session, tmp_path) -> None:
    candidate = await _candidate(session)

    item = await LearnedSkillService().publish(session, candidate.id, _curation(), root=tmp_path)

    assert item.steps == [
        "1. 建立基线：先记录一次控制请求",
        "2. 探测注入：只改变单引号",
    ]


async def test_publish_prefers_explicitly_curated_steps(session, tmp_path) -> None:
    candidate = await _candidate(session)

    item = await LearnedSkillService().publish(
        session, candidate.id, _curation(steps=("Do the thing.",)), root=tmp_path
    )

    assert item.steps == ["Do the thing."]


async def test_publish_redacts_curated_free_text(session, tmp_path) -> None:
    candidate = await _candidate(session)

    item = await LearnedSkillService().publish(
        session,
        candidate.id,
        _curation(summary="Replay against http://10.1.2.3/admin with flag{abc} in the body."),
        root=tmp_path,
    )

    assert "10.1.2.3" not in item.summary
    assert "flag{abc}" not in item.summary
    raw = (tmp_path / LEARNED_CORPUS_FILENAME).read_text(encoding="utf-8")
    assert "10.1.2.3" not in raw
    assert "flag{abc}" not in raw
    assert "redacted before writing" in item.provenance.notes


async def test_publish_records_the_entry_on_the_candidate(session, tmp_path) -> None:
    candidate = await _candidate(session)

    item = await LearnedSkillService().publish(session, candidate.id, _curation(), root=tmp_path)
    await session.refresh(candidate)

    assert candidate.metadata_json["published_knowledge_id"] == item.id
    assert candidate.metadata_json["published_at"] == "2026-03-04"
    assert candidate.metadata_json["published_corpus"].endswith(LEARNED_CORPUS_FILENAME)


# ----------------------------------------------------------------------- refusals


async def test_publish_refuses_a_candidate_that_is_not_approved(session, tmp_path) -> None:
    candidate = await _candidate(session, status="REVIEW_REQUIRED")

    with _expect("LEARN_CANDIDATE_NOT_APPROVED") as raised:
        await LearnedSkillService().publish(session, candidate.id, _curation(), root=tmp_path)

    assert raised.value.status_code == 422
    assert raised.value.details == {"status": "REVIEW_REQUIRED"}
    assert not (tmp_path / LEARNED_CORPUS_FILENAME).exists()


async def test_publish_refuses_a_candidate_that_failed_its_scan(session, tmp_path) -> None:
    candidate = await _candidate(session, scan={"passed": False})

    with _expect("LEARN_SCAN_NOT_PASSED"):
        await LearnedSkillService().publish(session, candidate.id, _curation(), root=tmp_path)


async def test_publish_requires_known_failures(session, tmp_path) -> None:
    """The loader rejects a ``case`` without known_failures, so publish must too."""

    candidate = await _candidate(session)

    with _expect("LEARN_KNOWN_FAILURES_REQUIRED"):
        await LearnedSkillService().publish(
            session, candidate.id, _curation(known_failures=()), root=tmp_path
        )

    assert not (tmp_path / LEARNED_CORPUS_FILENAME).exists()


async def test_publish_requires_a_verification_method(session, tmp_path) -> None:
    candidate = await _candidate(session)

    with _expect("LEARN_VERIFICATION_REQUIRED"):
        await LearnedSkillService().publish(
            session, candidate.id, _curation(verification_method=()), root=tmp_path
        )


async def test_publish_requires_a_technique_label(session, tmp_path) -> None:
    candidate = await _candidate(session)

    with _expect("LEARN_TECHNIQUE_REQUIRED"):
        await LearnedSkillService().publish(session, candidate.id, _curation(technique="  "), root=tmp_path)


async def test_publish_requires_recoverable_steps(session, tmp_path) -> None:
    candidate = await _candidate(session, content="# 无步骤\n\n只有一段说明。\n")

    with _expect("LEARN_STEPS_REQUIRED"):
        await LearnedSkillService().publish(session, candidate.id, _curation(), root=tmp_path)


async def test_publish_refuses_a_second_publication(session, tmp_path) -> None:
    candidate = await _candidate(session)
    service = LearnedSkillService()
    await service.publish(session, candidate.id, _curation(), root=tmp_path)

    with _expect("LEARN_ALREADY_PUBLISHED") as raised:
        await service.publish(session, candidate.id, _curation(), root=tmp_path)

    assert raised.value.status_code == 409
    assert len(_read_corpus(tmp_path)) == 1


async def test_publish_rolls_back_when_the_corpus_would_not_load(session, tmp_path, monkeypatch) -> None:
    """Publication is fail-closed: a rejected entry must leave the corpus untouched."""

    class _ExplodingLoader:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def load(self):
            raise ValueError("duplicate knowledge id 'x'")

    monkeypatch.setattr(learned_skills_module, "KnowledgeLoader", _ExplodingLoader)
    candidate = await _candidate(session)

    with _expect("LEARN_PUBLICATION_INVALID"):
        await LearnedSkillService().publish(session, candidate.id, _curation(), root=tmp_path)

    assert not (tmp_path / LEARNED_CORPUS_FILENAME).exists()


async def test_publish_rolls_back_to_the_previous_file_contents(session, tmp_path, monkeypatch) -> None:
    path = tmp_path / LEARNED_CORPUS_FILENAME
    path.write_text('{"id":"pre-existing"}\n', encoding="utf-8")

    real_loader = learned_skills_module.KnowledgeLoader

    class _ExplodingLoader:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def load(self):
            raise ValueError("boom")

    monkeypatch.setattr(learned_skills_module, "KnowledgeLoader", _ExplodingLoader)
    candidate = await _candidate(session)

    with _expect("LEARN_PUBLICATION_INVALID"):
        await LearnedSkillService().publish(session, candidate.id, _curation(), root=tmp_path)

    monkeypatch.setattr(learned_skills_module, "KnowledgeLoader", real_loader)
    assert path.read_text(encoding="utf-8") == '{"id":"pre-existing"}\n'
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


# ------------------------------------------------------------------ small helpers


def test_steps_from_markdown_stops_at_the_next_heading() -> None:
    assert _steps_from_markdown("## 已验证步骤\n1. a\n2. b\n\n## 安全约束\n- c\n") == ["1. a", "2. b"]


def test_steps_from_markdown_is_empty_without_the_heading() -> None:
    assert _steps_from_markdown("# 标题\n正文\n") == []
    assert _steps_from_markdown("") == []


def test_knowledge_id_strips_the_candidate_prefix() -> None:
    assert LearnedSkillService._knowledge_id("learned-My Challenge!") == "learned-my-challenge"
    assert LearnedSkillService._knowledge_id("plain") == "learned-plain"
    assert LearnedSkillService._knowledge_id("") == "learned-verified-path"


# ------------------------------------------------------------------------ audit


def _entry(item_id: str, *, published: str | None, source_type: str, source_ref: str) -> dict:
    return {
        "id": item_id,
        "kind": "case",
        "title": f"Title {item_id}",
        "technique": "strategy",
        "summary": "A route-level strategy.",
        "preconditions": ["In scope."],
        "target_stack": ["Web applications"],
        "payload_template": None,
        "verification_method": ["Compare control against candidate."],
        "known_failures": ["Fails behind a WAF."],
        "steps": ["Do the first thing."],
        "provenance": {
            "source_type": source_type,
            "source_ref": source_ref,
            "source_url": None,
            "notes": None,
        },
        "published_at": published,
        "trust_level": "curated",
    }


def _write(root, entries) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "corpus.jsonl").write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
        encoding="utf-8",
    )


def test_same_day_learned_entries_are_not_reported_as_placeholders(tmp_path) -> None:
    """Several runs finishing on one day is normal; that must not look suspicious."""

    _write(
        tmp_path,
        [
            _entry(
                f"learned-run-{index}",
                published="2026-03-04",
                source_type="learned",
                source_ref=f"VeriSure learned skill learned-run-{index} (source run r{index})",
            )
            for index in range(4)
        ]
        + [_entry("curated-a", published="2020-12-03", source_type="curated_reference", source_ref="OWASP WSTG v4.2: A")],
    )

    report = audit_published_at(tmp_path)

    assert report["suspected_placeholder_dates"] == {}
    assert report["suspected_placeholder_count"] == 0
    assert report["time_split_usable"] is True
    assert report["learned_count"] == 4
    assert report["learned_date_distribution"] == {"2026-03-04": 4}
    assert report["total"] == 5


def test_a_same_day_curated_group_is_still_suspected(tmp_path) -> None:
    """The carve-out must not weaken the rule for hand-cited entries."""

    _write(
        tmp_path,
        [
            _entry(f"a{index}", published="2026-03-04", source_type="curated_reference", source_ref=f"Doc {index}")
            for index in range(3)
        ],
    )

    report = audit_published_at(tmp_path)

    assert report["suspected_placeholder_count"] == 3
    assert report["time_split_usable"] is False
    assert report["learned_count"] == 0
