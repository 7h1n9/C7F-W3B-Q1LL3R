import json
import shutil
from pathlib import Path

from app.core.config import get_settings
from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.services.methodology_hints import hint_text, hints_for_challenge
from app.services.known_exploit import (
    FIRST_MOVE_DISCIPLINE,
    RECON_DISCIPLINE,
    known_exploit_block,
)

RUN_POLICY = """# Run Policy\n\nOnly interact with the challenge target hosts recorded in challenge.json. Do not use arbitrary shell commands, public targets, persistence, or paths outside this workspace. Record evidence for material conclusions.\n"""


#: Environment summary prepended to ``CLAUDE.md``.
#:
#: ``CLAUDE.md`` is seeded by the worker image's ``runtime-agent``, which writes
#: only a generic tool/knowledge-base description -- it never sees the per-run
#: disciplines the backend writes into ``AGENTS.md``.  Measured 2026-11-12 on
#: ``94af75e5`` (Peizheng Group): ``AGENTS.md`` carried the "report findings while
#: you work" rule, ``CLAUDE.md`` (1825 chars) carried none of it, and the
#: claude-cli worker that did 454 KB of the actual work emitted **0**
#: ``VERIFIED_FACT`` markers while its codex counterpart emitted 14.  Every fact
#: therefore landed at Finalize instead of steering the next turn.
#:
#: Retained here rather than read from the image because the image's copy is
#: unreachable from the backend.  Keep it short: it exists to stop the Worker
#: assuming a tool is missing, not to duplicate the full track matrix.
CLAUDE_ENV_PREAMBLE = (
    "\u4f60\u5728\u4e00\u4e2a Kali Linux \u5bb9\u5668\u91cc\uff0c\u5e38\u89c1\u5b89\u5168/CTF \u5de5\u5177\u4e0e\u79bb\u7ebf\u77e5\u8bc6\u5e93\u9f50\u5168\u3002"
    "\u5f53\u524d\u76ee\u5f55\u662f\u672c\u9898\u7684\u5de5\u4f5c\u7a7a\u95f4\uff08\u811a\u672c\u3001\u4ea7\u7269\u3001\u626b\u63cf\u7ed3\u679c\u90fd\u653e\u8fd9\u91cc\uff0c"
    "\u4f1a\u88ab\u4fdd\u7559\u5e76\u4e0e\u534f\u4f5c worker \u5171\u4eab\uff09\u3002\u51ed\u636e\u5df2\u6309\u5f15\u64ce\u6ce8\u5165\u5230\u73af\u5883\u3002"
    "\u4f60\u662f kali \u7528\u6237\uff0c\u6709 NOPASSWD sudo\u3002\u4e0d\u786e\u5b9a\u67d0\u5de5\u5177\u5728\u4e0d\u5728\uff0c"
    "\u5148 `which <x>` \u6216 `<x> --help`\uff0c\u522b\u5047\u8bbe\u6ca1\u6709\u3002\n\n"
    "\u79bb\u7ebf\u77e5\u8bc6\u5e93\uff08\u65e0\u7f51\u4e5f\u80fd\u67e5\uff0c\u4f18\u5148\u67e5\u672c\u5730\u518d\u4e0a\u7f51\uff09\uff1a\n"
    "- \u624b\u6cd5 / payload\uff1a`/home/kali/knowledges/PayloadsAllTheThings`\u3001"
    "`/home/kali/knowledges/InternalAllTheThings`\n"
    "- \u6280\u6218\u6cd5 wiki\uff1a`/home/kali/knowledges/hacktricks`\n"
    "- CVE \u590d\u73b0 / PoC\uff1a`/home/kali/pocs/vulhub`\u3001`/home/kali/pocs/Awesome-POC`\n\n"
    "\u4ee5\u4e0b\u4e3a\u672c\u6b21\u8fd0\u884c\u7684\u6267\u884c\u7eaa\u5f8b\u4e0e\u672c\u9898\u60c5\u62a5\uff08\u4e0e AGENTS.md \u540c\u6e90\uff09\uff1a\n\n"
)

#: Marks a ``CLAUDE.md`` this module has already written, so a reused workspace
#: refreshes the body instead of stacking a second preamble.
CLAUDE_SYNC_MARKER = "\u4ee5\u4e0b\u4e3a\u672c\u6b21\u8fd0\u884c\u7684\u6267\u884c\u7eaa\u5f8b\u4e0e\u672c\u9898\u60c5\u62a5"


def _sync_claude_md(workspace: Path, agents_md: str) -> None:
    """Mirror the run's disciplines into ``CLAUDE.md`` for claude-cli workers.

    Claude Code reads ``CLAUDE.md`` from the working directory; ``AGENTS.md`` is
    Codex's convention.  The backend writes the run policy, reconnaissance
    discipline, hints and the matched recipe into ``AGENTS.md`` only, so a
    claude-cli worker never received any of it -- measured 2026-11-12, run
    ``94af75e5``: ``CLAUDE.md`` was 1825 chars of generic environment text with
    zero mention of ``VERIFIED_FACT``, that worker did 454 KB of the run's actual
    work, and it published no facts until the final summary.  Its codex peer in
    the same Run, which does read ``AGENTS.md``, emitted 14 markers.

    The image's ``runtime-agent`` seeds ``CLAUDE.md`` but **does not overwrite an
    existing file** -- verified by mounting a pre-written ``CLAUDE.md`` into the
    worker image and observing the marker survive -- so this write wins, and the
    environment summary above is prepended because the image's copy would
    otherwise be lost.

    Idempotent on a reused workspace: when the marker is already present the
    existing preamble is kept and only the body is replaced.
    """

    target = workspace / "CLAUDE.md"
    try:
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        if CLAUDE_SYNC_MARKER in existing:
            head = existing.split(CLAUDE_SYNC_MARKER, 1)[0] + CLAUDE_SYNC_MARKER + "\uff1a\n\n"
        else:
            head = CLAUDE_ENV_PREAMBLE
        target.write_text(f"{head}{agents_md}", encoding="utf-8")
    except OSError:
        # A read-only or concurrently-written workspace must not fail creation:
        # the Run can still proceed on AGENTS.md alone (Codex), and the symptom is
        # the status quo this change improves, not a new outage.
        return


#: Public alias: the Muteki runtime seeds its own ``AGENTS.md`` under
#: ``<workspace>/muteki`` and calls this to mirror it, rather than importing a
#: private name across modules.
sync_claude_md = _sync_claude_md


def create_workspace(
    run_id: str, challenge: Challenge, attachments: list[object] | None = None
) -> Path:
    root = get_settings().workspace_root.resolve()
    workspace = (root / run_id).resolve()
    if workspace.parent != root:
        raise DomainError("WORKSPACE_INVALID", "Invalid run workspace path.")
    for child in (
        "source",
        "attachments",
        "requests",
        "responses",
        "scripts",
        "evidence",
        "outputs",
        "final",
        "notes",
        "scratch",
        "payloads",
        "extracted",
        "generated",
        "runtime",
        "archive",
    ):
        (workspace / child).mkdir(parents=True, exist_ok=True)
    for runtime_group in (
        "agents",
        "web-research",
        "tool-subrequests",
        "streams",
        "runner-jobs",
        "pending-promotion",
        "cleanup-manifests",
    ):
        (workspace / "runtime" / runtime_group).mkdir(parents=True, exist_ok=True)
    manifest = []
    repository_root = Path(__file__).resolve().parents[3]
    attachment_root = (repository_root / "data" / "challenges" / challenge.id / "attachments").resolve()
    for item in attachments or []:
        raw_source = repository_root / item.relative_path
        source = raw_source.resolve()
        target = workspace / "attachments" / item.stored_name
        if source.is_file() and source.parent == attachment_root and not raw_source.is_symlink():
            shutil.copyfile(source, target)
            manifest.append(
                {
                    "path": f"attachments/{item.stored_name}",
                    "sha256": item.sha256,
                    "size": item.size,
                    "primary": item.is_primary,
                }
            )
    primary = next((entry["path"] for entry in manifest if entry["primary"]), None)
    payload = {
        "id": challenge.id,
        "name": challenge.name,
        "challenge_type": challenge.challenge_type,
        "target_url": challenge.target_url,
        "allowed_hosts": challenge.allowed_hosts,
        "flag_pattern": challenge.flag_pattern,
        "primary_attachment": primary,
        "attachments": manifest,
        "metadata": challenge.metadata_json,
    }
    (workspace / "challenge.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # The production Worker is a CLI agent (Codex / Claude Code) whose prompt is
    # assembled by vendored upstream code.  ``AGENTS.md`` is the one file both
    # CLIs read on every turn, so the curated CVE recipe has to live here to
    # reach the Worker at all: a bare `Brief: https://github.com/vulhub/...`
    # left the Worker doing generic header/robots recon for the whole round
    # because it did not know the exploit path existed.
    # Order matters: the reconnaissance discipline is the only block that has to
    # be read before the Worker acts, and Workers read ``AGENTS.md`` with
    # ``| head -50``.  Everything below it is reference material.  Measured
    # 2026-10-29 (``c88aaf79``): without this block the Worker spent its first
    # commands inventing filenames and re-deriving facts it had already recorded.
    # ``getattr`` with a default rather than attribute access: settings objects
    # in tests are frequently lightweight stand-ins built before this flag
    # existed, and a measuring toggle must not be able to crash workspace
    # creation -- that would turn an experiment into an outage.  The default
    # mirrors ``Settings.first_move_discipline``.
    include_first_move = bool(getattr(get_settings(), "first_move_discipline", True))
    agents_md = (
        RUN_POLICY
        + (FIRST_MOVE_DISCIPLINE if include_first_move else "")
        + RECON_DISCIPLINE
        + "\n"
        + hint_text(hints_for_challenge(challenge))
        + known_exploit_block(challenge)
    )
    (workspace / "AGENTS.md").write_text(agents_md, encoding="utf-8")
    # Claude Code never reads AGENTS.md; without this it runs with only the
    # image's generic environment text.  See ``_sync_claude_md``.
    _sync_claude_md(workspace, agents_md)
    return workspace
