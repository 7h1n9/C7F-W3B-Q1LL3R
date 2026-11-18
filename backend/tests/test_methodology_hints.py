"""Methodology hints: the challenge's own ground truth must reach the Worker.

The regression this file pins down: a benchmark case stores ``initial_knowledge``
and ``attack_surface`` on its Challenge, but only a hand-written hint set keyed on
one challenge name was rendered into ``AGENTS.md``.  Every other Run therefore
started with an empty "Authorized Methodology Hints" section and blind-browsed
the target -- Run ``e087202c`` spent minutes on ``/search``, ``/track`` and
``/lookup`` (all 404) while the case file already named
``POST /api/warranty/check`` and the injectable ``department`` parameter.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.methodology_hints import hint_text, hints_for_challenge


def _challenge(**overrides: object) -> SimpleNamespace:
    values = {
        "name": "benchmark:sql-injection-golden",
        "target_url": "http://127.0.0.1:8383",
        "metadata_json": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_a_declared_attack_surface_becomes_a_hint() -> None:
    challenge = _challenge(
        metadata_json={
            "benchmark_case_id": "sql-injection-golden",
            "initial_knowledge": ["The range is a deterministic local service."],
            "attack_surface": [
                {
                    "method": "POST",
                    "endpoint": "/api/warranty/check",
                    "parameter": "department",
                    "role": "input",
                }
            ],
        }
    )
    hints = hints_for_challenge(challenge)
    assert hints["id"] == "declared:sql-injection-golden"
    rendered = "\n".join(hints["hints"])
    assert "deterministic local service" in rendered
    assert "POST /api/warranty/check" in rendered
    assert "parameter: department" in rendered
    assert hints["constraints"]["endpoint"] == "/api/warranty/check"
    assert hints["constraints"]["allowed_inputs"] == ["department"]


def test_the_hints_reach_the_agents_md_section() -> None:
    challenge = _challenge(
        metadata_json={
            "initial_knowledge": ["department is concatenated into the SQL string."],
            "attack_surface": [
                {"method": "POST", "endpoint": "/api/warranty/check", "parameter": "department"}
            ],
        }
    )
    text = hint_text(hints_for_challenge(challenge))
    assert "## Authorized Methodology Hints" in text
    assert "department is concatenated" in text
    assert "POST /api/warranty/check" in text


def test_a_challenge_that_declares_nothing_keeps_the_empty_set() -> None:
    hints = hints_for_challenge(_challenge())
    assert hints == {
        "id": "none",
        "challenge": "benchmark:sql-injection-golden",
        "hints": [],
        "constraints": {},
    }


def test_the_curated_hint_set_still_wins() -> None:
    challenge = _challenge(
        name="资产保修核验平台",
        metadata_json={"initial_knowledge": ["declared line"]},
    )
    hints = hints_for_challenge(challenge)
    assert hints["id"] == "asset-warranty-h1-h3"
    assert "declared line" not in "\n".join(hints["hints"])


def test_malformed_metadata_does_not_raise() -> None:
    for metadata in (None, [], "nope", {"attack_surface": "POST /x"}, {"initial_knowledge": [None, "  "]}):
        hints = hints_for_challenge(_challenge(metadata_json=metadata))
        assert hints["id"] == "none"
