"""The FIRST ACTION slot must hold a MOVE, never a report.

Every assertion here is written against a measurement taken on this platform on
2026-11-07 (``docs/notes/2026-11/2026-11-08-fa-adoption-diagnosis.md``):

* Of the 143 Challenges that had a FIRST ACTION available, **39 (27%)** were
  served a string a Worker cannot execute: a past-tense summary of the run that
  solved the box, a bare "confirm the version is in range", or a lead-in whose
  list the 190-char cut had already removed.
* Runs whose FIRST ACTION contained no copyable identifier sent anything
  resembling it in **4 of 54 (7%)** cases, against **78 of 124 (63%)** for runs
  whose FIRST ACTION named a path, parameter or command.
* **Zero of 178** Runs issued their FIRST ACTION as command #0, and **68%**
  opened with an orientation step (blackboard skill, ``AGENTS.md``, ``ls``)
  rather than touching the target.

The unit under test is ``_fa_is_move`` and the two functions that consume it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.known_exploit import (
    _fa_is_move,
    _first_action_candidate,
    _first_action_of,
    _first_action_repair,
    _first_action_resolved,
    exploit_first_action,
)


def _challenge(name: str, description: str = ""):
    return SimpleNamespace(
        name=name,
        description=description,
        target_url="http://target.example/",
        metadata_json={},
    )


# ── _fa_is_move: what may occupy the slot ───────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        # A past-tense summary of what a previous run saw.  This is the shape a
        # distilled ``first_action`` takes, because the solver's own summariser
        # writes it.
        "Flag found in postgres container environment via COPY FROM PROGRAM as superuser: <FLAG>",
        "Confirmed from real output: the base64 photo decoded to config.php source",
        "Flag recovered via DFS over /predict top-3 from seed CTF2{",
        "Target http://10.0.0.1:8080 is vulnerable to Python unpickle deserialization",
        "[claude] The vulnerability allows arbitrary Java code execution",
        "[codex] exploit successful against /manager",
        "已确认 couchdb 版本在 CVE-2017-12636 影响范围内",
        "目标在 Apache/2.4.18 前放置了 openresty 代理，已确认返回 404",
    ],
)
def test_a_report_of_a_previous_run_is_not_a_move(text: str) -> None:
    """The slot is the highest-authority line in the prompt.

    Spending it on ``Flag found in ...`` tells the Worker the answer to a
    question it has not been asked; it cannot be pasted anywhere.  The measured
    cost is that these slots were adopted in 7% of Runs against 63% for slots
    that named something copyable.

    ``no_identifier`` is an accepted reason here: a past-tense Chinese sentence
    that opens with the target rather than a verb is caught by the identifier
    test rather than the opener list, and both mean "the Worker cannot run this".

    ``environment_snapshot`` was added 2026-11-09: a distilled step can be dense
    in copyable tokens (``mode=``, ``/solr/``) while still being a description of
    the target rather than a move against it.  It belongs in this set -- it is
    the same judgement, reached by a sharper test.
    """

    ok, reason = _fa_is_move(text)
    assert not ok, text
    assert reason in {
        "report",
        "version_only",
        "no_identifier",
        "environment_snapshot",
    }, (text, reason)


@pytest.mark.parametrize(
    "text",
    [
        # A flag marker or the benchmark harness's tool-call envelope.  Both are
        # records of an answer, not an instruction.
        "FOUND_FLAG=<FLAG>",
        "VERIFIED_FACT=the id parameter is injectable",
        "DEADEND=no upload endpoint exists",
        '{"endpoint": "/api/verify", "method": "POST", "status_code": 200}',
    ],
)
def test_an_answer_or_tool_envelope_is_not_a_move(text: str) -> None:
    ok, reason = _fa_is_move(text)
    assert not ok, text
    assert reason == "answer_dump", (text, reason)


def test_a_lead_in_whose_list_was_cut_off_is_not_a_move() -> None:
    """``[:190]`` on ``先认轴…（下面每行一个轴，按题号选）：`` leaves a bare colon.

    The Worker receives an introduction to a list that is not there.  Eleven of
    the 143 slots looked like this, because the recipe's own step 0 is a heading
    and the rows it introduces start at step 1.
    """

    ok, reason = _fa_is_move("先认轴，**认错轴一定失败且返回正常页**（下面每行一个轴，按题号选）：")
    assert not ok
    assert reason == "dangling_lead_in"


@pytest.mark.parametrize(
    "text",
    [
        'GET /ws_utc/config.do 重试到设置页出现',
        "PUT /x.jsp/ 写入读取 /flag.txt 的 JSP",
        'curl -s -c cj.txt "$B/index.php" -o idx.html',
        "`GET /bea_wls_internal/pwn.jsp` 读回环境变量，取 `FLAG=`",
        "POST /ws_utc/resources/setting/options，带 setting_id=general",
        "打 /uddiexplorer/ 并轮询，直到返回体里出现真表单",
    ],
)
def test_a_request_line_or_shell_invocation_is_a_move(text: str) -> None:
    ok, _reason = _fa_is_move(text)
    assert ok, text


def test_a_string_that_names_no_identifier_is_not_a_move() -> None:
    """Nothing to copy means nothing to do.

    ``The server accepts .phtml file uploads`` is true and useful, but a Worker
    cannot run it.  This is the ``no_identifier`` class that was adopted in 7% of
    Runs.
    """

    ok, reason = _fa_is_move("The server accepts .phtml file uploads and executes them as PHP")
    assert not ok
    assert reason == "no_identifier"


def test_a_generic_path_does_not_count_as_an_identifier() -> None:
    """``GET /`` names every box, so it identifies none of them."""

    ok, _reason = _fa_is_move("Send a request to the home page")
    assert not ok


# ── _first_action_candidate: skipping, not truncating ───────────────────────


def test_a_report_first_action_falls_through_to_an_executable_step() -> None:
    """The entry keeps its own order; only unusable strings are skipped.

    ``first_action`` is the summariser's output, so it is the field most likely
    to hold a report.  Step 1 is the request that produced it, and that is what
    the Worker needs.
    """

    entry = {
        "first_action": "Flag found in the environment of the container: <FLAG>",
        "steps": [
            "Flag found in the environment of the container: <FLAG>",
            "GET /?file=/flag returns the flag",
        ],
    }

    action, source, verdict = _first_action_candidate(entry, 190)

    assert action == "GET /?file=/flag returns the flag"
    assert source == "step1"
    assert verdict == "command"


def test_a_lead_in_step_falls_through_to_the_row_it_introduces() -> None:
    """A heading is not the move; the row under it is."""

    entry = {
        "steps": [
            "先认轴，**认错轴一定失败且返回正常页**（下面每行一个轴，按题号选）：",
            "s2-045/046 → **Content-Type 头**，OGNL 压成一行。",
        ]
    }

    action, source, _verdict = _first_action_candidate(entry, 190)

    assert action.startswith("s2-045/046")
    assert source == "step1"


def test_an_entry_with_no_executable_string_yields_nothing() -> None:
    """Empty is a real answer: the caller falls through to the next recipe.

    Truncating a report into the slot is what the old code did, and it is worse
    than an empty slot -- an empty slot lets ``technique_candidate_goal`` supply
    a decision procedure instead.
    """

    entry = {
        "first_action": "Flag found in postgres container environment: <FLAG>",
        "steps": ["Flag found in postgres container environment: <FLAG>"],
    }

    assert _first_action_of(entry, 190) == ""
    assert _first_action_candidate(entry, 190)[0] == ""


# ── matrix entries: only this box's own advisory row ────────────────────────


def test_a_matrix_recipe_hands_the_box_its_own_advisory_row() -> None:
    """``struts2-ognl-axis-map`` covers every ``s2-0xx`` advisory, one row each.

    Reading it in file order gave an ``s2-016`` box the ``s2-045/046`` row.  That
    entry says in its own first line that the wrong axis is guaranteed to fail
    (``认错轴一定失败``), so the substitution has to select by advisory number.
    """

    from app.services.known_exploit import _pick_entries

    for title, expected in (
        ("[struts2]s2-016", "s2-016"),
        ("[struts2]s2-005", "s2-005/009"),
        ("[struts2]s2-001", "s2-001/007/008/012/013/015"),
    ):
        challenge = _challenge(title)
        action = exploit_first_action(challenge)
        assert expected in action, (title, action)
        # And it really did come from the matrix entry, not from a lucky pick.
        entry = next(e for e in _pick_entries(challenge) if e["id"] == "struts2-ognl-axis-map")
        _action, source, verdict = _first_action_resolved(entry, challenge, max_chars=190)
        assert verdict.startswith("own_advisory:"), (title, verdict)
        assert source.startswith("step"), (title, source)


def test_a_matrix_recipe_with_no_row_for_this_box_yields_nothing() -> None:
    """``s2-048`` has no row in the axis map, so it must not be given s2-045/046.

    The box still reaches its own recipe (``struts2-s2-009-…`` matches on the
    shared OGNL tokens); what it must never get is a row for a different
    advisory.
    """

    challenge = _challenge("[struts2]s2-048")
    action = exploit_first_action(challenge)

    assert "s2-045/046" not in action
    assert "s2-016" not in action


def test_a_single_advisory_recipe_keeps_its_own_step_order() -> None:
    """The matrix rule must not touch the ordinary case.

    ``struts2-s2-009-ognl-parameter-injection`` names one advisory, so its step 0
    is the right first move and reordering it would be a regression.
    """

    from app.services.known_exploit import _pick_entries

    challenge = _challenge("[struts2]s2-009")
    entry = _pick_entries(challenge)[0]
    action, source, verdict = _first_action_resolved(entry, challenge, max_chars=190)

    assert source == "step0"
    assert not verdict.startswith("own_advisory")
    assert "/nodecorate/jspEval.action" in action


# ── provenance: the telemetry event needs to be joinable ────────────────────


def test_the_repair_reports_where_the_string_came_from() -> None:
    """A silent rewrite of the highest-authority prompt line is not auditable.

    ``_first_action_repair`` returns the entry id, the field the text was read
    from and the ``_fa_is_move`` verdict, so the ``exploit.first.action`` event
    can be joined against Run outcomes later.
    """

    action, source, verdict = _first_action_repair(_challenge("[PHPMYADMIN]CVE-2018-12613"))

    assert action
    assert ":" in source, source
    # ``step:command`` / ``own_advisory:identifier`` -- the prefix says which rule
    # admitted the string, the suffix is the ``_fa_is_move`` verdict.
    assert verdict.split(":")[-1] in {"command", "identifier"}, verdict


def test_the_repair_agrees_with_the_public_function() -> None:
    """``exploit_first_action`` is what production calls; they must not diverge."""

    for title in (
        "[struts2]s2-045",
        "[struts2]s2-016",
        "[PHPMYADMIN]CVE-2018-12613",
        "[Weblogic]CVE-2018-2628",
        "[Solr]CVE-2019-0193",
        "[Ruby]CVE-2017-17405",
    ):
        challenge = _challenge(title)
        assert _first_action_repair(challenge)[0] == exploit_first_action(challenge), title


def test_a_box_with_no_recipe_reports_no_provenance() -> None:
    action, source, verdict = _first_action_repair(_challenge("[Ruby]CVE-2017-17405"))
    assert action == ""
    assert source == ""
    assert verdict == "none"


# ── environment snapshots and distilled capstones (2026-11-09) ──────────────
#
# A distilled ``first_action`` can be the summariser's capstone sentence rather
# than the step it intended.  Every "does it look like a command?" test admitted
# these, because each one embeds a copyable path, parameter or HTTP verb -- and
# a keyword blacklist added for one shape was dodged by the next.  Measured on
# the learned entry for ``[Solr]CVE-2019-0193``, whose ten steps were ALL
# observations, yet ``exploit_first_action`` served step 5 as an order.


@pytest.mark.parametrize(
    "text",
    [
        # A snapshot of what a previous run observed on the box.
        "Solr 8.1.1 standalone (mode=std) at http://x/solr/ ; solr_home=/var/solr/data, runs as user 'solr', JDK11",
        # A contrast sentence: "it failed, but this works".
        "/solr/admin/cores?action=STATUS initially returns status:{} (no cores) BUT core creation WORKS with an absolute configSet path: GET /solr/admin/cores?action=CREATE",
        # A negative observation -- obeying it would repeat a known failure.
        "Other configset names fail: basic_configs/sample_techproducts_configs do NOT exist under /opt/solr-8.1.1/server/solr/configsets/",
        # The summariser's own attribution prefix.
        "[claude] Actually in Solr standalone, /core/config POST with set",
        "[codex] RCE confirmed: Solr 8.1.1 DIH script-transformer runs `/bin/sh -c`",
        # A labelled narrative: label, then a sentence, then an embedded request.
        "rce: Solr 8.1.1 DIH script-transformer RCE works on this target — GET /solr/admin/cores?action=CREATE&name=dih",
        # The answer itself.
        "FLAG=<FLAG> found in the solr process env (uid 8983 solr, hostname ctf-target-b2cff0c571)",
    ],
)
def test_a_distilled_capstone_is_not_a_move(text: str) -> None:
    ok, _reason = _fa_is_move(text)
    assert not ok, text


@pytest.mark.parametrize(
    "text",
    [
        "send `GET /xxx/..;/admin/ HTTP/1.1`",
        "send `POST /service/extdirect HTTP/1.1`",
        'ssh -p 3322 -i id_rsa -t git@{{target_url}} "git-upload-archive \'--help\'"',
        "java -jar ysoserial.jar CommonsCollections5 \"bash -c id\"",
        "GET http://x/a?b=1&c=2 HTTP/1.1",
        "curl -s 'http://x/?id=1' --data 'a=b'",
        "枚举 core：`GET /solr/admin/cores?action=STATUS&wt=json`",
        "Login `root/root` on index.php succeeds (302).",
    ],
)
def test_a_real_move_survives_the_sharper_tests(text: str) -> None:
    """Regression guard: the added judgements must not eat legitimate steps.

    ``Login `root/root` …`` is the one that constrains the design most: it is
    past-tense prose, but it carries a concrete credential the Worker can paste,
    so it must keep passing.  That is why ``succeeds (302)`` is deliberately NOT
    a narrative marker.
    """
    ok, _reason = _fa_is_move(text)
    assert ok, text
