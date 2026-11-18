"""An unmeasured recipe that MATCHED a box must still reach the Worker.

``exploit_first_action`` is gated on ``verified_solve`` / ``trust_level ==
"reviewed"``, and that gate is right: the slot it fills is the highest-authority
line in the prompt, and a vulhub recipe that passes every schema check can still
be wrong for this instance (the S2-009 ``/ajax/example5`` case).

But measured 2026-11-08 over the 81 UNSOLVED arena Challenges, the gate left
**1** box able to issue a first action.  Twenty-one boxes already matched a real
vulhub recipe; thirteen of those recipes held a runnable first line, and every
one was discarded in favour of a technique clause that 68 boxes shared verbatim.

These tests pin the probe channel that closes that gap, and -- just as important
-- pin the two evasions that made the first version wrong: source fragments
handed out as commands, and payload lines that only ever accepted the HTTP verb
form.
"""

from __future__ import annotations

import pytest

from app.services.known_exploit import (
    _fa_payload_line,
    _fa_substitute_target,
    exploit_first_action,
    exploit_probe_action,
)


class _Box:
    """The three attributes the selector reads off a Challenge."""

    def __init__(self, name: str, **kw):
        self.name = name
        self.category = "web"
        self.description = ""
        self.target_url = kw.get("target_url", "")
        self.flag_format = r"flag\{.*?\}"


# ── the payload-line extractor ──────────────────────────────────────────────


def test_http_request_line_is_a_move() -> None:
    payload = "POST /service/extdirect HTTP/1.1\nHost: {{host}}"
    assert _fa_payload_line(payload, 190).startswith("send `POST /service/extdirect")


def test_shell_command_is_a_move() -> None:
    """A vulhub payload is as often a shell command as an HTTP request."""
    payload = 'ssh -p 3322 -i id_rsa -t git@{{target_url}} "git-upload-archive \'--help\'"'
    assert _fa_payload_line(payload, 190).startswith("ssh -p 3322")


def test_script_payload_yields_its_invocation_not_its_shebang() -> None:
    payload = "#!/usr/bin/env python\nimport sys\nimport requests\n\nmain()"
    line = _fa_payload_line(payload, 190)
    assert line == "", "a script with no invocation line is not a first move"


def test_script_with_invocation_line_is_a_move() -> None:
    """A script whose first real line is an invocation is usable; only its
    shebang/import preamble is skipped."""
    payload = "#!/bin/sh\ncurl -s http://target/ -d 'a=1'\n"
    assert _fa_payload_line(payload, 190).startswith("curl -s")


@pytest.mark.parametrize(
    "payload",
    [
        "package main",
        "import sys",
        "import (\n\t\"fmt\"\n)",
        "class JDatabaseDriverMysql {",
        "public x(){",
        "static void before_main(void) __attribute__((constructor));",
        "var $sanitize;",
        "var uid int64 = 1",
        "buf := bytes.NewBuffer(nil)",
        "offset = 605",
        "headers = {\n 'a': 1 }",
        "function __construct()",
        "$data = unserialize($x);",
        "msf6 > use exploit/linux/samba/is_known_pipename",
    ],
)
def test_source_fragments_are_never_handed_out_as_commands(payload: str) -> None:
    """Measured: the loose pattern that found 28 boxes also offered Go/PHP/Python/
    Java source as command #0 on five of them.  A wrong first command is worse
    than none -- it teaches the Worker the recipe is broken."""
    assert _fa_payload_line(payload, 190) == ""


def test_real_commands_survive_the_code_filter() -> None:
    for payload, expect in [
        ("java -jar ysoserial.jar CommonsCollections5 \"bash -c id\"", "java -jar"),
        ("$ go run . \"http://target/index.php\"", "$ go run"),
        ("DROP TABLE IF EXISTS cmd_exec;", "DROP TABLE"),
        ("curl -s http://target/ --data 'a=1'", "curl -s"),
        # The interpreter flag comes BEFORE the script name, so this is a
        # self-contained command, not a missing-file order.
        ("python3 -c \"import socket; print(1)\"", "python3 -c"),
    ]:
        assert _fa_payload_line(payload, 190).startswith(expect), payload


def test_a_recipe_local_script_is_not_offered() -> None:
    """Measured 2026-11-08: the Drupal CVE-2018-7602 probe was
    ``python3 drupa7-CVE-2018-7602.py -c "id" …`` -- a script that lives in the
    recipe, not in the Worker image.  The Run spent early commands on ``No such
    file or directory``.  Ordering a missing file is worse than saying nothing."""
    payload = 'python3 drupa7-CVE-2018-7602.py -c "id" user pass http://x/'
    assert _fa_payload_line(payload, 190) == ""


def test_a_tool_shipped_in_the_image_is_still_offered() -> None:
    """The filter must not be so broad that it drops real tooling."""
    # ``/home/kali/pocs/...`` is mounted in the Worker image, so an absolute path
    # is never treated as missing.
    payload = "python3 /home/kali/pocs/vulhub/git/CVE-2017-8386/poc.py http://x/"
    assert _fa_payload_line(payload, 190).startswith("python3 /home/kali")


def test_an_over_long_request_is_dropped_rather_than_cut() -> None:
    """A 190-char cut inside a URL yields a request that is syntactically valid
    and semantically wrong: the server answers 400 and the Run concludes the
    recipe does not apply.  Better to say nothing."""
    long_url = "http://t:8080/" + "a" * 400 + "?x=1"
    assert _fa_payload_line(long_url, 190) == ""


# ── target substitution ─────────────────────────────────────────────────────


def test_placeholders_are_filled_from_the_real_target() -> None:
    box = _Box("x", target_url="http://10.0.0.5:8080")
    out = _fa_substitute_target("GET http://{{target_url}}/a HTTP/1.1", box)
    assert "{{target_url}}" not in out
    assert "10.0.0.5:8080" in out


def test_unknown_target_leaves_the_placeholder_alone() -> None:
    """A guessed host is a wrong request dressed as a measured one."""
    box = _Box("no host here")
    assert "{{target_url}}" in _fa_substitute_target("GET http://{{target_url}}/", box)


# ── the two channels stay separate ──────────────────────────────────────────


def test_a_measured_recipe_uses_the_order_channel_not_the_probe() -> None:
    """The curated S2-057 recipe is ``reviewed``/measured; it must not be
    demoted into the probe channel."""
    box = _Box("[Struts2] CVE-2018-11776 Struts2 S2-057 OGNL RCE")
    assert exploit_first_action(box)
    assert exploit_probe_action(box) == ""


def test_an_unmeasured_recipe_uses_the_probe_channel_not_the_order() -> None:
    box = _Box("[Git]CVE-2017-8386")
    assert exploit_first_action(box) == "", "unmeasured recipes must not issue orders"
    probe = exploit_probe_action(box)
    assert probe, "the recipe matched and holds a runnable line -- it must not vanish"
    assert "git-upload-archive" in probe


def test_a_negative_entry_never_becomes_a_probe() -> None:
    """A wall entry carries a route to avoid, not a move to make.

    ``[GhostScript]CVE-2018-16509`` is the one box in the corpus whose only match
    is a ``kind: negative`` record, so ``exploit_probe_action`` must return "".
    """
    box = _Box("[GhostScript]CVE-2018-16509")
    assert exploit_probe_action(box) == ""


# ── observability: a probe must be visible in run_events ────────────────────


def test_probe_injection_emits_its_own_event() -> None:
    """24 of the 26 reachable UNSOLVED boxes arrive via the probe channel.  If it
    emitted nothing, an adoption audit would read all 24 as "no first action was
    available" -- the exact false negative the FIRST ACTION event exists to
    prevent."""
    from app.solver.muteki.coordinator import MutekiCoordinator
    from app.solver.muteki.events import EventType

    emitted: list[tuple[str, dict]] = []

    class _Graph:
        def emit_event(self, *, actor, event_type, payload):
            emitted.append((event_type, payload))

    coord = object.__new__(MutekiCoordinator)
    coord.graph = _Graph()
    coord.config = type("C", (), {"exploit_first_action_source": "", "exploit_first_action_verdict": ""})()

    probe = "send `GET /xxx/..;/admin/ HTTP/1.1`"
    goal = "Exploitation lane. " + probe + " then continue."
    coord._record_probe_injection(probe, goal, lane="exploit:known-path")

    assert len(emitted) == 1
    event_type, payload = emitted[0]
    assert event_type == EventType.EXPLOIT_PROBE_ACTION
    assert event_type != EventType.EXPLOIT_FIRST_ACTION, (
        "the two authorities must stay separable in a query"
    )
    assert payload["lane"] == "exploit:known-path"
    assert payload["text"] == probe
    assert payload["in_window"] is True


def test_probe_event_reports_truncation_outside_the_window() -> None:
    from app.solver.muteki.coordinator import MutekiCoordinator

    emitted: list[dict] = []

    class _Graph:
        def emit_event(self, *, actor, event_type, payload):
            emitted.append(payload)

    coord = object.__new__(MutekiCoordinator)
    coord.graph = _Graph()
    coord.config = type("C", (), {"exploit_first_action_source": "", "exploit_first_action_verdict": ""})()

    probe = "send `GET /late HTTP/1.1`"
    goal = ("x" * 700) + probe
    coord._record_probe_injection(probe, goal, lane="exploit:known-path")

    assert emitted[0]["in_window"] is False


# ── wrapped URLs and shell-prompt captures ─────────────────────────────────


def test_wrapped_url_is_joined_before_being_handed_over() -> None:
    """vulhub wraps long URLs across lines that begin with ``?`` / ``&``.  A
    single line of such a payload is a fragment, not a request."""
    payload = (
        "http://{{target_url}}/securityRealm/user/admin/descriptorByName/org.jenkinsci"
        ".plugins.scriptsecurity.sandbox.groovy.SecureGroovyScript/checkScript\n"
        "?sandbox=true\n"
        "&value=public class x {\n"
        "  public x(){\n"
        "}"
    )
    line = _fa_payload_line(payload, 400, _Box("x", target_url="http://t:8080"))
    assert line.startswith("send `GET http://t:8080/securityRealm/")
    assert "?sandbox=true&value=public class x {" in line


def test_shell_prompt_capture_is_not_a_command() -> None:
    """A copied terminal transcript is a record of somebody's session."""
    payload = "[root@VM-16-16-centos CVE-2020-1938]# docker exec -it fbe4 /bin/bash"
    assert _fa_payload_line(payload, 190) == ""


def test_join_continuation_stops_at_a_non_query_line() -> None:
    from app.services.known_exploit import _fa_join_continuation

    lines = ["http://x/a", "?b=1", "&c=2", "echo done"]
    assert _fa_join_continuation(lines, 0) == "http://x/a?b=1&c=2"
