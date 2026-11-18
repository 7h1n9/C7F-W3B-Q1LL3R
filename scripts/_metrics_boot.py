"""One-line provenance hook for scripts that print numbers.

``app.services.metrics_guard`` can already record what a query measured, but
until now nothing called it, so every analysis run left no trace.  This module
is the thin adapter an analysis script imports to fix that with a single call
at the end of ``main()``:

    record_analysis(
        "analyze_truncation_points",
        "transitions jsonl: data/training/transitions-...jsonl | limit=无",
        {"unsolved": {...}, "solved": {...}},
    )

Two deliberate asymmetries with the guard itself:

* **An empty ``sql_or_source`` raises.**  That is a bug in the calling script
  -- the row could never be recomputed -- so it must be loud, not a warning.
* **A failed write only warns.**  Provenance is evidence, not a precondition:
  a full disk must not cost you the analysis you just spent minutes producing.
  The row is lost, the numbers are not, and the warning names both.

The default log path is anchored to the repository root rather than the current
directory.  ``metrics_guard.DEFAULT_SNAPSHOT_LOG`` is repo-relative, so a script
launched from ``backend/`` would quietly write ``backend/data/logs/...`` while
every reader looks in ``data/logs/`` -- the same class of "the number exists but
nobody can find it" problem this guard exists to prevent.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]

#: ``metrics_guard`` lives under ``backend/``; analysis scripts are launched
#: from the repo root and do not otherwise need backend on ``sys.path``.
_BACKEND = ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services.metrics_guard import snapshot  # noqa: E402

#: Absolute, CWD-independent location of the snapshot log.
SNAPSHOT_LOG = ROOT / "data" / "logs" / "metrics-snapshots.jsonl"


def _guess_code_path(script: str) -> Path | None:
    """The code whose mtime dates these numbers: the script, else ``__main__``."""

    name = Path(script).name
    candidate = Path(__file__).with_name(name if name.endswith(".py") else f"{name}.py")
    if candidate.is_file():
        return candidate
    main_file = getattr(sys.modules.get("__main__"), "__file__", None)
    if main_file and Path(main_file).is_file():
        return Path(main_file)
    return None


def record_analysis(
    script: str,
    sql_or_source: str,
    result: dict[str, Any],
    *,
    filters: Mapping[str, Any] | None = None,
    data_window: Any = "all",
    code_path: str | Path | None = None,
    path: str | Path | None = None,
) -> None:
    """Append one provenance row for a finished analysis; never abort the caller.

    ``script`` becomes the prefix of ``query_id`` so a row can be traced back to
    the script that produced it without opening ``result``.

    ``path`` is exposed for tests and for replaying into a scratch log; it
    defaults to :data:`SNAPSHOT_LOG`.
    """

    if not str(sql_or_source or "").strip():
        raise ValueError(
            "record_analysis 必须给出 sql_or_source（SQL 语句或数据来源），"
            "否则这条数字无法重算；这是调用方的 bug，不是可降级的错误"
        )

    target = Path(path) if path is not None else SNAPSHOT_LOG
    try:
        snapshot(
            sql_or_source=sql_or_source,
            result=result,
            filters=filters,
            data_window=data_window,
            code_path=code_path if code_path is not None else _guess_code_path(script),
            query_id=f"{Path(script).stem}-{uuid.uuid4().hex[:8]}",
            path=target,
        )
    except (OSError, ValueError) as exc:
        print(
            f"[metrics] {script}: 快照未写入 {target}（本次出数不受影响）: {exc}",
            file=sys.stderr,
        )
