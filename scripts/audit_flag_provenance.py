"""Audit whether each solved Run's flag actually came from observed output.

Replaces the un-runnable fresh-reproduction check (see
``docs/notes/2026-11/2026-11-05-b1-feasibility.md``): the original design replayed an HTTP
request in a clean Runner session, but the legacy Runner is disabled in this
deployment, the solved Runs never made an ``http_request`` call, and every
target instance has been destroyed.  What *can* still be checked from stored
data is whether the flag the Run reported was ever actually seen in output --
a model that invents ``flag{...}`` leaves no trace in the event log.

Read-only.  Writes a report and, with ``--mark``, sets a review flag on the
suspect Runs.  It never deletes or downgrades anything.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: How many characters of context on each side of the flag to keep in the
#: report.  Enough to recognise the command that produced it, not enough to
#: leak the flag body into a file that gets pasted around.
CONTEXT_CHARS = 60


def _connect():
    """Connect to the compose MySQL.

    The published port is 3307, not 3306 (``docker port`` confirms it), and the
    container has to be reached over TCP because this script runs on the host.
    ``autocommit`` matters: without it the long read below freezes on the
    REPEATABLE READ snapshot taken at connect time.
    """

    import os

    import pymysql

    return pymysql.connect(
        host=os.environ.get("AUDIT_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("AUDIT_DB_PORT", "3307")),
        user=os.environ.get("AUDIT_DB_USER", "ctf_agent"),
        password=os.environ.get("AUDIT_DB_PASSWORD", "ctf_agent"),
        database=os.environ.get("AUDIT_DB_NAME", "ctf_agent"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def load_solved_runs(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.challenge_id, c.name, c.flag_pattern, c.target_url,
                   r.finished_at
            FROM solve_runs r
            LEFT JOIN challenges c ON c.id = r.challenge_id
            WHERE r.status = 'COMPLETED_SOLVED'
            ORDER BY r.finished_at DESC
            """
        )
        return list(cur.fetchall())


def load_flags(conn) -> dict[str, list[dict]]:
    """Every flag candidate per run, whatever its review state."""

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.run_id, f.candidate, f.review_state, f.verified,
                   f.source_tool_call_id
            FROM flag_candidates f
            JOIN solve_runs r ON r.id = f.run_id
            WHERE r.status = 'COMPLETED_SOLVED'
            """
        )
        out: dict[str, list[dict]] = defaultdict(list)
        for row in cur.fetchall():
            out[row["run_id"]].append(dict(row))
        return out


def load_outputs(conn, run_ids: list[str]) -> dict[str, list[tuple[int, str, str]]]:
    """(sequence, event_type, payload_text) for every event in these runs.

    The first version of this audit only looked at ``muteki.worker_command``
    tool_result excerpts and concluded 48% of flags were never observed.  That
    was wrong: a spot check on one "suspect" run showed the flag recorded in
    ``muteki.flag_found`` and ``muteki.fact_added`` instead, with a
    ``result_excerpt`` that had been truncated at 509 chars with a
    ``…[+2261 chars]`` marker -- 19% of excerpts are truncated that way, so the
    flag can legitimately sit in the discarded tail.  Scanning every event type
    removes both blind spots.

    ``event_type`` is kept alongside the payload so the verdict can say *where*
    it was seen: a flag in ``muteki.flag_found`` is a much stronger signal than
    one that merely appears inside a command string.
    """

    out: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    if not run_ids:
        return out
    # Chunked: this box SIGTERMs long multi-statement queries.
    for start in range(0, len(run_ids), 20):
        chunk = run_ids[start : start + 20]
        placeholders = ",".join(["%s"] * len(chunk))
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT run_id, sequence, event_type, payload_json
                FROM run_events
                WHERE run_id IN ({placeholders})
                ORDER BY run_id, sequence
                """,
                chunk,
            )
            for row in cur.fetchall():
                payload = row["payload_json"] or ""
                if not payload:
                    continue
                out[row["run_id"]].append(
                    (row["sequence"], row["event_type"], str(payload))
                )
    return out


def _redact(text: str, flag: str) -> str:
    """Blank the flag body but keep its shape, so reports stay shareable."""

    return text.replace(flag, "flag{<redacted>}")


#: Events that assert the platform itself recognised a flag.  Seeing the value
#: here is the strongest provenance available after the target is gone: it is
#: the host's own ``flag_pattern`` match, not the model's claim.
_AUTHORITATIVE_EVENTS = ("muteki.flag_found", "flag.candidate_found")

#: Events that carry what a command actually printed.  A flag appearing here
#: means it was in observed output, even if no dedicated event fired.
_OBSERVATION_EVENTS = ("muteki.worker_command", "muteki.worker_step")


def classify(run: dict, flags: list[dict], outputs: list[tuple[int, str, str]]) -> dict:
    """Decide whether the run's flag is attributable to observed output.

    Verdicts, strongest first:

    ``PLATFORM_CONFIRMED`` the flag value appears in a flag-found event, i.e.
                           the host matched it against ``flag_pattern``
    ``SEEN_IN_OUTPUT``     it appears in a command/tool-result payload
    ``SEEN_IN_NARRATIVE``  it only appears in facts/report text the model wrote
    ``NO_FLAG``            the run is solved but recorded no flag at all
    ``NOT_OBSERVED``       no candidate appears anywhere -- the suspect case
    """

    candidates = [f for f in flags if f.get("candidate")]
    confirmed: list[dict] = []
    observed: list[dict] = []
    narrative: list[dict] = []

    for flag in candidates:
        value = flag["candidate"]
        if not value:
            continue
        for seq, event_type, text in outputs:
            if value not in text:
                continue
            hit = {"sequence": seq, "event": event_type}
            if event_type in _AUTHORITATIVE_EVENTS:
                confirmed.append(hit)
            elif event_type in _OBSERVATION_EVENTS:
                observed.append(hit)
            else:
                narrative.append(hit)

    if not candidates:
        verdict = "NO_FLAG"
    elif confirmed:
        verdict = "PLATFORM_CONFIRMED"
    elif observed:
        verdict = "SEEN_IN_OUTPUT"
    elif narrative:
        verdict = "SEEN_IN_NARRATIVE"
    else:
        verdict = "NOT_OBSERVED"

    first = (confirmed or observed or narrative or [{}])[0]
    return {
        "run_id": run["id"],
        "name": run.get("name") or "",
        "target_url": run.get("target_url") or "",
        "flag_pattern": run.get("flag_pattern") or "",
        "finished_at": str(run.get("finished_at") or ""),
        "events": len(outputs),
        "candidates": len(candidates),
        "verdict": verdict,
        "evidence_sequence": first.get("sequence"),
        "evidence_event": first.get("event"),
        # A flag seen only in model-authored text is worth a human look; a
        # missing flag entirely is a data problem, not a cheating signal, so it
        # is reported but not treated as a provenance failure.
        "suspect": verdict == "NOT_OBSERVED",
    }


def pattern_mismatch(run: dict, flags: list[dict]) -> dict | None:
    """Flag accepted by the platform but not matching the challenge's pattern.

    Not a provenance failure on its own -- a spot check on ``641f7b08`` showed
    the model reporting ``n1book{git_looks_s0_easyfun}`` and the platform
    normalising it to ``CTF2{git_looks_s0_easyfun}``, both recorded with
    ``verified=True``.  What it does reveal is bad challenge metadata: several
    imported challenges carry ``flag\\{[^}]+\\}`` while their instance actually
    issues ``CTF2{...}``.

    Reported separately so a metadata problem is never mistaken for a cheating
    signal, which is exactly the mistake the first version of this audit made.
    """

    pattern = run.get("flag_pattern") or ""
    if not pattern:
        return None
    for flag in flags:
        value = flag.get("candidate") or ""
        if not value:
            continue
        try:
            if re.search(pattern, value):
                return None
        except re.error:
            return None
        return {
            "run_id": run["id"],
            "name": run.get("name") or "",
            "flag_pattern": pattern,
            # Only the wrapper matters for diagnosing the metadata; the body is
            # a real accepted flag and must not be written to a report file.
            "accepted_prefix": value.split("{", 1)[0] + "{...}",
            "review_state": flag.get("review_state"),
        }
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mark", action="store_true", help="tag suspect runs (no deletion)")
    parser.add_argument("--out", default="data/audit/flag-provenance.json")
    args = parser.parse_args()

    conn = _connect()
    try:
        runs = load_solved_runs(conn)
        flags = load_flags(conn)
        print(f"solved runs: {len(runs)}")
        outputs = load_outputs(conn, [r["id"] for r in runs])
        print(f"runs with events: {sum(1 for v in outputs.values() if v)}")

        results = [classify(r, flags.get(r["id"], []), outputs.get(r["id"], [])) for r in runs]
        mismatches = [
            m
            for r in runs
            if (m := pattern_mismatch(r, flags.get(r["id"], []))) is not None
        ]
    finally:
        conn.close()

    tally = Counter(r["verdict"] for r in results)
    print("\nverdicts:")
    for verdict, count in tally.most_common():
        print(f"  {verdict:20s} {count}")

    suspects = [r for r in results if r["suspect"]]
    print(f"\nsuspect: {len(suspects)}")
    for row in suspects[:12]:
        print(
            f"  {row['run_id'][:8]} {row['name'][:34]:36s} "
            f"cand={row['candidates']} events={row['events']}"
        )

    print(f"\nflag_pattern mismatches (metadata issue, not cheating): {len(mismatches)}")
    for row in mismatches[:8]:
        print(
            f"  {row['run_id'][:8]} {row['name'][:30]:32s} "
            f"pattern={row['flag_pattern']!r} got={row['accepted_prefix']} "
            f"({row['review_state']})"
        )

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "solved_total": len(runs),
        "verdicts": dict(tally),
        "suspect_count": len(suspects),
        "pattern_mismatch_count": len(mismatches),
        "pattern_mismatches": mismatches,
        "runs": results,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport -> {out_path}")

    if args.mark and suspects:
        print("\n--mark given: tagging suspect runs")
        conn = _connect()
        try:
            with conn.cursor() as cur:
                for row in suspects:
                    cur.execute(
                        """
                        UPDATE solve_runs
                        SET hints_json = JSON_SET(
                            COALESCE(hints_json, '{}'),
                            '$.flag_provenance',
                            %s
                        )
                        WHERE id = %s
                        """,
                        (row["verdict"], row["run_id"]),
                    )
            print(f"tagged {len(suspects)} runs via hints_json.$.flag_provenance")
        finally:
            conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
