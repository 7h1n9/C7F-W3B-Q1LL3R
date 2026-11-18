"""Mark a recipe as verified once a Run on this platform actually solved with it.

The gap this fills
------------------
``verified_solve`` is the only field that lets a recipe issue a FIRST ACTION --
``_pick_entries`` sorts on it first, ahead of curated and learned entries.  But
nothing ever set it: ``merge_vulhub_playbook.py`` only *preserves* an existing
value while merging, so the field was 0 across all 478 imported entries no
matter how many Runs succeeded.

Measured 2026-11-06: a CouchDB box was solved in 766s using
``vulhub:couchdb:cve-2017-12635``.  The imported payload is
``PUT /_users/org.couchdb.user:vulhub`` with ``roles: ["_admin"]`` -- a privilege
escalation -- and the Run afterwards read ``_config`` and injected a
``query_server`` to get command execution.  Those endpoints require the admin
role the payload grants, so the chain is real: the payload is why the later
steps worked.  That is exactly what ``verified_solve`` is supposed to mean.

Why attribution has to be mechanical
------------------------------------
The temptation is to mark "the recipe that was in AGENTS.md" whenever a Run with
that recipe succeeds.  That is how a wrong entry gets promoted forever: the
Drupal Run of 2026-11-06 solved its box while the imported Drupalgeddon2 payload
returned the registration page, and crediting the payload would have been a lie.

So attribution requires evidence, not coincidence.  An entry is credited only
when **both** hold:

1. the Run produced an accepted (``VALID``) flag, and
2. at least one *distinctive token* from the entry's own payload appears in the
   commands the Worker actually sent.

Distinctive means a token that is not generic HTTP noise -- ``_all_docs`` counts,
``GET`` does not.  The token set is derived from the payload itself so this stays
correct as the corpus grows, and an entry with no distinctive token is never
credited (silence beats a guess).

This script only reads run events and rewrites ``verified_solve`` /
``verified_at_run`` in the playbook.  It never changes a payload.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK = ROOT / "data" / "knowledge" / "vulhub.jsonl"

#: Tokens too generic to prove anything: every HTTP payload contains them.
_STOPWORDS = frozenset(
    {
        "http", "https", "host", "content-type", "content-length", "accept",
        "accept-language", "user-agent", "connection", "close", "keep-alive",
        "application", "json", "x-www-form-urlencoded", "mozilla", "compatible",
        "windows", "linux", "server", "date", "curl", "echo", "true", "false",
        "localhost", "127.0.0.1", "get", "post", "put", "delete", "head",
    }
)

#: A token must be at least this long to be worth matching.
_MIN_TOKEN = 6

#: How many distinct payload tokens must appear in the Run's commands.
#:
#: One is enough *when the token is rare*, because rarity already does the work
#: that a count cannot: ``_users/org.couchdb.user`` exists in exactly one entry,
#: so sending it is near-proof the recipe was read.
MIN_TOKEN_MATCHES = 1

#: A token appearing in more than this many entries is vocabulary, not a
#: fingerprint.  ``_drupal_ajax`` shows up across the Drupal family, so a Run
#: matching it says only "this was a Drupal box", which the challenge title
#: already said.
MAX_TOKEN_DOCUMENT_FREQUENCY = 2


def distinctive_tokens(payload: str) -> set[str]:
    """Path segments and parameter names that identify this payload.

    Deliberately conservative: only things that look like endpoints, parameter
    names or literal values, because those are what a Worker copies when it
    sends *this* recipe rather than improvising.
    """

    tokens: set[str] = set()
    # Endpoint-ish: /_all_docs, /_node/..., /solr/admin/cores, /index.php?option=
    for match in re.finditer(r"/[A-Za-z0-9_][A-Za-z0-9_\-/\.]{2,}", payload):
        token = match.group(0).strip("/").lower()
        if len(token) >= _MIN_TOKEN and token not in _STOPWORDS:
            tokens.add(token)
    # Query parameter names: option=, list[fullordering]=, element_parents=
    for match in re.finditer(r"[?&]([A-Za-z0-9_\-\[\]]{4,})=", payload):
        name = match.group(1).lower()
        if len(name) >= _MIN_TOKEN and name not in _STOPWORDS:
            tokens.add(name)
    return tokens


def load_runs(database_url: str, limit: int) -> list[dict]:
    """Solved Runs with an accepted flag, plus the commands they sent.

    Commands alone are not enough to attribute, which the 2026-11-06 corpus made
    concrete: the Drupal Run ``e92b8431`` sent requests carrying
    ``_drupal_ajax`` / ``element_parents`` -- strings unique to the imported
    recipe -- and those requests *returned the registration page*.  The payload
    did not fire; the flag came from a different chain.  A command-only matcher
    cannot see that difference, because the mistake is in the **response**.

    So each command is paired with the result that followed it, and an
    attribution needs at least one matched command whose reply is not simply the
    application's own page.
    """

    import pymysql

    match = re.search(r"//([^:]+):([^@]+)@([^:/]+):(\d+)/(\w+)", database_url)
    if not match:
        raise SystemExit(f"cannot parse database url: {database_url}")
    user, password, host, port, db = match.groups()
    conn = pymysql.connect(
        host=host, port=int(port), user=user, password=password, database=db,
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=True,
    )
    out: list[dict] = []
    with conn.cursor() as cur:
        cur.execute(
            """select r.id, c.name, c.metadata_json
                 from solve_runs r join challenges c on c.id = r.challenge_id
                where r.status = 'COMPLETED_SOLVED'
                order by r.started_at desc limit %s""",
            (limit,),
        )
        runs = cur.fetchall()
        for run in runs:
            cur.execute(
                """select review_state, candidate from flag_candidates
                    where run_id = %s and review_state = 'VALID' limit 1""",
                (run["id"],),
            )
            flag_row = cur.fetchone()
            if not flag_row:
                continue
            cur.execute(
                """select payload_json from run_events
                    where run_id = %s and event_type = 'muteki.worker_command'
                    order by sequence""",
                (run["id"],),
            )
            commands = []
            replies: list[str] = []
            for row in cur.fetchall():
                payload = (json.loads(row["payload_json"]) or {}).get("payload") or {}
                if payload.get("kind") == "tool" and payload.get("command"):
                    commands.append(str(payload["command"]).lower())
                    replies.append("")
                elif payload.get("kind") == "tool_result" and commands:
                    # Attach to the most recent command.
                    replies[-1] = str(payload.get("result_excerpt") or "").lower()
            out.append(
                {
                    "run_id": str(run["id"]),
                    "name": str(run["name"]),
                    "flag": str(flag_row["candidate"]),
                    "commands": commands,
                    "replies": replies,
                }
            )
    conn.close()
    return out


def _entry_matches_challenge(entry_id: str, challenge_name: str) -> bool:
    """Does this entry even claim to be about this box?

    Token overlap alone is not enough, and the failure is not hypothetical:
    dry-running this against the 2026-11-06 corpus credited
    ``vulhub:drupal:cve-2018-7600`` for the Drupal Run ``e92b8431`` -- the exact
    Run where that payload was **proven not to fire** (it returned the
    registration page, and the flag came from a different phar chain).  The
    overlap came from generic Drupal paths appearing in both.

    So the entry has to name the same product or the same CVE as the box.
    Anything else is a coincidence of vocabulary, not evidence of causation.
    """

    name = challenge_name.lower()
    lowered = entry_id.lower()

    cves = re.findall(r"cve-\d{4}-\d{4,7}", lowered)
    if cves:
        return any(cve in name for cve in cves)

    # No CVE in the id: fall back to the product segment (``vulhub:solr:...``).
    parts = [p for p in re.split(r"[:/]", lowered) if p and p not in ("vulhub", "awesome-poc")]
    return any(part and part in name for part in parts)


#: A reply that looks like this means the payload did not fire: the target
#: answered with its own page.  Measured on Drupal ``e92b8431`` -- the imported
#: Drupalgeddon2 request came back as the registration form, and crediting that
#: payload would have promoted a recipe proven not to work on this platform.
#:
#: Narrowed after GhostScript ``8d56bfab``: a bare ``<html`` / ``<form`` was
#: treated as "the app re-served its page", but a vulnerable *upload* page
#: legitimately answers with its own form (``enctype="multipart/form-data"``),
#: so that rule rejected a real solve.  Only markers that specifically mean
#: "a page came back instead of a reaction" belong here.
_DEAD_REPLY = re.compile(
    r"(name=\"form_build_id\"|name=\"form_token\"|"
    r"404 not found|page not found|service unavailable|bad gateway)",
    re.IGNORECASE,
)

#: A reply that looks like this means something executed, errored, or returned
#: data: the payload changed the target's behaviour.
_LIVE_REPLY = re.compile(
    r"(uid=\d|root:|www-data|/bin/(ba)?sh|command not found|"
    r"xp[a-z ]*error|sql syntax|traceback|stack trace|exception|"
    r"\"total_rows\"|\"rows\"|_admin|error\":|warning:|"
    # Structured API responses: the endpoint answered with data rather than
    # re-serving a page.  Added after two genuine solves were rejected --
    # 1Panel's login bypass returned {"code":200,...,"token":"","mfastatus":""}
    # plus a psession cookie, which the shell-oriented pattern could not see.
    r"\"code\":\s*\d|\"data\":|\"token\"|\"message\":|psession|"
    # An upload surface answering with its own handler name is a live target.
    r"multipart/form-data|file_upload)",
    re.IGNORECASE,
)


def _reply_is_live(reply: str) -> bool:
    """Did the target react to this request, rather than re-serve its own page?

    Both conditions must hold: a live marker present AND the page-shaped markers
    absent.  A 500 with a stack trace is live; a 200 that is the login form is
    not.  This is the only signal in the data that separates "the recipe ran"
    from "the recipe was sent and ignored".
    """

    if not reply:
        return False
    return bool(_LIVE_REPLY.search(reply)) and not _DEAD_REPLY.search(reply)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playbook", default=str(PLAYBOOK))
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--runs", type=int, default=200, help="how many solved Runs to scan")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    entries = [
        json.loads(line)
        for line in Path(args.playbook).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_id = {e["id"]: e for e in entries}
    token_index = {e["id"]: distinctive_tokens(e.get("payload_template") or "") for e in entries}

    # Token rarity across the whole corpus.  This is the correction that makes
    # attribution work at all: raw overlap could not separate two real Runs.
    #
    #   CouchDB ``bd2d60a2`` (payload genuinely used): matched
    #   ``_users/org.couchdb.user`` -- a string only this recipe contains.
    #   Drupal ``e92b8431`` (payload proven NOT to fire, flag came from another
    #   chain): matched ``_drupal_ajax`` / ``element_parents`` / ``user/register``
    #   -- generic Drupal vocabulary that any Worker emits on any Drupal box.
    #
    # Counting matches scored the second one *higher* (5 tokens vs 1).  Rarity
    # inverts that: a token shared by dozens of entries says nothing about which
    # one was followed, while a token in exactly one entry is close to a
    # fingerprint.
    df: dict[str, int] = {}
    for tokens in token_index.values():
        for token in tokens:
            df[token] = df.get(token, 0) + 1

    def rare_hits(entry_id: str, text: str) -> int:
        """How many of this entry's *rare* tokens the Worker actually sent."""

        return sum(
            1
            for token in token_index[entry_id]
            if df.get(token, 0) <= MAX_TOKEN_DOCUMENT_FREQUENCY and token in text
        )

    runs = load_runs(args.database_url, args.runs)
    print(f"扫描 {len(runs)} 个已解出且 flag 有效的 Run")

    credited: dict[str, dict] = {}
    rejected: list[tuple[str, str, str]] = []

    for run in runs:
        joined = "\n".join(run["commands"])
        # Which entries does the Worker's own command stream actually implicate?
        scored = []
        for entry_id in token_index:
            if not _entry_matches_challenge(entry_id, run["name"]):
                continue
            hits = rare_hits(entry_id, joined)
            if not hits:
                continue
            # The payload must have been sent AND the target must have reacted.
            # Without the second half the Drupal false positive returns: unique
            # tokens, request sent, registration page back, credit wrongly given.
            live = any(
                _reply_is_live(run["replies"][i])
                for i in range(len(run["commands"]))
                if any(
                    token in run["commands"][i]
                    for token in token_index[entry_id]
                    if df.get(token, 0) <= MAX_TOKEN_DOCUMENT_FREQUENCY
                )
            )
            if not live:
                continue
            scored.append((hits, entry_id))
        if not scored:
            rejected.append(
                (run["name"], run["run_id"], "no rare token with a live reply")
            )
            continue
        scored.sort(reverse=True)
        top_score, top_entry = scored[0]
        if len(scored) > 1 and scored[1][0] == top_score:
            rejected.append((run["name"], run["run_id"], f"ambiguous between {len(scored)} entries"))
            continue
        if top_score < MIN_TOKEN_MATCHES:
            rejected.append((run["name"], run["run_id"], f"only {top_score} rare token(s)"))
            continue
        credited.setdefault(top_entry, {"run_id": run["run_id"], "name": run["name"], "score": top_score})

    print(f"\n可归因 {len(credited)} 条，无法归因 {len(rejected)} 条")
    for entry_id, info in credited.items():
        print(f"  ✓ {entry_id}  <- run {info['run_id'][:8]} ({info['name'][:28]}) 匹配 {info['score']} 个特征")
    for name, run_id, why in rejected[:8]:
        print(f"  · {name[:30]:32s} {run_id[:8]}  {why}")

    changed = 0
    for entry_id, info in credited.items():
        entry = by_id.get(entry_id)
        if entry is None:
            continue
        if entry.get("verified_solve"):
            continue
        entry["verified_solve"] = True
        # ``KnowledgeItem.trust_level`` is a closed Literal
        # (``reviewed`` / ``curated`` / ``unverified``), and ``provenance`` is
        # ``extra="forbid"`` with only four fields.  Writing ``verified`` or a new
        # ``verified_by_run`` key failed schema validation on load and took out
        # 13 tests.  ``reviewed`` is the honest value for a one-Run confirmation,
        # and the Run id goes into ``provenance.notes``, which is free text.
        entry["trust_level"] = "reviewed"
        provenance = entry.get("provenance")
        if isinstance(provenance, dict):
            previous = str(provenance.get("notes") or "").strip()
            provenance["notes"] = (
                (previous + " " if previous else "")
                + f"verified by run {info['run_id']} (accepted flag)"
            )
        changed += 1

    print(f"\n更新 {changed} 条 verified_solve=True（此前为 0 的）")
    if not args.dry_run and changed:
        Path(args.playbook).write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
            encoding="utf-8",
        )
        print(f"写入 {args.playbook}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
