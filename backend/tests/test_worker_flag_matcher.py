"""The A-path Worker must accept the flag banner the challenge actually uses.

``OpenAICompatibleWorker._flag_matcher`` is the only gate between "the Worker saw
the real flag" and "the run records a candidate": ``_submit_flag`` rejects
anything whose shape does not match, with the message "does not have the flag's
shape. Submit the exact string the target returned" -- which reads like a model
error, not a host error, so the model keeps re-submitting and the run ends
UNSOLVED with an empty ledger.

These tests pin the contract: the shape comes from the challenge, whatever the
challenge calls that field.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from app.solver.muteki.worker.openai_compatible import OpenAICompatibleWorker


def _worker_with(challenge: object) -> OpenAICompatibleWorker:
    worker = object.__new__(OpenAICompatibleWorker)
    worker.graph = SimpleNamespace(_challenge=challenge)
    return worker


def test_orm_challenge_column_flag_pattern_is_honoured():
    """The host graph carries the ORM Challenge (``flag_pattern``)."""

    challenge = SimpleNamespace(flag_pattern=r"CTF2\{[^}]+\}")
    matcher = _worker_with(challenge)._flag_matcher()
    assert matcher.fullmatch("CTF2{deadbeef}")
    assert not matcher.fullmatch("flag{deadbeef}")


def test_upstream_projection_field_flag_format_still_wins():
    challenge = SimpleNamespace(flag_format=r"XXCTF\{[^}]+\}", flag_pattern=r"ignored")
    matcher = _worker_with(challenge)._flag_matcher()
    assert matcher.fullmatch("XXCTF{1}")
    assert not matcher.fullmatch("ignored")


def test_no_declared_shape_keeps_the_legacy_banner():
    matcher = _worker_with(SimpleNamespace())._flag_matcher()
    assert matcher.fullmatch("flag{abc}")
    assert not matcher.fullmatch("CTF2{abc}")


def test_token_mode_does_not_compile_the_literal_word():
    """``flag_format == "token"`` means "no wrapper", not "the word token"."""

    matcher = _worker_with(SimpleNamespace(flag_format="token"))._flag_matcher()
    assert isinstance(matcher, re.Pattern)
    assert matcher.fullmatch("flag{abc}")
