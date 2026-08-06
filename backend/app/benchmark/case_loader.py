"""Load immutable benchmark case definitions from JSON fixtures."""

import json
from pathlib import Path

from app.benchmark.case_definition import BenchmarkCase

DEFAULT_CASE_DIR = Path(__file__).resolve().parent / "cases"


def load_case(case_id: str, case_dir: str | Path | None = None) -> BenchmarkCase:
    directory = Path(case_dir) if case_dir is not None else DEFAULT_CASE_DIR
    path = directory / f"{case_id}.json"
    if path.is_file():
        return BenchmarkCase.model_validate(json.loads(path.read_text(encoding="utf-8")))
    for candidate in directory.glob("*.json"):
        data = json.loads(candidate.read_text(encoding="utf-8"))
        if str(data.get("case_id") or "") == case_id:
            return BenchmarkCase.model_validate(data)
    raise FileNotFoundError(f"Benchmark case not found: {case_id}")


def load_cases(case_dir: str | Path | None = None) -> list[BenchmarkCase]:
    directory = Path(case_dir) if case_dir is not None else DEFAULT_CASE_DIR
    return [
        BenchmarkCase.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(directory.glob("*.json"))
    ]
