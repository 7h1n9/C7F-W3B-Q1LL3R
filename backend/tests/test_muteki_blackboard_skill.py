from __future__ import annotations

import os
import subprocess
import sys

from app.solver.muteki.graph import MutekiGraph


def test_skill_read_facts_empty(tmp_path):
    db_path = tmp_path / "shared_graph.db"
    graph = MutekiGraph(db_path, challenge_id="test-run")
    graph.close()

    env = {
        "MUTEKI_BLACKBOARD_DB": str(db_path),
        "MUTEKI_CHALLENGE_ID": "test-run",
        "MUTEKI_WORKER_ID": "test-worker",
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.solver.muteki.skill.blackboard", "read-facts"],
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__) or ".", "..")),
        env={**os.environ, **env},
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "no facts" in result.stdout or "(no facts" in result.stdout


def test_skill_write_fact_and_read(tmp_path):
    db_path = tmp_path / "shared_graph.db"
    graph = MutekiGraph(db_path, challenge_id="test-run")
    graph.close()

    env = {
        "MUTEKI_BLACKBOARD_DB": str(db_path),
        "MUTEKI_CHALLENGE_ID": "test-run",
        "MUTEKI_WORKER_ID": "test-worker",
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.solver.muteki.skill.blackboard",
         "write-fact", "port 80 is open", "--verified"],
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__) or ".", "..")),
        env={**os.environ, **env},
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "verified fact" in result.stdout

    result = subprocess.run(
        [sys.executable, "-m", "app.solver.muteki.skill.blackboard", "read-facts"],
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__) or ".", "..")),
        env={**os.environ, **env},
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "port 80 is open" in result.stdout


def test_skill_mark_deadend(tmp_path):
    db_path = tmp_path / "shared_graph.db"
    graph = MutekiGraph(db_path, challenge_id="test-run")
    graph.close()

    env = {
        "MUTEKI_BLACKBOARD_DB": str(db_path),
        "MUTEKI_CHALLENGE_ID": "test-run",
        "MUTEKI_WORKER_ID": "test-worker",
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.solver.muteki.skill.blackboard",
         "mark-deadend", "sql injection not possible"],
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__) or ".", "..")),
        env={**os.environ, **env},
        capture_output=True, text=True,
    )
    assert result.returncode == 0

    result = subprocess.run(
        [sys.executable, "-m", "app.solver.muteki.skill.blackboard", "read-deadends"],
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__) or ".", "..")),
        env={**os.environ, **env},
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "sql injection not possible" in result.stdout


def test_skill_list_intents(tmp_path):
    db_path = tmp_path / "shared_graph.db"
    graph = MutekiGraph(db_path, challenge_id="test-run")
    graph.propose_intent(actor="coordinator", description="explore endpoint")
    graph.close()

    env = {
        "MUTEKI_BLACKBOARD_DB": str(db_path),
        "MUTEKI_CHALLENGE_ID": "test-run",
        "MUTEKI_WORKER_ID": "test-worker",
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.solver.muteki.skill.blackboard", "list-intents"],
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__) or ".", "..")),
        env={**os.environ, **env},
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "explore endpoint" in result.stdout


def test_skill_claim_intent(tmp_path):
    db_path = tmp_path / "shared_graph.db"
    graph = MutekiGraph(db_path, challenge_id="test-run")
    intent_id = graph.propose_intent(actor="coordinator", description="test intent")
    graph.close()

    env = {
        "MUTEKI_BLACKBOARD_DB": str(db_path),
        "MUTEKI_CHALLENGE_ID": "test-run",
        "MUTEKI_WORKER_ID": "test-worker",
        "MUTEKI_INTENT_ID": intent_id,
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.solver.muteki.skill.blackboard", "claim", intent_id],
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__) or ".", "..")),
        env={**os.environ, **env},
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "WON" in result.stdout
