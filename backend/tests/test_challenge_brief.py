"""The Challenge's declared surface must reach both model boundaries.

The regression this file pins down: ``initial_knowledge`` / ``attack_surface``
used to be rendered only into the workspace ``AGENTS.md``, which is read by the
CLI Worker and by nothing else.  Run ``e3391674`` recorded the cost in its own
``muteki.intent_proposed`` event: the Reason model chose the correct technique
("boolean/LIKE injection oracle ... tautology/contradiction pairs") and then
guessed the parameters as ``serial`` / ``warranty_id`` / ``product_id``, while
the case file had named ``department`` all along.

The declarations are kept in their own channel rather than merged into the
retrieved ``knowledge_prior``: a prior is a hypothesis and is rendered under an
"unverified" heading, and telling a model to distrust the endpoint name the
Challenge declared defeats the point of sending it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.methodology_hints import brief_lines
from app.solver.muteki.adapter.reason_model import CoordinatorReasonModel
from app.solver.muteki.worker.official_worker import _parse_challenge_brief
from app.solver.muteki.worker.openai_compatible import OpenAICompatibleWorker
from muteki.core.cost import CostController


class _Graph:
    def snapshot(self):
        return SimpleNamespace(evidence=[])


def _challenge(**metadata: object) -> SimpleNamespace:
    return SimpleNamespace(
        name="benchmark:sql-injection-golden",
        target_url="http://127.0.0.1:8383",
        metadata_json=dict(metadata),
    )


def _declared() -> SimpleNamespace:
    return _challenge(
        initial_knowledge=["department is concatenated into the SQL string."],
        attack_surface=[
            {
                "method": "POST",
                "endpoint": "/api/warranty/check",
                "parameter": "department",
            }
        ],
    )


def _worker(tmp_path: Path, brief: list[str]) -> OpenAICompatibleWorker:
    return OpenAICompatibleWorker(
        graph=_Graph(),
        container=object(),
        workspace=str(tmp_path),
        worker_id="worker-brief",
        intent_id="intent-brief",
        target_url="http://target.test",
        base_url="https://provider.test/v1",
        api_key="secret",
        model="deepseek-v4-flash",
        max_turns=2,
        timeout_seconds=10,
        cost=CostController(),
        run_id="run-brief",
        challenge_brief=brief,
    )


# --- the declaration itself ------------------------------------------------


def test_brief_lines_render_declared_knowledge_and_surface() -> None:
    lines = brief_lines(_declared())
    joined = "\n".join(lines)
    assert "department is concatenated into the SQL string." in joined
    assert "POST /api/warranty/check" in joined
    assert "parameter: department" in joined
    assert "Declared entry point: POST /api/warranty/check" in joined


def test_a_challenge_that_declares_nothing_yields_no_lines() -> None:
    assert brief_lines(_challenge()) == []


def test_curated_hints_are_reused_when_they_match() -> None:
    curated = SimpleNamespace(
        name="资产保修核验平台",
        target_url="http://127.0.0.1:28319",
        metadata_json={},
    )
    lines = brief_lines(curated)
    assert any("H1" in line for line in lines)
    # The curated set names the fixed control values; those must survive the
    # trip into a prompt, not just into AGENTS.md.
    assert any("asset_no=PC-2026-013" in line for line in lines)


# --- A path (chat_completions) ---------------------------------------------


def test_worker_prompt_carries_the_declared_surface(tmp_path: Path) -> None:
    brief = brief_lines(_declared())
    prompt = json.loads(_worker(tmp_path, brief)._initial_prompt("probe the endpoint", {}))
    assert prompt["challenge_declared_surface"] == brief
    assert "parameter: department" in "\n".join(prompt["challenge_declared_surface"])


def test_the_declared_surface_is_absent_when_nothing_was_declared(tmp_path: Path) -> None:
    prompt = json.loads(_worker(tmp_path, [])._initial_prompt("probe", {}))
    assert prompt["challenge_declared_surface"] == []


# --- the profile -> worker handoff -----------------------------------------


def test_the_brief_round_trips_through_the_profile_environment() -> None:
    brief = ["POST /api/warranty/check (parameter: department)", "asset_no=PC-2026-013"]
    encoded = json.dumps(brief, ensure_ascii=False)
    assert _parse_challenge_brief(encoded) == brief


def test_an_unreadable_brief_degrades_to_none_instead_of_failing() -> None:
    for raw in (None, "", "   ", "not json", '{"a": 1}', "[]"):
        assert _parse_challenge_brief(raw) == []


# --- Reason boundary -------------------------------------------------------


def test_reason_prompt_labels_the_brief_as_declared_not_retrieved() -> None:
    model = CoordinatorReasonModel(
        engine=object(),
        model_config_id="cfg",
        model_name="deepseek-v4-flash",
        challenge_brief=["Declared entry point: POST /api/warranty/check"],
        knowledge_prior="### prior item\nold technique",
    )
    assert model.challenge_brief == ["Declared entry point: POST /api/warranty/check"]
    # Rendered before the prior and under its own heading, so the model can
    # tell the Challenge's statement apart from an unverified hypothesis.
    assert model.challenge_brief[0].startswith("Declared entry point")
