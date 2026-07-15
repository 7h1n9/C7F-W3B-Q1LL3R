from pathlib import Path

import pytest

from app.config import settings
from app.executors.script_executor import script_run
from app.models import JobRequest
from app.workspace.paths import initialize_workspace


@pytest.mark.asyncio
async def test_script_run_syncs_interpreter_result_and_generated_files(tmp_path: Path) -> None:
    settings.workspace_root = tmp_path
    settings.max_output_bytes = 4096
    workspace = initialize_workspace("script-test")
    script = workspace / "scripts" / "solve.py"
    script.write_text("from pathlib import Path\nPath('outputs/generated').mkdir(parents=True, exist_ok=True)\nPath('outputs/generated/flag.txt').write_text('flag{test}')\nprint('ok')\n", encoding="utf-8")
    result = await script_run(JobRequest(run_id="script-test", allowed_hosts=["challenge.local"], tool="script_run", arguments={"path": "scripts/solve.py", "interpreter": "python", "network_mode": "none"}))
    assert result["exit_code"] == 0
    assert result["stdout_excerpt"].strip() == "ok"
    assert result["generated_files"][0]["path"] == "outputs/generated/flag.txt"


@pytest.mark.asyncio
async def test_script_run_rejects_scripts_outside_allowed_directories(tmp_path: Path) -> None:
    settings.workspace_root = tmp_path
    initialize_workspace("script-boundary")
    with pytest.raises(Exception, match="scripts/|scratch/scripts"):
        await script_run(JobRequest(run_id="script-boundary", tool="script_run", arguments={"path": "attachments/solve.py", "interpreter": "python"}))
