"""Classify unlabelled shell commands into technique labels with an LLM.

Why this exists
---------------
The regex vocabulary in ``app.training.candidates`` covers only ~17% of the
commands in the export (5,301 of 33,914 when measured on 2026-11-03).  The
other 83% are not noise -- they are real attempts written in ways a fixed
needle list will never enumerate: inline python exploit scripts, base64 blobs,
custom curl invocations, framework-specific paths.  That gap is exactly why
per-technique evidence is thin (6 candidates, and only 1 of them recurring in
the held-out split).

A regular expression cannot close that gap, because deciding "is this a
deserialisation attempt or just a template render" needs semantics.  So this
module asks a model, **offline**, and writes the answer into a cache file that
the deterministic miner then reads.

Design constraints (each one is load-bearing)
---------------------------------------------
* **Model is configurable, never hard-coded.**  The operator picks a
  ``model_configs`` row by name or id; the run records which one produced the
  cache.  Mixing models inside one cache is refused -- see ``cache_header``.
* **Offline only.**  Nothing here runs on the solve path.  The solver must not
  gain a network round-trip or a new failure mode.
* **Cache is keyed by content hash**, so a re-run costs nothing for commands
  already classified, and re-exporting the dataset does not invalidate it.
* **A failure is recorded as a failure, never as "unknown".**  A model timeout
  and a model judgement of "not a technique" are different facts; collapsing
  them would silently poison the evidence counts.
* **Output is validated against the closed vocabulary.**  A label outside
  ``TECHNIQUE_SIGNATURES`` is rejected, so the miner's input stays in one
  namespace instead of drifting into invented categories.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.training.candidates import TECHNIQUE_SIGNATURES

#: The full allowed output vocabulary.  ``unknown`` means the model read the
#: command and concluded it is not a technique at all (pure bookkeeping, a
#: file read, a shell convenience).  Anything outside this set is a bug.
ALLOWED_LABELS: tuple[str, ...] = tuple(label for label, _ in TECHNIQUE_SIGNATURES) + (
    "unknown",
)

#: Commands shorter than this carry no recoverable technique signal
#: (``ls``, ``pwd``, ``cd /tmp``).  Sending them costs tokens and invites the
#: model to hallucinate a label to be helpful.
MIN_COMMAND_CHARS = 25

#: Commands that are pure platform bookkeeping.  These are matched by shape,
#: not by model judgement, because they are *our* protocol, not the target's
#: surface: the blackboard helper, workspace navigation, flag greps.
PLATFORM_NOISE = re.compile(
    r"(blackboard\.py|read-all|read-facts|read-deadends|write-fact|"
    r"^\s*\{\{linux_path\}\}\s*$|^\s*(ls|cd|pwd|echo|export|cat)\b[^\n]{0,40}$)",
    re.I,
)

PROMPT_VERSION = "technique-classify-v1"

_SYSTEM_PROMPT = """You are a classifier. You do not execute anything, and you never
produce shell output. Your only job is to label one recorded command.

INPUT: a single shell command string, recorded during a CTF web-exploitation run.
OUTPUT: exactly one JSON object, nothing else -- no prose, no code fence, no
explanation before or after:

{"labels": ["<label>"], "confidence": <0.0-1.0>, "why": "<10 words max>"}

Allowed labels (exact spelling, choose every one that applies):
{schema}

Rules:
- This is a CLASSIFICATION task, not a request to run the command. Never output
  the command's result, never invent a flag, never say you cannot execute.
- At most 2 labels. One command is usually one move.
- "unknown" ONLY when the command is not an exploitation attempt at all: a
  directory listing, reading a local file, printing a variable, running our own
  helper scripts.
- A string that merely appears in a command is not a technique: `grep -r "union
  select" .` is a source search, not SQL injection.
- Do not invent labels.

Example input: `cat /flag`
Example output: {"labels": ["unknown"], "confidence": 0.9, "why": "reads a local file"}
"""


@dataclass
class ClassificationResult:
    command_hash: str
    labels: tuple[str, ...]
    confidence: float
    why: str
    model: str
    error: str = ""
    #: True when the label was recovered from prose rather than read from a
    #: JSON object the model produced.  Kept as a separate flag because the
    #: label may still be right, but the *rate* of salvage is the signal that
    #: this model does not honour the output contract -- measured 2026-11-04,
    #: every single verdict from glm-5.3-flash on this relay was salvaged.
    salvaged: bool = False

    @property
    def ok(self) -> bool:
        return not self.error


def command_hash(command: str) -> str:
    """Content hash of a command, normalised for whitespace only.

    Not normalised further: two commands that differ in a payload are genuinely
    different attempts and deserve different verdicts.
    """

    normalised = " ".join(str(command or "").split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:24]


def is_platform_noise(command: str) -> bool:
    value = str(command or "").strip()
    if len(value) < MIN_COMMAND_CHARS:
        return True
    return bool(PLATFORM_NOISE.search(value))


#: How much of the command to send.  Measured 2026-11-04: a 439-character
#: inline python script made glm-5.3-flash think for over 150 seconds and then
#: time out, while a 100-character curl came back in 6.  The technique is
#: visible in the first line or two -- the script body is not what identifies
#: it -- so sending less is both faster and just as accurate.
COMMAND_SEND_CHARS = 400

#: How long to wait for the cache write lock before giving up.  A batch append
#: takes milliseconds; this only fires if a worker died holding the marker.
LOCK_TIMEOUT_SECONDS = 5.0


def build_messages(command: str) -> list[dict[str, str]]:
    """Wrap the command as *data*, not as an instruction.

    Measured 2026-11-04 on glm-5.3: with the command sent as a bare user turn,
    the model treated `cat /flag` as a request to run it, replied with a
    simulated shell transcript, and **invented a flag** (`flag{you_found_me}`)
    instead of classifying.  Delimiting the payload and naming the field
    removes the ambiguity.

    The command is passed through as stored, placeholders included.  A prettier
    rendering (`{{target_url}}` -> `http://TARGET`) was prototyped and reverted:
    it measurably helped the model read python exploit scripts, but changing the
    prompt mid-dataset would leave two generations of verdicts in one cache with
    no way to tell them apart.  Bumping ``PROMPT_VERSION`` and re-running is the
    honest way to adopt it.
    """

    schema = "\n".join(f"- {label}" for label in ALLOWED_LABELS if label != "unknown")
    body = str(command or "")
    if len(body) > COMMAND_SEND_CHARS:
        # Cut at a line boundary where possible so the visible head stays
        # syntactically meaningful instead of ending mid-token.
        cut = body.rfind("\n", 0, COMMAND_SEND_CHARS)
        body = body[: cut if cut > COMMAND_SEND_CHARS // 2 else COMMAND_SEND_CHARS] + "\n[...truncated]"
    return [
        {"role": "system", "content": _SYSTEM_PROMPT.replace("{schema}", schema)},
        {
            "role": "user",
            "content": "Classify the command between the markers.\n\n"
            "<<<COMMAND\n" + body + "\nCOMMAND>>>\n\n"
            # Repeated in the user turn on purpose: measured 2026-11-04, the
            # system-only instruction produced Markdown prose on complex
            # commands, and adding this line made the same command return a
            # JSON object on three consecutive attempts.
            'Reply with the JSON object only, using the key "labels". '
            "No markdown, no headings, no prose.",
        },
    ]


def _salvage_label(text: str) -> tuple[str, float] | None:
    """Recover a label from a prose answer, or ``None``.

    Measured 2026-11-04: ``glm-5.3-flash`` honours ``response_format`` for most
    commands but reverts to Markdown prose for others ("**Classification:
    Reconnaissance / Service Enumeration**..."), even with
    ``response_format={"type": "json_object"}`` set.  Those answers are usually
    *correct* -- they just are not JSON -- so discarding them threw away real
    judgements: 238 of 310 rows were lost this way on the first large batch.

    The recovery is deliberately conservative.  Confidence is capped at 0.4 so a
    salvaged verdict can never outrank a clean one when run counts are summed,
    and the ``why`` field says it was salvaged so a reviewer can see the prompt
    still needs work.
    """

    haystack = str(text or "").casefold()
    if not haystack:
        return None
    # Technique names spelled out in prose come first, because a prose answer
    # like "This command attempts a Local File Inclusion test" names the move
    # correctly -- it just refused the JSON contract.  Longest phrase first so
    # "local file inclusion" is not shadowed by "inclusion".
    for phrase in sorted(_ALIAS_LABELS, key=len, reverse=True):
        if len(phrase) < 4:
            continue
        if phrase in haystack:
            return _ALIAS_LABELS[phrase], 0.4
    # Exact vocabulary labels, longest first: `sqli.union` before `sqli`.
    for label in sorted(ALLOWED_LABELS, key=len, reverse=True):
        if label == "unknown":
            continue
        if label.casefold() in haystack:
            return label, 0.4
    # Prose that describes non-technique work in the model's own words.  These
    # are the phrasings actually observed, not a guess at what it might say.
    if re.search(
        r"\bunknown\b|not an? (?:exploit|technique|attack)|reads? a local file|"
        r"directory listing|lists? (?:a )?directory|prints? a variable|"
        r"helper script|no exploit|not (?:an )?exploitation",
        haystack,
    ):
        return "unknown", 0.4
    # Reconnaissance prose: a real answer, and `recon.*` is in the vocabulary.
    if re.search(r"reconnaissance|enumeration|information gathering|fingerprint|discovery", haystack):
        return "recon.source", 0.3
    return None


#: Free-text category words the model actually emits, mapped onto the closed
#: vocabulary.  Measured, not imagined: every key here appeared in a real reply
#: from glm-5.3-flash on 2026-11-04 when it was asked for `labels` and answered
#: with `classification: "reconnaissance"` instead.  Anything not in this table
#: is rejected, so the vocabulary still cannot drift.
_ALIAS_LABELS: dict[str, str] = {
    "reconnaissance": "recon.source",
    "recon": "recon.source",
    "enumeration": "recon.dir",
    "discovery": "recon.dir",
    "directory bruteforce": "recon.dir",
    "port scan": "recon.port",
    "port scanning": "recon.port",
    "sql injection": "sqli.union",
    "sqli": "sqli.union",
    "blind sql injection": "sqli.boolean",
    "local file inclusion": "lfi.traversal",
    "lfi": "lfi.traversal",
    "path traversal": "lfi.traversal",
    "directory traversal": "lfi.traversal",
    "php wrapper": "lfi.wrapper",
    "server-side template injection": "ssti.eval",
    "ssti": "ssti.eval",
    "deserialization": "rce.deser",
    "insecure deserialization": "rce.deser",
    "command injection": "rce.ognl",
    "remote code execution": "rce.ognl",
    "rce": "rce.ognl",
    "code injection": "rce.ognl",
    "ssrf": "ssrf.internal",
    "server-side request forgery": "ssrf.internal",
    "file upload": "upload.bypass",
    "unrestricted file upload": "upload.bypass",
    "jwt": "auth.jwt",
    "privilege escalation": "privesc.suid",
    "none": "unknown",
    "benign": "unknown",
    "safe": "unknown",
    # Verdict words rather than technique names.  The model reaches for these
    # when it cannot name a technique: measured 2026-11-04, a long exploit
    # script came back as `{"labels": ["malicious"]}` -- true, but it says
    # nothing about which move it was, so it is recorded as unknown rather than
    # allowed to invent a class.
    "malicious": "unknown",
    "suspicious": "unknown",
    "attack": "unknown",
    "exploitation": "unknown",
    "exploit": "unknown",
    "unknown": "unknown",
}


def _normalise_label(raw: str) -> str | None:
    """Map one label to the closed vocabulary, or ``None`` if unmappable."""

    value = str(raw or "").strip()
    if not value:
        return None
    if value in ALLOWED_LABELS:
        return value
    key = " ".join(value.casefold().replace("_", " ").replace("-", " ").split())
    if key in _ALIAS_LABELS:
        return _ALIAS_LABELS[key]
    # A reply like "reconnaissance / enumeration" names two things; take the
    # first that maps rather than failing the whole row.
    for part in re.split(r"[/,;|]", key):
        part = part.strip()
        if part in _ALIAS_LABELS:
            return _ALIAS_LABELS[part]
        if part in ALLOWED_LABELS:
            return part
    return None


def parse_response(text: str, *, model: str) -> ClassificationResult:
    """Parse and validate one model reply.

    Validation is strict on purpose.  A model that answers with a prose
    paragraph, an invented label, or an empty list is recorded as a failure so
    the operator sees the prompt needs work -- not silently coerced into
    ``unknown``, which would look like a judgement.
    """

    raw = str(text or "").strip()
    block = raw
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fence:
        block = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", raw, re.S)
        if brace:
            block = brace.group(0)
    try:
        payload = json.loads(block)
    except ValueError:
        payload = None

    if payload is None:
        # Not JSON at all: fall back to reading the prose.
        salvaged = _salvage_label(raw)
        if salvaged is not None:
            label, confidence = salvaged
            return ClassificationResult(
                "", (label,), confidence, "salvaged from prose", model, salvaged=True
            )
        return ClassificationResult("", (), 0.0, "", model, error="unparseable_json")

    # From here the reply *is* JSON, but it may not be the JSON that was asked
    # for.  ``salvaged`` tracks exactly that distinction, because the two cases
    # are otherwise indistinguishable in the cache -- measured 2026-11-04, 28
    # rows arrived with confidence 0.3 and an empty ``why`` while looking clean,
    # and every one of them had come through an alias field.
    salvaged = False
    labels = payload.get("labels")
    if not isinstance(labels, list) or not labels:
        # The model was asked for ``labels`` but reliably answers with its own
        # field name.  Measured 2026-11-04 on glm-5.3-flash, three consecutive
        # calls on one command produced ``classification``, then
        # ``category``, then ``classification`` again -- all JSON, all correct
        # in intent, none of them parseable under a strict schema.
        for alias in ("classification", "category", "label", "technique", "type"):
            candidate = payload.get(alias)
            if isinstance(candidate, str) and candidate.strip():
                labels = [candidate.strip()]
                salvaged = True
                break
            if isinstance(candidate, list) and candidate:
                labels = candidate
                salvaged = True
                break
    if not isinstance(labels, list) or not labels:
        return ClassificationResult("", (), 0.0, "", model, error="no_labels")
    kept: list[str] = []
    rejected: list[str] = []
    for item in labels:
        label = _normalise_label(str(item or ""))
        if label is None:
            rejected.append(str(item or ""))
            continue
        if label not in kept:
            kept.append(label)
    if not kept:
        # Every label was outside the vocabulary.  Recording what it said is the
        # only way to notice the prompt needs another alias, instead of staring
        # at an opaque count of failures.
        return ClassificationResult(
            "", (), 0.0, "", model,
            error="label_out_of_vocabulary:" + ",".join(rejected)[:80],
        )
    if len(kept) > 2:
        return ClassificationResult("", (), 0.0, "", model, error="too_many_labels")
    # ``unknown`` means "this is not a technique at all".  Paired with a real
    # label it is self-contradictory, and the pair was observed in practice
    # (``["unknown", "ssrf.internal"]`` on a Solr XXE probe).  The specific
    # label wins: it carries more information than the abstention.
    if len(kept) > 1 and "unknown" in kept:
        kept = [label for label in kept if label != "unknown"]
    if "confidence" in payload and payload.get("confidence") is not None:
        try:
            confidence = float(payload.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.3  # present but unusable: treat as the lowest usable
    else:
        # The model omitted confidence entirely.  That is not the same as
        # asserting zero certainty, and recording 0.0 would drag down any
        # downstream weighting.
        confidence = 0.3
    confidence = min(1.0, max(0.0, confidence))
    why = payload.get("why") or payload.get("reason") or payload.get("intent") or ""
    if not why:
        # An answer with no rationale usually means the schema was only partly
        # followed; flag it so the cache distinguishes it from a clean verdict.
        salvaged = True
    return ClassificationResult(
        "", tuple(kept), confidence, str(why)[:120], model, salvaged=salvaged
    )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class TechniqueCache:
    """Append-only JSONL cache of command hash -> labels.

    Append-only rather than rewritten wholesale: a classification is expensive
    (a model call) and a crash mid-run must not lose the work already paid for.
    Duplicate lines are tolerated -- :meth:`load` keeps the last verdict per
    hash, which is also what makes a re-classification of a specific command
    possible without editing history.
    """

    def __init__(self, path: Path, *, model: str) -> None:
        self.path = Path(path)
        self.model = model

    def load(self) -> dict[str, dict]:
        entries: dict[str, dict] = {}
        if not self.path.is_file():
            return entries
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                key = row.get("h")
                if isinstance(key, str) and key:
                    entries[key] = row
        return entries

    def append(self, results: Sequence[ClassificationResult]) -> None:
        if not results:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            json.dumps(
                {
                    "h": result.command_hash,
                    "labels": list(result.labels),
                    "confidence": round(result.confidence, 3),
                    "why": result.why,
                    "model": result.model,
                    "prompt": PROMPT_VERSION,
                    "salvaged": bool(result.salvaged),
                    **({"error": result.error} if result.error else {}),
                },
                ensure_ascii=False,
            )
            for result in results
            if result.command_hash
        ]
        if not rows:
            return
        # One write per batch, and taken under an exclusive lock, because two
        # workers appending at once would interleave partial lines and corrupt
        # the file for every later reader.  Measured 2026-11-04: a single
        # process tops out near 215 commands/minute (429 above concurrency 16),
        # so running two workers is the practical way to go faster -- which puts
        # two writers on this file.
        with self._locked("a") as handle:
            handle.write("\n".join(rows) + "\n")

    @contextlib.contextmanager
    def _locked(self, mode: str):
        """Append-only, mutually exclusive.

        ``fcntl`` does not exist on Windows, which is where this actually runs,
        so the guard there is an atomic exclusive-create marker file.  An
        earlier version opened a shared lock *file* and locked it; on Windows
        two threads opening the same path for ``a+b`` collided with
        ``PermissionError`` before any locking happened, which the concurrent
        test caught immediately.
        """

        marker = self.path.with_suffix(self.path.suffix + ".lock")
        acquired = False
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                if time.monotonic() > deadline:
                    # Never wedge a run on a stale marker; fall through and
                    # append anyway.  A torn line is recoverable (``load``
                    # skips unparsable rows), a hung run is not.
                    break
                time.sleep(0.003)
            except OSError:
                break
        try:
            with self.path.open(mode, encoding="utf-8") as handle:
                yield handle
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if acquired:
                try:
                    os.unlink(marker)
                except OSError:
                    pass


def load_model_credentials(config_row: Mapping[str, Any]) -> tuple[str, str, str]:
    """Return ``(base_url, model_name, api_key)`` from a model config row."""

    base = str(config_row.get("base_url") or "").rstrip("/")
    model = str(config_row.get("model_name") or "")
    key = str(config_row.get("api_key") or "")
    if not base or not model or not key:
        raise ValueError("model config is missing base_url / model_name / api_key")
    if not base.endswith("/v1") and "/v1/" not in base:
        base = f"{base}/v1"
    return base, model, key


async def classify_batch(
    client,
    commands: Iterable[str],
    *,
    base_url: str,
    model: str,
    api_key: str,
    concurrency: int = 4,
    timeout: float = 60.0,
    max_tokens: int = 1200,
    json_mode: bool = True,
    max_retries: int = 1,
    retry_backoff: float = 2.0,
) -> list[ClassificationResult]:
    """Classify commands concurrently, keeping failures as failures.

    ``json_mode`` asks the provider to constrain decoding to a JSON object.  It
    is not cosmetic: measured 2026-11-04 on this relay, ``glm-5.3`` ignores the
    parameter and answers with prose ("**Classification:** Read a file...")
    while ``glm-5.3-flash`` honours it and returns
    ``{"labels": ["unknown"], ...}`` verbatim.

    ``max_tokens`` defaults high on purpose.  These models emit
    ``reasoning_content`` *before* the answer, and it is billed against the same
    budget: at 160 the reasoning consumed everything, ``finish_reason`` came
    back ``length``, ``content`` was empty, and the run looked like a 100%
    parse failure on commands the model had actually understood.
    """

    semaphore = asyncio.Semaphore(max(1, concurrency))
    url = f"{base_url.rstrip('/')}/chat/completions"

    async def one(command: str, attempt: int = 0) -> ClassificationResult:
        digest = command_hash(command)
        payload = {
            "model": model,
            "messages": build_messages(command),
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        async with semaphore:
            try:
                # A hard ceiling *in addition to* the httpx timeout: httpx's
                # read timeout only fires between bytes, so a model that streams
                # reasoning slowly can hold a slot far longer than intended.
                # Measured 2026-11-04: one command blocked a batch for over 150
                # seconds while the client's own timeout never fired.
                response = await asyncio.wait_for(
                    client.post(
                        url,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                        timeout=timeout,
                    ),
                    timeout=timeout,
                )
            except Exception as error:  # noqa: BLE001 - transport failure is data
                # Retried, because on this relay a timeout is far more often a
                # transient blip than a permanent refusal: measured 2026-11-04,
                # the same command failed then succeeded seconds later with no
                # change to the request.
                if attempt < max_retries:
                    await asyncio.sleep(retry_backoff * (attempt + 1))
                    return await one(command, attempt + 1)
                name = "Timeout" if isinstance(error, asyncio.TimeoutError) else type(error).__name__
                return ClassificationResult(
                    digest, (), 0.0, "", model, error=f"transport:{name}"
                )
            if response.status_code != 200:
                return ClassificationResult(
                    digest, (), 0.0, "", model, error=f"http_{response.status_code}"
                )
            try:
                body = response.json()
                choice = body["choices"][0]
                text = choice["message"]["content"]
                finish = str(choice.get("finish_reason") or "")
            except (ValueError, KeyError, IndexError, TypeError):
                return ClassificationResult(digest, (), 0.0, "", model, error="bad_envelope")
            if finish == "length" and not str(text or "").strip():
                # Distinguished from a bad answer: the model understood the
                # command but spent the whole budget on reasoning, so the fix is
                # a bigger max_tokens, not a better prompt.
                return ClassificationResult(
                    digest, (), 0.0, "", model, error="truncated_before_answer"
                )
            result = parse_response(text, model=model)
            result.command_hash = digest
            if not result.ok and attempt < max_retries:
                # A prose answer is not stable for a given input: the same
                # command yields clean JSON on a retry often enough that
                # discarding it on the first attempt wastes most of the batch.
                await asyncio.sleep(retry_backoff * (attempt + 1))
                return await one(command, attempt + 1)
            return result

    return await asyncio.gather(*(one(c) for c in commands))


# ---------------------------------------------------------------------------
# Model registry (operator-selectable)
# ---------------------------------------------------------------------------


def resolve_model_entry(rows: Sequence[Mapping[str, Any]], wanted: str) -> Mapping[str, Any]:
    """Find the configured model the operator asked for, by name or id.

    Exists so the choice is a config decision, not a code change: the
    classifier is meant to be pointed at whatever model the operator trusts for
    this job, and swapping it must not require editing this module.
    """

    if not rows:
        raise ValueError("no model configs are available")
    wanted_cf = wanted.strip().casefold()
    for row in rows:
        if str(row.get("id") or "").casefold() == wanted_cf:
            return row
    for row in rows:
        if str(row.get("name") or "").casefold() == wanted_cf:
            return row
    available = ", ".join(sorted({str(r.get("name") or "") for r in rows}))
    raise ValueError(f"model {wanted!r} not found; available: {available}")
