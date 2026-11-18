"""Classify unlabelled commands with a configurable model, offline.

This is the driver for ``app.training.technique_classifier``: it picks the
commands the deterministic vocabulary cannot label, asks a model to label them,
and appends the verdicts to a cache the miner reads.  Nothing here is on the
solve path.

Picking the model
-----------------
The model is a **configuration choice, not a code change** -- that was the
explicit requirement.  Two ways to select it, both resolved against the
operator's own ``model_configs`` table so no new credential store is introduced:

    --model GLM                 # by display name (case-insensitive)
    --model c8b5fc1c-...        # by config id
    --model none                # list what is available, then exit

Providers differ in which wire endpoint actually works (a gateway can serve
``/v1/messages`` while its ``/v1/chat/completions`` is behind Cloudflare), so a
failed run reports the HTTP status per command instead of collapsing everything
into "unknown".  Re-running is always safe: already-classified commands cost
nothing.

Usage::

    python scripts/classify_unknown_commands.py --list-models
    python scripts/classify_unknown_commands.py --model GLM --limit 200 --dry-run
    python scripts/classify_unknown_commands.py --model GLM --limit 2000
    python scripts/classify_unknown_commands.py --model GLM --report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.training.candidates import technique_labels  # noqa: E402
from app.training.technique_classifier import (  # noqa: E402
    ALLOWED_LABELS,
    PROMPT_VERSION,
    TechniqueCache,
    classify_batch,
    command_hash,
    is_platform_noise,
    load_model_credentials,
    resolve_model_entry,
)

CONTAINER = "c7f-w3b-q1ll3r-mysql-1"
TRAINING_DIR = ROOT / "data" / "training"
CACHE_DIR = ROOT / "data" / "training" / "technique-cache"

#: How many commands to classify before appending to the cache.  Small on
#: purpose: each call takes 30-60s on this relay, so a 25-wide batch meant
#: nothing reached disk for 15 minutes and an interrupted run lost all of it.
FLUSH_EVERY = 5


def sh_json(sql: str) -> list[dict]:
    proc = subprocess.run(
        [
            "docker", "exec", CONTAINER,
            "mysql", "--default-character-set=utf8mb4",
            "-uctf_agent", "-pctf_agent", "ctf_agent",
            # -r is load-bearing: without it the client re-escapes backslashes
            # and every JSON payload with a quote in it fails to parse.
            "-N", "-B", "-r", "-e", sql,
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"mysql failed: {proc.stderr[:400]}")
    rows: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Warning"):
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def fetch_models() -> list[dict]:
    """Enabled chat-capable configs, with decrypted keys, kept local only."""

    from app.services.crypto import decrypt_api_key

    rows = sh_json(
        "select json_object('id',id,'name',name,'base_url',base_url,"
        "'model_name',model_name,'provider_type',provider_type,"
        "'key',encrypted_api_key) "
        "from model_configs where enabled=1 order by name"
    )
    out: list[dict] = []
    for row in rows:
        try:
            row["api_key"] = decrypt_api_key(row.pop("key", ""))
        except Exception:  # noqa: BLE001 - a config without a usable key is skipped
            row["api_key"] = ""
        if row["api_key"]:
            out.append(row)
    return out


def newest_export() -> Path | None:
    files = sorted(TRAINING_DIR.glob("transitions-*.jsonl"))
    return files[-1] if files else None


def collect_unlabelled(path: Path) -> Counter:
    """Commands the regex vocabulary cannot label, deduplicated.

    Counted rather than listed: the same command recurs across runs (measured
    2.1x duplication), and frequency is the only cheap proxy for how much a
    correct label is worth.
    """

    counts: Counter = Counter()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                transition = json.loads(line)
            except ValueError:
                continue
            command = (transition.get("action") or {}).get("command") or ""
            if not command.strip():
                continue
            if technique_labels(command):
                continue
            counts[command] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="", help="model config name or id; 'none' lists available")
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument("--input", default="", help="transitions jsonl (default: newest)")
    parser.add_argument("--cache", default="", help="cache jsonl (default: derived from model+prompt)")
    parser.add_argument("--limit", type=int, default=200, help="max commands this run")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--min-frequency", type=int, default=1, help="skip commands seen fewer times")
    parser.add_argument("--shard", type=int, default=0, help="this worker's index (0-based)")
    parser.add_argument("--shards", type=int, default=1, help="total workers sharing this cache")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", action="store_true", help="summarise the cache and exit")
    args = parser.parse_args()

    models = fetch_models()
    if args.list_models or args.model.casefold() == "none":
        print(f"{'name':<22} {'model_name':<24} {'provider':<20} base_url")
        for row in models:
            print(
                f"{row['name']:<22} {row['model_name']:<24} "
                f"{row['provider_type']:<20} {row['base_url']}"
            )
        return 0
    if not args.model:
        raise SystemExit("--model is required (or use --list-models)")

    entry = resolve_model_entry(models, args.model)
    cache_name = f"{entry['name']}-{PROMPT_VERSION}.jsonl".replace("/", "_")
    cache_path = Path(args.cache) if args.cache else CACHE_DIR / cache_name
    cache = TechniqueCache(cache_path, model=entry["model_name"])

    if args.report:
        cached = cache.load()
        ok_rows = [r for r in cached.values() if not r.get("error")]
        failed = [r for r in cached.values() if r.get("error")]
        print(f"cache: {cache_path}")
        print(f"  entries: {len(cached)}  ok: {len(ok_rows)}  failed: {len(failed)}")
        print("  label histogram:", dict(Counter(l for r in ok_rows for l in r["labels"]).most_common()))
        if failed:
            print("  error histogram:", dict(Counter(r["error"] for r in failed).most_common(8)))
        return 0

    source = Path(args.input) if args.input else newest_export()
    if source is None or not source.is_file():
        raise SystemExit("no transitions export found")

    counts = collect_unlabelled(source)
    print(f"unlabelled distinct commands: {len(counts)} (occurrences {sum(counts.values())})")

    cached = cache.load()
    done = {h for h, row in cached.items() if not row.get("error")}
    candidates = [
        (cmd, n)
        for cmd, n in counts.most_common()
        if n >= args.min_frequency
        and not is_platform_noise(cmd)
        and command_hash(cmd) not in done
    ]

    # Sharding lets several workers run at once without duplicating calls.
    #
    # Workers share one cache, so they all see the same "still unlabelled" list
    # and would start from the same top entry -- measured 2026-11-04, two
    # workers overlapped almost completely for the first minute.  Round-robin
    # assignment (`--shard i/n`) splits the list deterministically, so worker i
    # only ever takes every n-th command.
    if args.shards > 1:
        candidates = candidates[args.shard :: args.shards]

    print(f"  skipped as platform noise / short: {sum(1 for c in counts if is_platform_noise(c))} distinct")
    print(f"  already classified: {len(done)}")
    if args.shards > 1:
        print(f"  shard {args.shard}/{args.shards}: {len(candidates)} command(s) assigned")
    print(f"  queued this run: {len(candidates[: args.limit])} (limit {args.limit})")

    if args.dry_run:
        for cmd, n in candidates[:10]:
            print(f"   {n:4d}x {cmd[:100].replace(chr(10), ' | ')}")
        return 0
    if not candidates:
        print("nothing to do")
        return 0

    import httpx

    base_url, model_name, api_key = load_model_credentials(entry)
    print(f"model: {entry['name']} -> {model_name} @ {base_url}")

    batch = [cmd for cmd, _ in candidates[: args.limit]]
    total_ok = total_err = 0

    async def run_all():
        nonlocal total_ok, total_err
        # trust_env=False: this box has a system proxy that must not be used
        # for a direct provider call (it caused a whole night of false failures).
        async with httpx.AsyncClient(trust_env=False) as client:
            for start in range(0, len(batch), FLUSH_EVERY):
                chunk = batch[start : start + FLUSH_EVERY]
                results = await classify_batch(
                    client,
                    chunk,
                    base_url=base_url,
                    model=model_name,
                    api_key=api_key,
                    concurrency=args.concurrency,
                    timeout=args.timeout,
                )
                # Only successes are cached.  A failure row would be read back as
                # "already classified" by the next run and permanently block the
                # command -- measured 2026-11-04, 245 failed rows made the cache
                # look complete while holding 26% of the data.
                cache.append([r for r in results if r.ok])
                ok = sum(1 for r in results if r.ok)
                total_ok += ok
                total_err += len(results) - ok
                print(
                    f"  {start + len(chunk)}/{len(batch)}: ok={ok} "
                    f"err={len(results) - ok} (total ok={total_ok})",
                    flush=True,
                )

    asyncio.run(run_all())
    print(f"done: ok={total_ok} err={total_err} -> {cache_path}")
    return 0 if total_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
