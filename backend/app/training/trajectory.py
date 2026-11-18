"""Compress agent trajectories into auditable segments, with lossless backfill.

Why this module exists
----------------------
A single arena run routinely issues 400-500 commands, and the dataset row for
each command already carries a 600-character (redacted, truncated) excerpt.  A
diagnosis or a meta-policy that reads the raw run therefore burns its whole
context on recon noise and still cannot answer "what did the last command
actually say?".  Naive truncation fails the other way: it throws away the one
symptom that identifies the fault (a URL, a path, a payload, an HTTP status),
and then no amount of downstream reasoning can recover it.

So compression here is *segmentation plus artifact protection*, not
summarisation:

* commands are cut into fixed windows (``window=10`` by default) so every
  segment stays small and comparable across runs;
* each segment records its own starting knowledge, its aim, and one line per
  action;
* every *key artifact* -- URL, ``IP:PORT``, file path, HTTP status + status
  line, payload string, ``flag{...}``, ``VERIFIED_FACT=`` / ``DEADEND=``
  values -- is copied **verbatim** into the segment and never abbreviated;
* anything elided is replaced by a stable token (``<<seg:3:cmd#17>>``) that
  ``expand_placeholder`` resolves back to the original text.

The result must be able to answer follow-up questions from the compressed form
alone, which is why the tokens are keyed to the *run event index* and not to
the position inside the window: inserting or dropping a segment does not
renumber the other tokens.

Determinism
-----------
Every function here is a pure function of its arguments.  Nothing consults the
clock, the filesystem (except :func:`load_run_trajectory`, which is documented
as the single IO entry point), ``dict`` iteration order of caller data, or any
random source.  Two runs over the same input produce byte-identical text; the
regression tests rely on this to assert compression ratios.
"""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Sequence

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Events per segment.  Ten is the largest window that still keeps a segment
#: under a screenful after artifact lines are appended; it also matches the
#: cadence at which workers re-read their own plan (turn boundaries average out
#: to roughly ten calls).
DEFAULT_WINDOW = 10

#: Paths that show up in bulk from ``find /`` and package listings.  They are
#: real strings but never diagnostic: keeping them costs thousands of
#: characters and answers no question a reader would ask.
NOISY_PATH_PREFIXES = (
    "/proc/",
    "/sys/",
    "/usr/lib",
    "/usr/share",
    "/usr/include",
    "/usr/local/lib",
    "/usr/local/share",
    "/bin/",
    "/sbin/",
    "/node_modules/",
    "/var/lib/dpkg",
    "/snap/",
)

#: Path artifacts read out of an *observation* are capped per event.  A single
#: ``find`` can print hundreds; three is enough to prove where something lives,
#: and the rest stay recoverable through the result token.
MAX_RESULT_PATHS = 3

#: Characters of a command kept inline.  The rest lives behind a token.  This
#: is deliberately small: the payload itself is protected separately as an
#: artifact, so clipping the wrapper costs nothing diagnostic.
STEP_CHARS = 84

#: Characters of the first non-empty observation line kept inline.
GIST_CHARS = 48

#: A backfilled observation is cut here.  The dataset excerpt is 600 chars;
#: 4000 is ~1.5 screens and covers the long ``curl -v`` / ``nmap`` dumps that
#: actually contain the status line and the server banner.
BACKFILL_CHARS = 4000

#: Prefix length used to match a dataset command against a CLI-log command.
#: Redaction rewrites paths and hosts inside both, so the prefix is stable.
MATCH_PREFIX = 120

#: Characters of a result kept in the per-segment evidence block.
ARTIFACT_CHARS = 240


# ---------------------------------------------------------------------------
# Artifact patterns
# ---------------------------------------------------------------------------
#
# All patterns are anchored to the *leftmost* start so that extraction order
# equals reading order.  ``finditer`` over the concatenation
# ``command + "\n" + result`` yields a deterministic, per-event sequence.

URL_RE = re.compile(r"""(?i)\bhttps?://[^\s"'<>()\[\]{}]+""")

#: The corpus is already redacted, so in practice the "URL" is
#: ``{{target_url}}/index.php?id=1``: the origin is a token and the *path* is
#: the part that identifies the technique.  Treating the token plus its path
#: as the address artifact is what makes "which endpoint did this run hit"
#: answerable from the compressed form.
REDACTED_URL_RE = re.compile(r"\{\{target_url\}\}[^\s\"'<>()\[\]]*")
REDACTED_HOST_RE = re.compile(r"\{\{target_host\}\}(?::\d{1,5})?")

#: ``10.0.0.5:8080`` -- kept whole.  A bare ``10.0.0.5`` is only an artifact
#: when it is not part of a version string, so it is matched separately and
#: filtered by :func:`_looks_like_ip`.
IP_PORT_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b")
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+")

#: Filesystem paths.  Two shapes: an absolute path (``/etc/passwd``,
#: ``/home/kali/workspace/x.sh``) or a traversal-relative one (``../../flag``).
#: The leading ``(?<![\w:/.-])`` stops the regex from starting in the middle of
#: a URL (``://1.2.3.4:80/index.php``) or of a version string.
PATH_RE = re.compile(
    r"(?<![\w:/.$@%~-])(?:/(?:[\w.+@%~-]+/)*[\w.+@%~-]{2,}|(?:\.{1,2}/)+[\w.+@%~/-]{2,})"
)

#: HTTP status line and bare status codes that carry a phrase with them.
STATUS_RE = re.compile(
    r"(?i)HTTP/\d(?:\.\d)?\s+\d{3}[^\r\n]{0,48}"
    r"|\bstatus(?:_code)?\s*[=:]\s*\d{3}\b"
    r"|\b[1-5]\d{2}\s+(?:OK|Found|Not Found|Forbidden|Unauthorized|"
    r"Internal Server Error|Bad Request|Method Not Allowed|Moved Permanently)\b"
)

#: Redacted flag token from the export, plus anything that still looks like a
#: flag.  ``{{flag_pattern}}`` matters most: in this corpus the *real* flags
#: were already stripped, and the token is the only surviving evidence that a
#: flag channel exists at all.
FLAG_RE = re.compile(r"(?i)\{\{flag_pattern\}\}|\b[a-z0-9_]{0,24}(?:flag|ctf)\{[^{}\r\n]{1,160}\}")

#: Blackboard markers.  The value runs to end of line, and is kept whole --
#: truncating ``VERIFIED_FACT=`` destroys the fact it is meant to prove.
MARKER_RE = re.compile(rf"(?m)\b(?:VERIFIED_FACT|DEADEND)=[^\r\n]{{0,{ARTIFACT_CHARS}}}")

#: Payload markers: the substrings that make an argument an *attack* rather
#: than recon.  Only strong markers are listed.  Generic shell operators were
#: tried first and produced garbage ("$(date +%T) $code", "; do :; done") that
#: answered no question while costing a line per segment.
PAYLOAD_MARKERS = (
    "../",
    "..%2f",
    "%2e%2e",
    "%253f",
    "php://",
    "union select",
    "union all",
    "<script",
    "onerror=",
    "${jndi",
    "--data",
    "-d+",
    "' or ",
    '" or ',
    "system(",
    "eval(",
    "exec(",
    "bash -c",
    "nc -e",
    "chmod +x",
    "reverse shell",
    "{{flag_pattern}}",
)

#: How much text before a quoted argument counts as its context.  ``--data``
#: lives outside the quotes, so the marker search has to look backwards.
PAYLOAD_CONTEXT = 24

#: Quoted spans.  ``-lc '...'`` wrappers are unwrapped first by
#: :func:`_payload_slices`, otherwise every command would trivially match.
QUOTED_RE = re.compile(r"'([^'\r\n]{1,300})'|\"([^\"\r\n]{1,300})\"")

#: ANSI escapes: CLI logs are captured with colour on.  Stripping them keeps
#: the compressed text byte-stable across terminals.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

#: Hostnames in backfilled output.  Only well-known TLDs are masked so that
#: ``index.php`` and ``resolv.conf`` survive; the trade-off is documented in
#: the report (an exotic ccTLD could slip through).
HOST_RE = re.compile(
    r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|cn|net|org|io|edu|gov|local|internal|test|dev|cc|info|site|online|"
    r"top|xyz|me|ru|jp|kr|uk|de|app|cloud|tech|store|live|space)\b"
)

HEX_SECRET_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrajectoryEvent:
    """One command/observation pair, normalised from either input shape.

    ``index`` is the position inside the run (0-based, contiguous).  It is the
    key used in placeholder tokens, so it must not depend on windowing.
    """

    index: int
    sequence: int
    tool: str
    command: str
    result: str
    delta_facts: int = 0
    delta_dead_ends: int = 0
    facts_after: int = 0
    dead_ends_after: int = 0
    event_id: str = ""
    turn: int | None = None

    @property
    def has_result(self) -> bool:
        return bool(self.result.strip())


@dataclass(frozen=True)
class RunTrajectory:
    """All events of one run plus its labels, ready for compression."""

    run_id: str
    events: tuple[TrajectoryEvent, ...]
    challenge_id: str = ""
    group_key: str = ""
    final_status: str = ""
    accepted: bool = False
    fresh_reproduction_verified: bool = False
    sources: tuple[str, ...] = ()
    backfilled_events: int = 0

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self) -> Iterator[TrajectoryEvent]:
        return iter(self.events)

    @property
    def source_chars(self) -> int:
        """Characters the compressed form has to compete against."""

        return sum(len(e.command) + len(e.result) for e in self.events)

    def compress(self, *, window: int = DEFAULT_WINDOW) -> "CompressedTrajectory":
        return compress_trajectory(self, window=window)


@dataclass(frozen=True)
class SegmentStep:
    """One rendered action inside a segment."""

    index: int
    event_id: str
    command_head: str
    gist: str
    delta_facts: int
    delta_dead_ends: int
    command_token: str
    result_token: str
    collapsed: bool = False


@dataclass(frozen=True)
class Segment:
    index: int
    start: int
    end: int
    goal: str
    steps: tuple[SegmentStep, ...]
    artifacts: tuple[str, ...]


@dataclass(frozen=True)
class CompressedTrajectory:
    """The compressed view plus everything needed to expand it again."""

    run_id: str
    segments: tuple[Segment, ...]
    artifacts: tuple[str, ...]
    placeholders: Mapping[str, str]
    text: str
    source_chars: int
    window: int
    event_count: int
    final_status: str = ""

    @property
    def compressed_chars(self) -> int:
        return len(self.text)

    @property
    def ratio(self) -> float:
        """Compressed / source.  ``0.0`` when the run had no text at all."""

        if self.source_chars <= 0:
            return 0.0
        return self.compressed_chars / self.source_chars

    def expand_placeholder(self, token: str) -> str:
        return expand_placeholder(self, token)

    def expand_all(self) -> str:
        """Replace every token in :attr:`text` with its original text.

        This is the round-trip check: the expanded form must contain every
        command and observation verbatim, so compression is lossless even
        though it is lossy *for reading*.
        """

        return _expand_all(self.text, self.placeholders)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _event_from_mapping(row: Mapping[str, Any], index: int) -> TrajectoryEvent:
    """Accept a dataset transition row (the JSONL shape)."""

    action = row.get("action") or {}
    observation = row.get("observation") or {}
    state = row.get("state") or {}
    turn = action.get("turn")
    facts = state.get("facts")
    dead = state.get("dead_ends")
    delta_facts = int(observation.get("delta_facts") or 0)
    delta_dead = int(observation.get("delta_dead_ends") or 0)
    return TrajectoryEvent(
        index=index,
        sequence=int(row.get("sequence", index)),
        tool=str(action.get("tool") or ""),
        command=str(action.get("command") or ""),
        result=str(observation.get("result_excerpt") or ""),
        delta_facts=delta_facts,
        delta_dead_ends=delta_dead,
        facts_after=int(facts or 0) + delta_facts if facts is not None else delta_facts,
        dead_ends_after=int(dead or 0) + delta_dead if dead is not None else delta_dead,
        event_id=str(row.get("event_id") or ""),
        turn=int(turn) if isinstance(turn, int) else None,
    )


def _normalise_events(events: Iterable[Any]) -> tuple[TrajectoryEvent, ...]:
    """Coerce mappings / :class:`TrajectoryEvent` / :class:`RunTrajectory`."""

    out: list[TrajectoryEvent] = []
    for position, item in enumerate(events):
        if isinstance(item, RunTrajectory):
            out.extend(item.events)
        elif isinstance(item, TrajectoryEvent):
            out.append(item if item.index == position else _reindex(item, position))
        elif isinstance(item, Mapping):
            out.append(_event_from_mapping(item, position))
        else:  # pragma: no cover - defensive, caller error
            raise TypeError(f"unsupported event type: {type(item)!r}")
    return tuple(out)


def normalise_events(events: Iterable[Any]) -> tuple[TrajectoryEvent, ...]:
    """Public alias for :func:`_normalise_events` (used by ``diagnosis``)."""

    return _normalise_events(events)


def _reindex(event: TrajectoryEvent, index: int) -> TrajectoryEvent:    return TrajectoryEvent(
        index=index,
        sequence=event.sequence,
        tool=event.tool,
        command=event.command,
        result=event.result,
        delta_facts=event.delta_facts,
        delta_dead_ends=event.delta_dead_ends,
        facts_after=event.facts_after,
        dead_ends_after=event.dead_ends_after,
        event_id=event.event_id,
        turn=event.turn,
    )


# ---------------------------------------------------------------------------
# Artifact extraction
# ---------------------------------------------------------------------------


def _looks_like_ip(candidate: str) -> bool:
    """Reject ``1.2.3.4`` shapes that are really version numbers.

    ``6.10.2.0`` style build strings are the common false positive; a real
    address has a non-trivial last octet and no letter adjacent to it.
    """

    if SEMVER_RE.match(candidate) and candidate.count(".") >= 3:
        last = candidate.rsplit(".", 1)[-1]
        if last in {"0", "1", "2", "3"}:
            return False
    return True


def _payload_slices(command: str) -> list[str]:
    """Extract the quoted attack strings from a command, innermost first.

    The worker wraps everything in ``/bin/bash -lc '...'``; unwrapping that
    layer first is what makes the inner payload (``--data "<script>..."``)
    visible instead of the whole wrapper.

    A quoted body that *contains another quoted body* is a wrapper, not a
    payload -- keeping it would duplicate the command verbatim into the
    artifact line and undo the compression.  So wrappers are skipped and only
    the innermost quoted value survives.
    """

    text = command or ""
    match = re.match(r"^\s*(?:/bin/)?(?:ba)?sh\s+-l?c\s+(['\"])(.*)\1\s*$", text, re.S)
    if match:
        text = match.group(2)

    slices: list[str] = []
    for quoted in QUOTED_RE.finditer(text):
        body = quoted.group(1) if quoted.group(1) is not None else quoted.group(2)
        if not body:
            continue
        if QUOTED_RE.search(body):
            continue
        context = text[max(0, quoted.start() - PAYLOAD_CONTEXT): quoted.start()].lower()
        haystack = f"{context} {body.lower()}"
        if not any(marker in haystack for marker in PAYLOAD_MARKERS):
            continue
        # A quoted body that is merely a path or a flag name carries no
        # injection; requiring structure keeps ``'{{target_url}}/'`` out.
        if len(body) < 4:
            continue
        slices.append(body[:ARTIFACT_CHARS])
    return slices


def extract_artifacts(
    text: str,
    *,
    paths: bool = True,
    max_paths: int | None = None,
) -> tuple[str, ...]:
    """Return every key artifact in *text*, in reading order, de-duplicated.

    The order is by first appearance, which makes the output stable for
    ``assert rendered == golden`` style tests.  Overlapping matches are
    resolved by keeping the longer one and skipping any span already consumed:
    a URL must not also yield a path artifact for its ``/index.php`` tail.

    ``paths=False`` drops the path class entirely and ``max_paths`` caps it;
    both exist for observation text, where a single ``find /`` would otherwise
    contribute hundreds of strings and drown the compression.
    """

    body = text or ""
    if not body:
        return ()

    spans: list[tuple[int, int, str]] = []

    def add(match: re.Match[str], kind_rank: int = 0) -> None:
        value = match.group(0).strip()
        if not value:
            return
        spans.append((match.start(), match.end(), value))

    for match in URL_RE.finditer(body):
        add(match)
    for match in REDACTED_URL_RE.finditer(body):
        add(match)
    for match in REDACTED_HOST_RE.finditer(body):
        add(match)
    for match in IP_PORT_RE.finditer(body):
        add(match)
    for match in IP_RE.finditer(body):
        if _looks_like_ip(match.group(0)):
            add(match)
    for match in FLAG_RE.finditer(body):
        add(match)
    for match in MARKER_RE.finditer(body):
        add(match)
    for match in STATUS_RE.finditer(body):
        add(match)

    # Paths last: they are the noisiest, and every one whose span is already
    # covered by a URL / marker is dropped by the overlap test below.
    path_count = 0
    if paths:
        for match in PATH_RE.finditer(body):
            candidate = match.group(0)
            if len(candidate.rstrip("/")) < 2:
                continue
            if candidate.startswith(NOISY_PATH_PREFIXES):
                continue
            if candidate.count("/") == 1 and "." not in candidate:
                # A bare "/word" is almost always prose or a shell flag;
                # requiring a second segment or a dot removes ``/dev`` noise
                # without losing ``/flag``.
                if candidate not in {"/flag", "/etc/passwd", "/root/.ssh"}:
                    continue
            if max_paths is not None and path_count >= max_paths:
                break
            path_count += 1
            add(match, kind_rank=1)

    spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))

    ordered: list[str] = []
    seen: set[str] = set()
    consumed_to = -1
    for start, end, value in spans:
        if start < consumed_to:
            continue
        consumed_to = max(consumed_to, end)
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


def _artifacts_for(event: TrajectoryEvent) -> tuple[str, ...]:
    """Artifacts of one event: full extraction from the command, capped from
    the observation.  The command is authored (every path in it was a decision)
    while an observation can be a 4000-line ``find`` dump."""

    artifacts: list[str] = list(extract_artifacts(event.command))
    if event.result:
        for artifact in extract_artifacts(event.result, max_paths=MAX_RESULT_PATHS):
            if artifact not in artifacts:
                artifacts.append(artifact)
    for payload in _payload_slices(event.command):
        if payload not in artifacts:
            artifacts.append(payload)
    return tuple(artifacts)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


#: ``/bin/bash -lc '…'`` wrapper.  Every worker command is dressed in it; the
#: 14 boilerplate characters are the least informative in the whole line, so
#: the inline head shows the inner command instead.  The original string is
#: still recoverable through the command token.
WRAPPER_RE = re.compile(r"^\s*(?:/bin/|/usr/bin/)?(?:ba)?sh\s+-l?c\s+(['\"])(.*)\1\s*$", re.S)


def _one_line(text: str, limit: int) -> str:
    """First line, clipped.  Newlines would break the segment grid."""

    head = text.split("\n", 1)[0].strip()
    head = re.sub(r"\s+", " ", head)
    if len(head) > limit:
        return head[:limit].rstrip()
    return head


def _command_head(command: str, limit: int) -> str:
    """Inline rendering of a command: unwrap ``bash -lc``, first line, clipped."""

    body = (command or "").strip()
    match = WRAPPER_RE.match(body)
    if match:
        body = match.group(2).strip()
    return _one_line(body, limit)


def _gist(result: str) -> str:
    """The first informative observation line, clipped, with error emphasis.

    No summarisation model is involved: the first non-empty line is what a
    human reads first too, and keeping it verbatim means the gist can be
    grepped for later.
    """

    for line in (result or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if set(stripped) <= set("-=*# "):
            continue
        marker = "!" if _ERROR_HINT.search(stripped) else ""
        return marker + _one_line(stripped, GIST_CHARS)
    return ""


#: Cheap error detector used only to decorate the gist.  The full taxonomy
#: lives in ``diagnosis.py``; duplicating it here would couple the two modules.
_ERROR_HINT = re.compile(
    r"(?i)no such file|not found|permission denied|refused|timed out|cannot "
    r"|failed|error|syntax error|unreachable"
)


def _command_token(segment: int, index: int) -> str:
    return f"<<seg:{segment}:cmd#{index}>>"


def _result_token(segment: int, index: int) -> str:
    return f"<<seg:{segment}:res#{index}>>"


def _delta_label(event: TrajectoryEvent) -> str:
    parts: list[str] = []
    if event.delta_facts:
        parts.append(f"+{event.delta_facts}F")
    if event.delta_dead_ends:
        parts.append(f"+{event.delta_dead_ends}D")
    return " ".join(parts)


def _aim(command: str) -> str:
    """The verb that names what the step is trying to do."""

    stripped = (command or "").strip()
    if not stripped:
        return "noop"
    match = re.search(
        r"(?i)\b(curl|wget|nmap|sqlmap|gobuster|ffuf|dirb|nikto|nuclei|hydra|"
        r"python3?|php|perl|ruby|nc|ssh|docker|git|cat|grep|find|ls|cd|echo|"
        r"chmod|./[\w.-]+\.sh)\b",
        stripped,
    )
    return match.group(1).lower() if match else stripped.split()[0][:16]


# ---------------------------------------------------------------------------
# Compression
# ---------------------------------------------------------------------------


def _segments_of(count: int, window: int) -> list[tuple[int, int]]:
    return [(start, min(start + window, count)) for start in range(0, count, window)]


def _segment_goal(index: int, chunk: Sequence[TrajectoryEvent], artifacts: Sequence[str]) -> str:
    """What the segment set out to do, derived only from the data.

    Three ingredients, all cheap and all checkable: the knowledge the segment
    started with (facts / dead ends carried over from the previous segment),
    the dominant aim of its commands, and the first target-shaped artifact it
    touched.
    """

    first = chunk[0]
    aims: dict[str, int] = {}
    for event in chunk:
        key = _aim(event.command)
        aims[key] = aims.get(key, 0) + 1
    # sorted() on (-count, name) keeps ties deterministic.
    ranked = sorted(aims.items(), key=lambda item: (-item[1], item[0]))
    aim = ranked[0][0] if ranked else "noop"
    target = next(
        (
            a
            for a in artifacts
            if URL_RE.match(a) or REDACTED_URL_RE.match(a) or IP_PORT_RE.match(a)
        ),
        "",
    )
    goal = f"[seg {index}] #{first.index}-{chunk[-1].index} facts={first.facts_after} dead={first.dead_ends_after} aim={aim}"
    if target:
        goal += f" target={target}"
    return goal


def _step_lines(
    segment_index: int,
    chunk: Sequence[TrajectoryEvent],
    placeholders: dict[str, str],
    *,
    final_index: int | None,
    first_seen: dict[str, int],
    result_by_command: dict[str, str],
) -> tuple[list[str], list[SegmentStep], list[str]]:
    """Render one segment's steps.

    Two collapses keep the segment readable *and* small without losing
    information:

    * a command already issued earlier in the run renders as ``≡#11`` -- in
      this corpus 52% of commands are re-issues, and re-stating them would
      dominate the compressed text;
    * a result identical to the one already printed for the same command (or
      to the previous step) keeps only a pointer, because the bytes are
      already on the page;
    * a result short enough to survive whole inside the gist needs no token at
      all -- the gist *is* the text.

    Both keep their tokens, so nothing becomes unreachable; the pointers exist
    to save a reader from scanning the same 80 characters twice.
    """

    lines: list[str] = []
    steps: list[SegmentStep] = []
    visible: list[str] = []

    for position, event in enumerate(chunk):
        # The last *real* command of the run is rendered verbatim (see
        # ``compress_trajectory``); a trailing empty command does not qualify.
        is_last_overall = event.index == final_index
        command = event.command or ""
        previous = chunk[position - 1] if position else None

        if is_last_overall:
            head = command.strip()
        elif command.strip() and first_seen.get(command, event.index) < event.index:
            head = f"≡#{first_seen[command]}"
        else:
            head = _command_head(command, STEP_CHARS)

        clipped = len(head) < len(command.strip()) or "\n" in command.strip()
        collapsed = not is_last_overall and command.strip() and first_seen.get(
            command, event.index
        ) < event.index

        cmd_token = ""
        if clipped or not command.strip() or is_last_overall:
            cmd_token = _command_token(segment_index, event.index)
            placeholders[cmd_token] = command

        gist = ""
        res_token = ""
        result = event.result or ""
        if result.strip():
            same_as_previous = previous is not None and result == previous.result
            same_as_first = (
                collapsed and result_by_command.get(command.strip(), "") == result
            )
            if same_as_previous:
                gist = "=prev"
            elif same_as_first:
                # The identical result is already rendered under the first
                # occurrence, which this step points at.
                gist = ""
            elif "\n" not in result.strip() and len(result.strip()) <= GIST_CHARS:
                # The gist renders the whole result; a token would duplicate it.
                gist = _gist(result)
            else:
                gist = _gist(result)
                res_token = _result_token(segment_index, event.index)
                placeholders[res_token] = result

        delta = _delta_label(event)

        line = f"  #{event.index} {head}"
        if cmd_token and not collapsed:
            line += f" {cmd_token}"
        if gist:
            line += f" ⇒ {gist}"
        if delta:
            line += f" ({delta})"
        if res_token:
            line += f" {res_token}"

        lines.append(line)
        visible.append(line)
        steps.append(
            SegmentStep(
                index=event.index,
                event_id=event.event_id,
                command_head=head,
                gist=gist,
                delta_facts=event.delta_facts,
                delta_dead_ends=event.delta_dead_ends,
                command_token=cmd_token,
                result_token=res_token,
                collapsed=collapsed,
            )
        )

        if command.strip():
            first_seen.setdefault(command, event.index)
            result_by_command.setdefault(command.strip(), result)
    return lines, steps, visible


def compress_trajectory(
    events: Sequence[Any] | RunTrajectory | Iterable[Any],
    *,
    window: int = DEFAULT_WINDOW,
) -> CompressedTrajectory:
    """Compress *events* into :class:`CompressedTrajectory`.

    ``events`` accepts a :class:`RunTrajectory`, dataset transition rows
    (``dict``), or :class:`TrajectoryEvent` objects.  The last event of the run
    is always rendered verbatim: "what was the final command" is the single
    most asked follow-up, and clipping it would make the compressed form
    useless for exactly the case it is built for.
    """

    if window < 1:
        raise ValueError("window must be >= 1")

    trajectory = events if isinstance(events, RunTrajectory) else None
    ordered = _normalise_events(events)
    placeholders: dict[str, str] = {}
    artifacts_seen: dict[str, None] = {}
    #: Artifacts already visible anywhere in the rendered text.  Mirrored per
    #: segment would repeat every hot URL 50 times -- a URL established in
    #: segment 1 is not re-stated in segment 40, and the compressed form is
    #: read as one document, so one occurrence is enough for zero loss.
    already_visible: dict[str, None] = {}
    first_seen: dict[str, int] = {}
    result_by_command: dict[str, str] = {}
    segments: list[Segment] = []

    header = (
        f"run {trajectory.run_id if trajectory else '-'} "
        f"status={trajectory.final_status if trajectory else '?'} "
        f"events={len(ordered)} window={window}"
    )
    lines: list[str] = [header]

    if ordered:
        bounds = _segments_of(len(ordered), window)
        final_index = next(
            (
                event.index
                for event in reversed(ordered)
                if event.command.strip() and len(event.command.strip()) <= 400
            ),
            None,
        )
        for segment_index, (start, end) in enumerate(bounds, start=1):
            chunk = ordered[start:end]
            segment_artifacts: dict[str, None] = {}
            for event in chunk:
                for artifact in _artifacts_for(event):
                    segment_artifacts.setdefault(artifact, None)
                    artifacts_seen.setdefault(artifact, None)
                    # Register even when the artifact survives inline: the
                    # token map is the authoritative index of the run.
                    placeholders.setdefault(_artifact_token(segment_index, event.index, artifact), artifact)

            step_lines, steps, visible = _step_lines(
                segment_index,
                chunk,
                placeholders,
                final_index=final_index,
                first_seen=first_seen,
                result_by_command=result_by_command,
            )
            goal = _segment_goal(segment_index, chunk, tuple(segment_artifacts))
            visible_blob = "\n".join(visible)

            missing = [
                a
                for a in segment_artifacts
                if a not in visible_blob and a not in already_visible
            ]
            lines.append(goal)
            lines.extend(step_lines)
            if missing:
                # One line, pipe-separated: the smallest possible form that
                # still keeps every artifact byte-exact.
                lines.append("  art: " + " | ".join(missing))

            for artifact in segment_artifacts:
                if artifact in visible_blob:
                    already_visible.setdefault(artifact, None)
            for artifact in missing:
                already_visible.setdefault(artifact, None)

            segments.append(
                Segment(
                    index=segment_index,
                    start=chunk[0].index,
                    end=chunk[-1].index,
                    goal=goal,
                    steps=tuple(steps),
                    artifacts=tuple(segment_artifacts),
                )
            )

    text = "\n".join(lines)
    return CompressedTrajectory(
        run_id=trajectory.run_id if trajectory else "",
        segments=tuple(segments),
        artifacts=tuple(artifacts_seen),
        placeholders=placeholders,
        text=text,
        source_chars=trajectory.source_chars if trajectory else sum(
            len(e.command) + len(e.result) for e in ordered
        ),
        window=window,
        event_count=len(ordered),
        final_status=trajectory.final_status if trajectory else "",
    )


def _artifact_token(segment: int, index: int, artifact: str) -> str:
    """Token for a single artifact, used as a secondary index.

    It is not rendered (artifacts are, by definition, present verbatim), but
    it lets a reader who only has the artifact string recover which event it
    came from: ``expand_placeholder(traj, "<<seg:2:art#17>>")``.
    """

    return f"<<seg:{segment}:art#{index}>>"


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------


TOKEN_RE = re.compile(r"<<seg:\d+:(?:cmd|res|art)#\d+>>")


def expand_placeholder(trajectory: CompressedTrajectory, token: str) -> str:
    """Return the original text behind *token*.

    Raises :class:`KeyError` for a token that was never emitted by this
    trajectory -- a silently empty expansion would look like data loss.
    """

    try:
        return trajectory.placeholders[token]
    except KeyError as exc:
        raise KeyError(f"unknown placeholder: {token}") from exc


def _expand_all(text: str, placeholders: Mapping[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        return placeholders.get(match.group(0), match.group(0))

    return TOKEN_RE.sub(repl, text)


def expand_text(trajectory: CompressedTrajectory, text: str) -> str:
    """Expand every token inside an arbitrary *text* (a prompt, a question)."""

    return _expand_all(text, trajectory.placeholders)


def artifact_coverage(trajectory: CompressedTrajectory) -> tuple[list[str], list[str]]:
    """``(present, missing)`` artifacts measured against the source events.

    The compressed text is *supposed* to contain every artifact verbatim; this
    is the assertion the tests (and the CLI) use to prove it, rather than
    trusting the construction.
    """

    present: list[str] = []
    missing: list[str] = []
    for artifact in trajectory.artifacts:
        if artifact in trajectory.text:
            present.append(artifact)
        else:
            missing.append(artifact)
    return present, missing


# ---------------------------------------------------------------------------
# IO: the only impure entry points, kept at the bottom on purpose
# ---------------------------------------------------------------------------


def _resolve_dataset(data_dir: str, name: str) -> str:
    """Pick the single ``name-*.jsonl`` in *data_dir* deterministically."""

    pattern = os.path.join(data_dir, f"{name}-*.jsonl")
    candidates = sorted(glob.glob(pattern))
    if not candidates:
        raise FileNotFoundError(f"no {name}-*.jsonl under {data_dir}")
    return candidates[-1]


def _iter_jsonl(path: str, needle: str = "") -> Iterator[dict]:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if needle and needle not in line:
                continue
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _redact(text: str) -> str:
    """Apply the dataset's own redaction to freshly read source text.

    Backfilling from CLI logs would otherwise re-introduce exactly what the
    export removed (target origin, real workspace path, flags).  The function
    is imported lazily so that compression stays importable without the rest
    of the backend.
    """

    from app.training.transitions import redact_public_text

    return redact_public_text(text)


def _mask_backfilled(text: str) -> str:
    """Redact a backfilled excerpt, including bare hosts and long hex blobs.

    ``redact_public_text`` already removes URL origins and flag shapes.  Two
    shapes still leak through in raw CLI output: ``Trying direct-ctf2.dasctf.com
    (...) port 28075`` prints the host *without* a scheme, and a flag whose
    prefix is unknown can arrive as a 32-hex digest.  Both are masked here.
    """

    value = _redact(text)
    value = IP_RE.sub("{{target_host}}", value)
    value = HOST_RE.sub("{{target_host}}", value)
    value = HEX_SECRET_RE.sub("{{secret_value}}", value)
    return value


def _parse_cli_log(path: str) -> list[tuple[str, str]]:
    """Extract ``(command, output)`` pairs from one CLI conversation log.

    Two engines write these logs and they disagree on shape:

    * codex: ``{"type":"item.completed","item":{"type":"command_execution",
      "command":…,"aggregated_output":…}}``
    * claude: ``assistant`` message with a ``tool_use`` block, answered by a
      ``user`` message carrying ``tool_result`` for the same id.

    Both are read in file order, which is chronological because each log is
    one session.
    """

    pairs: list[tuple[str, str]] = []
    pending: dict[str, str] = {}
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"command_execution"' not in line and '"tool_use"' not in line and '"tool_result"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue

            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") == "command_execution":
                if obj.get("type") != "item.completed":
                    continue
                pairs.append((str(item.get("command") or ""), str(item.get("aggregated_output") or "")))
                continue

            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") in {"Bash", "bash", "shell"}:
                    body = block.get("input") or {}
                    command = str(body.get("command") or "")
                    if command:
                        pending[str(block.get("id") or "")] = command
                        pairs.append((command, ""))
                elif block.get("type") == "tool_result":
                    identifier = str(block.get("tool_use_id") or "")
                    command = pending.get(identifier)
                    if command is None:
                        continue
                    output = block.get("content")
                    if isinstance(output, list):
                        output = "\n".join(
                            str(part.get("text") or "")
                            for part in output
                            if isinstance(part, dict)
                        )
                    # The pair was appended with an empty output when the call
                    # started; fill that same slot so ordering is preserved.
                    for position in range(len(pairs) - 1, -1, -1):
                        if pairs[position][0] == command and not pairs[position][1]:
                            pairs[position] = (command, str(output or ""))
                            break
    return pairs


def _log_pairs(run_id: str, workspace_dir: str) -> tuple[list[tuple[str, str]], list[str]]:
    directory = os.path.join(workspace_dir, run_id, "muteki", "evidence", "cli-conversations")
    if not os.path.isdir(directory):
        return [], []
    paths = sorted(glob.glob(os.path.join(directory, "*.log")))
    pairs: list[tuple[str, str]] = []
    used: list[str] = []
    for path in paths:
        try:
            found = _parse_cli_log(path)
        except OSError:
            continue
        if found:
            used.append(path)
            pairs.extend(found)
    return pairs, used


def _trajectory_from_rows(
    run_id: str,
    rows: Sequence[dict],
    path: str,
) -> RunTrajectory:
    """Build a :class:`RunTrajectory` from raw dataset rows, in sequence order."""

    ordered = sorted(
        rows, key=lambda row: (int(row.get("sequence") or 0), str(row.get("event_id") or ""))
    )
    labels = ordered[0].get("labels") or {}
    return RunTrajectory(
        run_id=run_id,
        challenge_id=str(ordered[0].get("challenge_id") or ""),
        group_key=str(ordered[0].get("group_key") or ""),
        final_status=str(labels.get("final_status") or ""),
        accepted=bool(labels.get("accepted")),
        fresh_reproduction_verified=bool(labels.get("fresh_reproduction_verified")),
        events=tuple(_event_from_mapping(row, index) for index, row in enumerate(ordered)),
        sources=(path,),
    )


def _backfill_events(
    events: Sequence[TrajectoryEvent],
    run_id: str,
    workspace_dir: str,
) -> tuple[list[TrajectoryEvent], int, list[str]]:
    """Extend ``result_excerpt`` from the raw CLI logs.  Returns (events, count,
    log files used).

    Matching is by redacted command prefix: redaction rewrites paths and hosts
    inside both sides, so the prefix is the only stable join key between the
    dataset row and the log line.  When several sessions ran the same command,
    the longest output wins -- deterministic, and usually the attempt that
    actually produced a result.
    """

    pairs, used = _log_pairs(run_id, workspace_dir)
    if not pairs:
        return list(events), 0, []

    index: dict[str, list[str]] = {}
    for command, output in pairs:
        if not command or not output:
            continue
        index.setdefault(_safe_prefix(command), []).append(output)

    rebuilt: list[TrajectoryEvent] = []
    backfilled = 0
    for event in events:
        candidates = index.get(_safe_prefix(event.command))
        if not candidates:
            rebuilt.append(event)
            continue
        best = max(candidates, key=len)
        if len(best.strip()) <= len(event.result.strip()):
            rebuilt.append(event)
            continue
        try:
            extended = _mask_backfilled(ANSI_RE.sub("", best))
        except Exception:  # pragma: no cover - backend import failure
            rebuilt.append(event)
            continue
        backfilled += 1
        rebuilt.append(
            TrajectoryEvent(
                index=event.index,
                sequence=event.sequence,
                tool=event.tool,
                command=event.command,
                result=extended[:BACKFILL_CHARS],
                delta_facts=event.delta_facts,
                delta_dead_ends=event.delta_dead_ends,
                facts_after=event.facts_after,
                dead_ends_after=event.dead_ends_after,
                event_id=event.event_id,
                turn=event.turn,
            )
        )
    return rebuilt, backfilled, used


def _safe_prefix(command: str) -> str:
    try:
        return _redact(command)[:MATCH_PREFIX]
    except Exception:  # pragma: no cover - backend import failure
        return (command or "")[:MATCH_PREFIX]


def load_run_trajectory(
    run_id: str,
    *,
    data_dir: str = "data/training",
    workspace_dir: str = "data/workspaces",
    backfill: bool = True,
    transitions_path: str | None = None,
) -> RunTrajectory:
    """Assemble one run's trajectory from the exported JSONL (+ raw logs).

    The dataset row is canonical: it is already redacted and every delta is
    authoritative.  Raw CLI logs are used for one thing only -- extending a
    ``result_excerpt`` that the export cut at 600 characters -- and their text
    is passed through the same redaction before it is allowed in.
    """

    path = transitions_path or _resolve_dataset(data_dir, "transitions")
    needle = f'"{run_id}"'
    rows = [row for row in _iter_jsonl(path, needle) if row.get("run_id") == run_id]
    if not rows:
        raise LookupError(f"run {run_id} not found in {path}")

    trajectory = _trajectory_from_rows(run_id, rows, path)
    if not backfill:
        return trajectory

    events, backfilled, used = _backfill_events(trajectory.events, run_id, workspace_dir)
    return RunTrajectory(
        run_id=trajectory.run_id,
        challenge_id=trajectory.challenge_id,
        group_key=trajectory.group_key,
        final_status=trajectory.final_status,
        accepted=trajectory.accepted,
        fresh_reproduction_verified=trajectory.fresh_reproduction_verified,
        events=tuple(events),
        sources=trajectory.sources + tuple(used),
        backfilled_events=backfilled,
    )


def load_run_trajectories(
    run_ids: Iterable[str],
    *,
    data_dir: str = "data/training",
    workspace_dir: str = "data/workspaces",
    backfill: bool = True,
) -> dict[str, RunTrajectory]:
    """Batch variant that reads the transitions file once.

    ``load_run_trajectory`` re-reads the 68 MB export per call; a diagnosis
    sweep over ten runs would spend more time in ``json.loads`` than in
    analysis.  Backfill is applied in the same pass, for the same reason.
    """

    wanted = {str(rid) for rid in run_ids}
    path = _resolve_dataset(data_dir, "transitions")
    grouped: dict[str, list[dict]] = {rid: [] for rid in wanted}
    for row in _iter_jsonl(path):
        rid = row.get("run_id")
        if rid in wanted:
            grouped[rid].append(row)

    out: dict[str, RunTrajectory] = {}
    for rid in sorted(grouped):
        rows = grouped[rid]
        if not rows:
            continue
        trajectory = _trajectory_from_rows(rid, rows, path)
        if not backfill:
            out[rid] = trajectory
            continue
        events, backfilled, used = _backfill_events(trajectory.events, rid, workspace_dir)
        out[rid] = RunTrajectory(
            run_id=trajectory.run_id,
            challenge_id=trajectory.challenge_id,
            group_key=trajectory.group_key,
            final_status=trajectory.final_status,
            accepted=trajectory.accepted,
            fresh_reproduction_verified=trajectory.fresh_reproduction_verified,
            events=tuple(events),
            sources=trajectory.sources + tuple(used),
            backfilled_events=backfilled,
        )
    return out


def run_ids_in(data_dir: str = "data/training") -> list[str]:
    """Every run id that has at least one transition, in file order."""

    path = _resolve_dataset(data_dir, "transitions")
    seen: dict[str, None] = {}
    for row in _iter_jsonl(path):
        rid = row.get("run_id")
        if isinstance(rid, str):
            seen.setdefault(rid, None)
    return list(seen)
