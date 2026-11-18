"""Turn this project's own run history into box-indexed playbook entries.

Why this exists
---------------
The arena gives a Worker almost nothing to match on: the title is a CTF problem
name and the description is literally "点击启动靶机。".  Measured 2026-10-29 on the
11 runs of that night, the matcher hit **0/11** -- the CVE axis needs an id, the
product axis needs a product word, and `[CISCN2019 华东北赛区]Web6` has neither.

The only axis that can reach those titles is `box_names`, i.e. "this recipe is
about that exact box".  And the only source of box-indexed knowledge this
project owns is its own run history:

* ``templates/web__*.yaml``      -- paths that produced an accepted flag
* ``templates/negative__*.yaml`` -- routes a run proved do not work
* ``run_events`` dead_end / fact -- what each round actually established

So the input is the history, the output is ``data/knowledge/learned-boxes.jsonl``
which the playbook loader picks up as an extra corpus.

Two hard rules carried over from the existing design
----------------------------------------------------
* A negative entry never commands.  It carries no steps to run, only walls that
  were already hit, so a re-run does not pay for the same discovery twice.
* Nothing here is invented.  Every line is quoted from a recorded event or a
  distilled template, and ``provenance`` names the run it came from.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "data" / "knowledge" / "templates"
# Deliberately *not* a direct child of ``data/knowledge``: everything flat in
# that directory is loaded as a ``KnowledgeItem``, and that schema requires every
# entry to carry at least one executable step.  A wall entry must carry none --
# a list of settled routes is not an instruction.  Keeping the corpus one level
# down satisfies both: the playbook loader reads it by explicit path, and the
# knowledge loader (which globs the flat directory only) never sees it.
OUTPUT = ROOT / "data" / "knowledge" / "playbooks" / "learned-boxes.jsonl"
CONTAINER = "c7f-w3b-q1ll3r-mysql-1"
SINCE = "2026-10-20"
#: Written into ``published_at``.  A generated corpus still benefits the audit,
#: which reports on entries whose provenance cannot be dated at all.
GENERATED_ON = "2026-10-29"

# Per-entry ceilings.  Two entries already compete for a 3500-char block, and
# the goal that carries FIRST ACTION is capped at 600 chars, so a long list is
# strictly worse than a short one: the tail gets cut anyway.
MAX_STEPS = 4
MAX_WALLS = 4
STEP_CHARS = 200

#: Sentences that carry no information about *this* box.  The coordinator
#: markers are round bookkeeping, and the "completed a bounded observation" line
#: is the distiller's placeholder for "a step happened" -- quoting them into a
#: recipe would read like a finding while saying nothing.
BOILERPLATE = (
    "native worker completed a bounded evidence-backed observation",
    "round_budget_exhausted_before_reason",
    "race_worker_timeout",
    # Engine-slot failures, not findings about the box: quoting them would tell
    # the next Worker a route is dead when in fact nobody ever tried it.
    "no_healthy_worker_profile",
    "worker_healthcheck",
    "need_kind=",
    "need_input=",
    "need_info=",
    "dead ends ruled out:",
)

#: Markers that make a sentence worth quoting: a concrete endpoint, path, status
#: code, version or flag trial.  Used only to *rank*, never to discard -- a fact
#: with none of them is still kept if there is room.
CONCRETE = re.compile(
    r"`[^`]+`|/[a-z0-9_.-]{2,}|HTTP\s*\d{3}|\b\d{3}\b|\b\d+\.\d+(\.\d+)?\b|"
    r"\bCVE-\d{4}-\d+|404|500|403|401",
    re.IGNORECASE,
)

_ACTOR_PREFIX = re.compile(r"^\[(?:claude|codex|worker-\d+)\]\s*", re.IGNORECASE)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

#: What makes a recorded step usable as the FIRST ACTION.  That slot is the one
#: instruction that survives the goal's character budget, so it has to be a move,
#: not an observation: the first step of a solved Run is frequently "the endpoint
#: accepts JSON {a1,a2,a3}" while the request that returns the flag comes later.
_ACTIONABLE = re.compile(
    r"^(?:GET|POST|PUT|PATCH|DELETE|curl|sqlmap|nmap|nc|python3?|bash|sh|php)\b|"
    r"`(?:GET|POST|PUT|PATCH|DELETE|curl|sqlmap|python3?|bash|cat|php)\b|"
    r"\bcurl\b|--data|--cookie|-X\s+POST",
    re.IGNORECASE,
)

#: Workers label facts they consider settled (``VERIFIED_FACT=...``), so the same
#: sentence arrives twice -- once raw and once tagged.  The tag is stripped before
#: de-duplication, otherwise a recipe spends two of its four lines on one finding.
_FACT_TAG = re.compile(r"^(?:VERIFIED_FACT|VERIFIED|FACT|CONFIRMED)\s*=\s*", re.IGNORECASE)

#: A round's own bookkeeping ("all findings are on the board (facts 8, 11, ...)",
#: "No FOUND_FLAG", "dead ends 9, 10 ...").  It reports on the process, not on the
#: target, so quoting it into a recipe teaches the next Worker nothing.
_META_SUMMARY = re.compile(
    r"all findings are on the board|no found_flag|^\W*(?:facts?|dead ends?|summary)\s*\d|"
    r"\bfacts?\s+\d+\s*[,;]\s*\d",
    re.IGNORECASE,
)

#: Text that is a *plan*, not a result: "the most promising unexplored vector is
#: X", "X has not been tested yet".  Filing that under "do not waste the round on"
#: would tell the next Worker to skip the one thing worth trying.  Also dropped:
#: lines that blame this side's plumbing (sandbox connection trouble, a killed
#: background job) rather than a property of the target.
_NOT_A_WALL = re.compile(
    r"most promising|unexplored|not (?:yet )?(?:been )?tested|has(?:n't| not) been tested|"
    r"would be worth|remains to be|next step should|"
    r"connection issues?|connection (?:was )?(?:refused|reset)|"
    r"background jobs? (?:kept|was) (?:getting )?kill|preventing reliable testing",
    re.IGNORECASE,
)


def mysql_json(sql: str) -> list[dict]:
    """Run one query and parse it as JSON lines.

    ``JSON_OBJECT`` is used rather than tab-separated output because event text
    contains newlines; JSON escapes them, so one event stays on one line and a
    free-text column can never shift the columns.
    """

    proc = subprocess.run(
        [
            "docker", "exec", CONTAINER,
            "mysql", "--default-character-set=utf8mb4",
            "-uctf_agent", "-pctf_agent", "ctf_agent",
            # ``-r`` (raw) is load-bearing.  Without it the batch client
            # re-escapes every backslash, so a stored `\"` comes back as `\\"`
            # and the line is no longer valid JSON.  Measured on the sibling
            # exporter: 421 of 526 lines failed to parse, and the loop below
            # silently dropped them -- the corpus quietly lost 80% of its
            # command text while the script reported success.
            "-N", "-B", "-r", "-e", sql,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"mysql failed: {proc.stderr[:400]}")
    rows: list[dict] = []
    dropped = 0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Warning"):
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            dropped += 1
            continue
    # A silent drop is indistinguishable from "this event had nothing to say",
    # which is exactly how the escaping bug stayed invisible.  Any drop is now
    # reported, and a large fraction aborts: a corpus built from a third of the
    # history would look complete while being wrong.
    if dropped:
        total = dropped + len(rows)
        print(f"warning: dropped {dropped}/{total} unparseable rows from this query")
        if total and dropped > total * 0.1:
            raise SystemExit(
                f"refusing to build from a partial read: {dropped}/{total} rows unparseable"
            )
    return rows


def clean(text: str) -> str | None:
    """Strip narration wrappers from one recorded sentence, or drop it."""

    value = str(text or "").strip()
    if not value:
        return None
    value = _ACTOR_PREFIX.sub("", value)
    value = _FACT_TAG.sub("", value)
    # A tool result serialised into a step is evidence for the distiller, not an
    # instruction for the next Worker.
    if value.startswith("{"):
        return None
    lowered = value.casefold()
    if any(marker in lowered for marker in BOILERPLATE):
        return None
    if _META_SUMMARY.search(value):
        return None
    if _NOT_A_WALL.search(value):
        return None
    value = " ".join(value.split())
    if len(value) < 30:
        return None
    return value[:STEP_CHARS].strip()


def rank(lines: list[str]) -> list[str]:
    """Most concrete first, duplicates removed, preserving original order."""

    seen: set[str] = set()
    scored: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        key = re.sub(r"\s+", " ", line.casefold())[:120]
        if key in seen:
            continue
        seen.add(key)
        scored.append((-len(CONCRETE.findall(line)), index, line))
    scored.sort()
    return [line for _, _, line in scored]


def load_templates() -> tuple[dict[str, dict], list[dict]]:
    """(challenge_id -> case template), [negative templates] from disk."""

    try:
        import yaml
    except ImportError:  # pragma: no cover - environment guard
        raise SystemExit("PyYAML is required to read the template store")

    cases: dict[str, dict] = {}
    negatives: list[dict] = []
    for path in sorted(TEMPLATES.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as error:  # noqa: BLE001 - one bad file must not stop the rest
            print(f"skip {path.name}: {error}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            continue
        match = _UUID.search(str(data.get("source") or ""))
        if not match:
            continue
        record = dict(data)
        record["_challenge_id"] = match.group(0)
        record["_path"] = path.name
        if str(data.get("kind") or "case") == "negative":
            negatives.append(record)
        else:
            # One case template per box; the store already de-duplicates, but a
            # human-curated replay can coexist with the distilled one.
            cases.setdefault(match.group(0), record)
    return cases, negatives


def first_move(steps: list[str]) -> str:
    """The step a Worker should open with: the first one that *is* a move.

    Falls back to the first step so an entry never ships without one.
    """

    for step in steps:
        if _ACTIONABLE.search(step):
            return step
    return steps[0] if steps else ""


def build() -> int:
    challenges = {
        row["id"]: row
        for row in mysql_json(
            "select json_object('id',id,'status',status,'name',"
            "replace(replace(name,char(9),' '),char(10),' ')) from challenges"
        )
    }
    print(f"challenges: {len(challenges)}")

    events: dict[str, list[dict]] = defaultdict(list)
    for row in mysql_json(
        "select json_object("
        "'cid', r.challenge_id,"
        "'type', e.event_type,"
        "'actor', coalesce(json_unquote(json_extract(e.payload_json,'$.actor')),''),"
        "'text', coalesce("
        "  json_unquote(json_extract(e.payload_json,'$.payload.payload.reason')),"
        "  json_unquote(json_extract(e.payload_json,'$.payload.payload.fact')),"
        "  '')) "
        "from run_events e join solve_runs r on r.id = e.run_id "
        "where e.event_type in ('muteki.dead_end','muteki.fact_added') "
        f"and e.created_at >= '{SINCE}'"
    ):
        events[str(row.get("cid") or "")].append(row)
    print(f"challenges with recorded dead_end/fact: {sum(1 for v in events.values() if v)}")

    cases, negatives = load_templates()
    print(f"templates: {len(cases)} case / {len(negatives)} negative")

    walls_by_challenge: dict[str, list[str]] = defaultdict(list)
    for record in negatives:
        cid = record["_challenge_id"]
        for step in record.get("steps") or []:
            line = clean(str(step))
            if line:
                walls_by_challenge[cid].append(line)

    entries: list[dict] = []
    stats = {"success": 0, "wall": 0, "no_name": 0}

    # --- Source A: boxes this project has already solved ------------------
    for cid, template in cases.items():
        meta = challenges.get(cid)
        if not meta:
            stats["no_name"] += 1
            continue
        name = meta["name"]
        steps = rank([line for line in (clean(str(s)) for s in template.get("steps") or []) if line])
        if not steps:
            continue
        walls = rank(walls_by_challenge.get(cid, []))[:2]
        source = str(template.get("source") or "")
        entries.append(
            {
                "id": f"learned-box:{cid}",
                "kind": "poc",
                "title": name,
                "box_names": [name],
                "summary": (
                    "本机实测解出过这道题，下面是当时的执行路径。来源 "
                    f"{source}。靶机是新实例，凭据/路径需逐条对照确认。"
                ),
                "technique": str(template.get("category") or "web"),
                "preconditions": [],
                "target_stack": [str(template.get("category") or "web")],
                # Only measured success can command; that is what the flag
                # behind ``success_format`` proves.
                "verified_solve": True,
                # Nothing wrote or audited this by hand -- a machine distilled it
                # from a Run.  Measurement is carried by ``verified_solve``; the
                # two fields answer different questions on purpose.
                "trust_level": "unverified",
                "steps": steps[:MAX_STEPS],
                "first_action": first_move(steps[:MAX_STEPS]),
                "known_failures": walls,
                "published_at": GENERATED_ON,
                "provenance": {
                    "source_type": "arena_run_history",
                    "source_ref": str(template.get("source") or ""),
                    "notes": f"distilled from {template['_path']}",
                },
            }
        )
        stats["success"] += 1

    # --- Source B: boxes still unsolved, from their own failed rounds -----
    for cid, rows in events.items():
        if cid in cases:
            continue  # an already-solved box is served by its success entry
        meta = challenges.get(cid)
        if not meta:
            continue
        name = meta["name"]
        walls = rank([line for line in (clean(str(r.get("text"))) for r in rows) if line])
        if not walls:
            continue
        runs = {r.get("text") for r in rows if r.get("text")}
        entries.append(
            {
                "id": f"learned-wall:{cid}",
                "kind": "negative",
                "title": name,
                "box_names": [name],
                "summary": f"本机 {len(rows)} 条历史记录中实测排除的路径。",
                "technique": "web",
                "preconditions": [],
                "target_stack": ["web"],
                "verified_solve": False,
                "trust_level": "unverified",
                "steps": [],
                "known_failures": walls[:MAX_WALLS],
                # These lines are mostly recon the run already settled (real
                # paths, the routing model, which handlers start a session), so
                # "do not waste the round on" would misfile them as traps.
                "known_failures_title": "Already established here by earlier rounds (do not re-derive)",
                "published_at": GENERATED_ON,
                "provenance": {
                    "source_type": "arena_run_history",
                    "source_ref": f"{len(runs)} recorded dead_end/fact lines",
                    "notes": "distilled from run_events; no accepted flag behind it",
                },
            }
        )
        stats["wall"] += 1

    OUTPUT.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(entries)} entries -> {OUTPUT.relative_to(ROOT)}")
    print(f"  success-path entries : {stats['success']}")
    print(f"  wall-only entries    : {stats['wall']}")
    if stats["no_name"]:
        print(f"  skipped (box not in roster): {stats['no_name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
