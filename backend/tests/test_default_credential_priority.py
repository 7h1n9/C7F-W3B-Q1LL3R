"""Default credentials must be tried in a fixed, documented order -- and be told to the Worker at all.

The failure this pins down, measured on run ``11ef6fe7`` (phpMyAdmin
CVE-2016-5734): the box is an *authenticated* RCE.  The Worker spent its entire
720s turn porting exploit-db 40185 and never made one login attempt with a
default pair, so the exploit it built had no session to run in and the Run
closed UNSOLVED.

Two independent causes, both fixed here:

1. ``_render_entry`` never rendered the recipe's ``preconditions``, and that is
   exactly the field that said "you need a login first".
2. The Race lane goal tells a Worker not to spend its window on "login
   crawling".  Correct for an anonymous box, actively wrong once a recipe
   declares authentication -- and the Worker obeys the more prominent text.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if BACKEND_ROOT not in sys.path:
    sys.path.append(BACKEND_ROOT)

from app.services.known_exploit import (  # noqa: E402
    _DEFAULT_CREDENTIALS,
    _declares_authentication,
    _render_entry,
    known_exploit_block,
)


def _challenge(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, description="", target_url="", metadata_json={})


AUTH_RECIPE = {
    "id": "demo-auth",
    "title": "Demo authenticated RCE",
    "summary": "An exploit that needs a session.",
    "technique": "command injection",
    "preconditions": ["Target requires a valid login to reach the vulnerable page."],
    "steps": ["Log in, then send the payload."],
    "payload_template": "GET /admin HTTP/1.1\nHost: TARGET\n\n",
}

ANON_RECIPE = {
    "id": "demo-anon",
    "title": "Demo anonymous SSRF",
    "summary": "No session needed.",
    "technique": "ssrf",
    "preconditions": ["The endpoint is reachable without authentication."],
    "steps": ["Send the payload."],
}


# ── the ordering contract ────────────────────────────────────────────────────


def test_the_credential_order_is_the_documented_one() -> None:
    """The order is a contract: a Worker gets one attempt per pair, in order.

    Asserting the whole tuple (not just its membership) is the point -- if the
    order drifts, the Worker starts with a pair these images do not ship.
    """

    assert _DEFAULT_CREDENTIALS[:4] == (
        ("root", "root"),
        ("root", "toor"),
        ("admin", "admin"),
        ("pma", "pma"),
    )
    assert len({pair for pair in _DEFAULT_CREDENTIALS}) == len(_DEFAULT_CREDENTIALS)


def test_a_recipe_that_needs_a_login_is_recognised() -> None:
    assert _declares_authentication(AUTH_RECIPE) is True


def test_an_anonymous_recipe_is_not_flagged() -> None:
    """Over-firing would tell an SSRF box to go guess passwords."""

    assert _declares_authentication(
        {
            "id": "x",
            "title": "SSRF",
            "summary": "The endpoint is public.",
            "preconditions": ["Reachable without authentication."],
            "steps": ["Send it."],
        }
    ) is False


# ── rendering ────────────────────────────────────────────────────────────────


def test_preconditions_are_rendered_at_all() -> None:
    """The field used to be dropped, which deleted the deciding fact."""

    block = _render_entry(AUTH_RECIPE, budget=3500)
    assert "Preconditions" in block
    assert "requires a valid login" in block


def test_an_authenticated_recipe_puts_credentials_first_with_the_exact_order() -> None:
    block = _render_entry(AUTH_RECIPE, budget=3500)
    assert "CREDENTIALS FIRST" in block
    # The rendered order must match the constant, adjacent and in sequence.
    rendered = ", ".join(f"`{u}/{p or '(empty)'}`" for u, p in _DEFAULT_CREDENTIALS)
    assert rendered in block
    assert block.index("CREDENTIALS FIRST") < block.index("Run this order")


def test_an_authenticated_recipe_overrides_the_no_login_crawl_rule() -> None:
    """The Worker must be told the generic rule does not apply here."""

    block = _render_entry(AUTH_RECIPE, budget=3500)
    assert "does not apply" in block


def test_an_anonymous_recipe_gets_no_credential_directive() -> None:
    block = _render_entry(ANON_RECIPE, budget=3500)
    assert "CREDENTIALS FIRST" not in block
    assert "`root/root`" not in block


def test_the_credential_line_survives_a_tight_budget() -> None:
    """It is rendered before the steps so truncation cannot delete it."""

    block = _render_entry(AUTH_RECIPE, budget=600)
    assert "CREDENTIALS FIRST" in block


# ── end to end through the real playbook ────────────────────────────────────


@pytest.mark.parametrize("title", ["[PHPMYADMIN]CVE-2018-12613"])
def test_shipped_authenticated_recipes_reach_the_worker_with_credentials(title: str) -> None:
    """Only recipes that actually *declare* a login in preconditions/summary.

    ``[WebLogic]CVE-2018-2894`` and ``[Weblogic]CVE-2018-2628`` used to be listed
    here on the assumption that every WebLogic box needs a console session.  Their
    own text says otherwise -- 2894's preconditions read "不需要登 console" and
    2628's summary reads "这题不是 HTTP 漏洞" -- and they were only being counted
    as authenticated because the old check also scanned ``steps``, where those
    words appear inside the denials.  A false positive there is not cosmetic: it
    puts seven password attempts at the top of a budget-limited block for a box
    that has no login form.
    """

    block = known_exploit_block(_challenge(title))
    assert "CREDENTIALS FIRST" in block
    assert "`root/root`" in block


@pytest.mark.parametrize(
    "title",
    [
        "[struts2]s2-046",
        "[Weblogic]SSRF",
        "[WebLogic]CVE-2018-2894",
        "[Weblogic]CVE-2018-2628",
        "[ThinkPHP]IN SQL INJECTION",
        "[Solr]CVE-2019-0193",
    ],
)
def test_shipped_anonymous_recipes_do_not_get_the_directive(title: str) -> None:
    block = known_exploit_block(_challenge(title))
    assert block, "these boxes must still get their recipe"
    assert "CREDENTIALS FIRST" not in block


# ── execution checklist: the two silent-empty traps ──────────────────────────


def test_the_checklist_states_the_post_body_requirement() -> None:
    """``php://input`` is empty unless the code is in the request body.

    Run ``bb145b48`` issued ``curl "url?-d+...php://input"`` with no body: the
    URL looked right and nothing executed, which reads as "the box is patched".
    """

    from app.services.known_exploit import EXECUTION_CHECKLIST

    assert "POST **body**" in EXECUTION_CHECKLIST
    assert "executes nothing" in EXECUTION_CHECKLIST


def test_the_checklist_warns_against_double_quoted_flags() -> None:
    """``system("echo $FLAG")`` returns empty because PHP eats the variable.

    Measured: every ``echo $FLAG`` on run ``bb145b48`` used double quotes and
    produced no output, so the worker concluded the flag was not in the env.
    """

    from app.services.known_exploit import EXECUTION_CHECKLIST

    assert "single" in EXECUTION_CHECKLIST
    assert "interpolates" in EXECUTION_CHECKLIST


def test_the_checklist_names_the_rotation_order() -> None:
    from app.services.known_exploit import EXECUTION_CHECKLIST

    order = ["`system`", "`passthru`", "`shell_exec`", "backtick", "`exec`", "`popen`", "`proc_open`"]
    positions = [EXECUTION_CHECKLIST.index(token) for token in order]
    assert positions == sorted(positions), positions


def test_the_checklist_has_a_stop_condition() -> None:
    """A worker must be told when to call a box mis-provisioned.

    ``[PHP]CVE-2012-1823`` burned three Runs re-searching an empty box: shell as
    www-data, no flag file, no `FLAG` in a 36-variable CGI env, `/proc/1/environ`
    root-only, stock SUID set.  Without a stop condition the fourth Run does the
    same thing with a different HTTP wrapper.
    """

    from app.services.known_exploit import EXECUTION_CHECKLIST

    assert "Stop condition" in EXECUTION_CHECKLIST
    assert "does not expose a retrievable flag" in EXECUTION_CHECKLIST
    # It must name the root-only path, or the worker will "just try harder".
    assert "/proc/1/" in EXECUTION_CHECKLIST
    # And it must ask whether root is even reachable before giving up.
    assert "no upward path" in EXECUTION_CHECKLIST.lower()


def test_the_flag_hunt_tells_the_worker_the_file_may_be_obfuscated() -> None:
    """``find -iname '*flag*'`` misses the names these boxes actually use.

    The instruction used to be a name-pattern search.  A flag file called
    ``fl4g`` (or a name with no hint at all) is invisible to it, so the worker
    reports "no flag file" from a search that could never have found one.  The
    checklist must ask for a directory listing plus a CONTENT search instead.
    """

    from app.services.known_exploit import EXECUTION_CHECKLIST

    assert "fl4g" in EXECUTION_CHECKLIST
    assert "ls -laR" in EXECUTION_CHECKLIST
    # Content search is the reliable axis: the flag text is fixed, the name is not.
    assert "grep -rIl 'CTF2{'" in EXECUTION_CHECKLIST
