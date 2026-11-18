"""Unit tests for the transition exporter's pure core.

No database, no fixtures beyond plain dicts: the whole point of keeping
``app.training.transitions`` side-effect free is that its behaviour is fully
specified by tests like these.
"""

from __future__ import annotations

from app.training.transitions import (
    EventInput,
    RunInput,
    assign_split,
    build_transitions,
    classify_run,
    group_key_for,
)


def make_run(**overrides) -> RunInput:
    base = dict(
        run_id="run-1",
        challenge_id="chal-1",
        status="COMPLETED_SOLVED",
        accepted=True,
        fresh_reproduction_verified=False,
        challenge_name="[CISCN2019]Web6",
    )
    base.update(overrides)
    return RunInput(**base)


def command(event_id: int, cmd: str, **payload) -> EventInput:
    data = {
        "kind": "shell",
        "tool": "command_execution",
        "turn": event_id,
        "call_id": f"call-{event_id}",
        "command": cmd,
        "session": "main",
        "worker_id": "worker-1",
        "result_excerpt": f"output of {cmd}",
    }
    data.update(payload)
    return EventInput(seq=event_id, event_type="muteki.worker_command", payload=data)


def fact(event_id: int, text: str, actor: str = "worker-1") -> EventInput:
    return EventInput(
        seq=event_id,
        event_type="muteki.fact_added",
        payload={"actor": actor, "payload": {"fact": text}, "upstream_sequence": event_id},
    )


def dead_end(event_id: int, reason: str, actor: str = "worker-1") -> EventInput:
    return EventInput(
        seq=event_id,
        event_type="muteki.dead_end",
        payload={"actor": actor, "payload": {"reason": reason}, "upstream_sequence": event_id},
    )


# ---------------------------------------------------------------------------
# Transition construction
# ---------------------------------------------------------------------------


def test_single_command_produces_one_transition_with_zero_deltas():
    transitions, audit = build_transitions(make_run(), [command(1, "curl http://target/")])
    assert len(transitions) == 1
    t = transitions[0]
    assert t["sequence"] == 0
    assert t["state"]["facts"] == 0
    assert t["state"]["commands_issued"] == 0
    assert t["observation"]["delta_facts"] == 0
    assert t["labels"]["final_status"] == "COMPLETED_SOLVED"
    assert t["labels"]["accepted"] is True
    assert audit["exclude_reason"] is None
    assert audit["command_events"] == 1


def test_knowledge_between_commands_attributes_to_previous_transition():
    events = [
        command(1, "nmap target"),
        fact(2, "port 8080 open running Apache"),
        dead_end(3, "gobuster /admin returned 404"),
        command(4, "curl http://target:8080/"),
        fact(5, "login page found"),
    ]
    transitions, audit = build_transitions(make_run(), events)
    assert len(transitions) == 2

    first, second = transitions
    # The fact and the dead end landed between command 1 and command 2, so
    # they are the observable effect of command 1.
    assert first["observation"]["delta_facts"] == 1
    assert first["observation"]["delta_dead_ends"] == 1
    # And the second command's state reflects what was established.
    assert second["state"]["facts"] == 1
    assert second["state"]["dead_ends"] == 1
    assert "port 8080 open running Apache" in second["state"]["recent_facts"]
    assert second["observation"]["delta_facts"] == 1
    assert audit["fact_events"] == 2
    assert audit["dead_end_events"] == 1


def test_events_are_sorted_by_id_not_input_order():
    events = [command(3, "second"), fact(2, "early fact"), command(1, "first")]
    transitions, _ = build_transitions(make_run(), events)
    assert [t["action"]["command"] for t in transitions] == ["first", "second"]
    # The fact (id=2) sits between command id=1 and id=3.
    assert transitions[0]["observation"]["delta_facts"] == 1


def test_recovery_rewrapped_facts_are_not_counted():
    events = [
        command(1, "cat /flag"),
        fact(2, "recovered flag offered again", actor="coordinator:recovery:flag"),
        fact(3, "real finding", actor="worker-1"),
    ]
    transitions, audit = build_transitions(make_run(), events)
    assert transitions[0]["observation"]["delta_facts"] == 1
    assert audit["fact_events"] == 1


def test_wrapped_envelope_command_payload_is_unwrapped():
    # Older runs stored worker_command with the same {payload: {...},
    # upstream_*} envelope as knowledge events; both shapes must parse.
    wrapped = EventInput(
        seq=1,
        event_type="muteki.worker_command",
        payload={
            "payload": {"tool": "shell", "command": "id", "result_excerpt": "uid=33"},
            "upstream_sequence": 7,
            "upstream_event_type": "tool_call",
        },
    )
    transitions, _ = build_transitions(make_run(), [wrapped])
    assert transitions[0]["action"]["command"] == "id"
    assert transitions[0]["observation"]["result_excerpt"] == "uid=33"


def test_recent_commands_window_is_bounded():
    events = [command(i, f"cmd-{i}") for i in range(1, 10)]
    transitions, _ = build_transitions(make_run(), events)
    last = transitions[-1]
    assert len(last["state"]["recent_commands"]) == 5
    assert last["state"]["recent_commands"][-1] == "cmd-8"


# ---------------------------------------------------------------------------
# Quality gate
# ---------------------------------------------------------------------------


def test_cancelled_run_is_excluded_with_reason():
    transitions, audit = build_transitions(
        make_run(status="CANCELLED"), [command(1, "curl x")]
    )
    assert transitions == []
    assert audit["exclude_reason"] == "status:cancelled"


def test_infrastructure_error_code_excludes_run():
    transitions, audit = build_transitions(
        make_run(status="UNSOLVED", last_error_code="STALLED_NO_WORKER_COMMAND"),
        [command(1, "curl x")],
    )
    assert transitions == []
    assert audit["exclude_reason"] == "infrastructure:stalled_no_worker_command"


def test_infrastructure_message_needle_excludes_legacy_runs():
    transitions, audit = build_transitions(
        make_run(status="FAILED", last_error_message="No healthy worker profile available"),
        [command(1, "curl x")],
    )
    assert transitions == []
    assert audit["exclude_reason"].startswith("infrastructure:")


def test_zero_command_run_is_excluded():
    assert classify_run(make_run(status="UNSOLVED"), command_count=0) == "no_commands"


def test_unsolved_but_honest_run_is_kept():
    # A failed trajectory is dead-end mining material, not garbage.
    assert classify_run(make_run(status="COMPLETED_UNSOLVED"), command_count=12) is None


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_flags_and_secrets_are_redacted_everywhere():
    events = [
        command(
            1,
            "curl -H 'Authorization: Bearer abc123' http://10.0.0.8:8080/login",
            result_excerpt="flag{th1s_1s_r34l} password=hunter2",
        ),
        fact(2, "flag{th1s_1s_r34l} works"),
    ]
    transitions, audit = build_transitions(make_run(), events)
    blob = str(transitions)
    assert "flag{th1s_1s_r34l}" not in blob
    assert "abc123" not in blob
    assert "hunter2" not in blob
    assert "10.0.0.8" not in blob
    assert audit["redactions"] > 0


def test_known_secret_is_replaced_even_with_unknown_prefix():
    # Arena flags are not always flag{...}: the exact accepted string is the
    # only reliable redaction target.
    transitions, audit = build_transitions(
        make_run(),
        [command(1, "echo DASCTF{unusual_prefix_xxx} >> answer.txt")],
        known_secrets=["DASCTF{unusual_prefix_xxx}"],
    )
    assert "unusual_prefix_xxx" not in str(transitions)
    assert "{{flag_pattern}}" in transitions[0]["action"]["command"]
    assert audit["redactions"] >= 1


def test_ctf_prefixed_flags_are_redacted_without_known_secrets():
    transitions, _ = build_transitions(
        make_run(),
        [command(1, "cat out.txt", result_excerpt="got SCTF{r3v3rs3_m3} here")],
    )
    assert "r3v3rs3_m3" not in str(transitions)


def test_canonical_technique_paths_survive_redaction():
    # /etc/passwd is public knowledge and the whole point of an LFI step; the
    # shared sanitiser's generic LINUX_PATH rule must not eat it.
    transitions, _ = build_transitions(
        make_run(), [command(1, "curl 'http://t/?f=../../etc/passwd'")]
    )
    text = transitions[0]["action"]["command"]
    assert "/etc/passwd" in text


def test_machine_specific_paths_are_still_redacted():
    transitions, _ = build_transitions(
        make_run(), [command(1, "cd /home/kali/workspace/session-42 && ls")]
    )
    assert "/home/kali/workspace" not in transitions[0]["action"]["command"]


def test_url_origin_is_redacted_but_path_survives():
    transitions, _ = build_transitions(
        make_run(),
        [command(1, "curl -s http://10.1.2.3:8080/api/v2/users?id=1")],
    )
    text = transitions[0]["action"]["command"]
    assert "10.1.2.3" not in text
    assert "{{target_url}}" in text
    # The path is the technique; deleting it would make the transition useless.
    assert "/api/v2/users?id=1" in text


def test_split_flag_half_is_redacted():
    # A two-part flag never appears complete, so only the head rule can see it.
    transitions, _ = build_transitions(
        make_run(),
        [command(1, "cat robots.txt", result_excerpt="PART ONE: flag{r0bots_1s_s0_us3ful---")],
    )
    assert "r0bots_1s_s0_us3ful" not in str(transitions)


def test_recon_command_with_bare_flag_literal_stays_readable():
    # `grep -r "flag{" .` is a legitimate finding, not a leak: it has no body,
    # so the head rule must leave it alone.
    transitions, _ = build_transitions(
        make_run(), [command(1, 'grep -rn "flag{" . 2>/dev/null')]
    )
    assert "grep" in transitions[0]["action"]["command"]


def test_flag_straddling_the_truncation_boundary_is_still_redacted():
    # Redaction must run before truncation: a flag cut in half matches neither
    # the exact-secret pass nor any {...} regex.
    prefix = "A" * 590
    transitions, _ = build_transitions(
        make_run(),
        [command(1, "cat evidence.txt", result_excerpt=prefix + "flag{leaked_tail_value}")],
    )
    excerpt = transitions[0]["observation"]["result_excerpt"]
    assert len(excerpt) <= 600
    assert "leaked_tail_value" not in excerpt


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------


def test_cve_challenge_groups_by_cve_id():
    key = group_key_for("chal-9", "[vulhub] Apache Shiro CVE-2016-4437 反序列化")
    assert key == "cve:CVE-2016-4437"
    assert group_key_for("chal-1", "Web6") == "challenge:chal-1"


def test_split_is_deterministic_per_seed():
    assert assign_split("challenge:x", seed="s1") == assign_split("challenge:x", seed="s1")


def test_same_challenge_never_crosses_splits():
    # Leakage guard: no matter how often a challenge appears in history, every
    # run of it lands in exactly one split.
    splits = {assign_split(group_key_for("c1", "Web6"), seed="s") for _ in range(50)}
    assert len(splits) == 1


def test_split_distribution_covers_all_buckets():
    from collections import Counter

    counts = Counter(assign_split(f"challenge:{i}", seed="c7f-v1") for i in range(1000))
    assert set(counts) == {"train", "dev", "test"}
    assert counts["train"] > counts["dev"] > 0
    assert counts["train"] > counts["test"] > 0
