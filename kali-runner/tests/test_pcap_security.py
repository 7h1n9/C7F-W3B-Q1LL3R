from pathlib import Path

import pytest

from app.config import settings
from app.executors.pcap_executor import _pcap_path
from app.models import JobRequest
from app.workspace.paths import initialize_workspace


def test_pcap_tool_rejects_path_outside_attachments(tmp_path: Path) -> None:
    settings.workspace_root = tmp_path
    workspace = initialize_workspace("pcap-outside")
    (workspace / "outputs" / "sample.pcap").write_bytes(b"\xd4\xc3\xb2\xa1")
    with pytest.raises(Exception) as error:
        _pcap_path(JobRequest(run_id="pcap-outside", tool="pcap_metadata", arguments={"path": "outputs/sample.pcap"}))
    assert getattr(error.value, "status_code", None) == 403


def test_pcap_tool_rejects_invalid_magic(tmp_path: Path) -> None:
    settings.workspace_root = tmp_path
    workspace = initialize_workspace("pcap-invalid")
    (workspace / "attachments" / "sample.pcap").write_bytes(b"not-a-pcap")
    with pytest.raises(Exception) as error:
        _pcap_path(JobRequest(run_id="pcap-invalid", tool="pcap_metadata", arguments={"path": "attachments/sample.pcap"}))
    assert getattr(error.value, "status_code", None) == 422
