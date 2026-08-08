from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .blackboard import SharedGraph


def main() -> int:
    parser = argparse.ArgumentParser(description="Worker SharedGraph CLI")
    parser.add_argument("command", choices=("write-fact", "read-facts", "mark-deadend", "list-intents", "claim", "claim-resource", "release-resource", "write-poc", "write-flag"))
    parser.add_argument("value", nargs="?")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--verified", action="store_true")
    parser.add_argument("--worker-id", default=os.environ.get("MUTEKI_WORKER_ID"))
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    db_path = os.environ.get("MUTEKI_BLACKBOARD_DB")
    if not db_path:
        parser.error("MUTEKI_BLACKBOARD_DB is required")
    graph = SharedGraph(Path(db_path), worker_id=args.worker_id, run_id=os.environ.get("MUTEKI_RUN_ID", ""))
    if args.command == "write-fact":
        result = graph.write_fact(args.value or "", args.verified)
    elif args.command == "read-facts":
        result = [item.__dict__ if hasattr(item, "__dict__") else {"id": item.id, "content": item.content, "source_worker_id": item.source_worker_id, "verified": item.verified, "created_at": item.created_at} for item in graph.read_facts(args.limit)]
    elif args.command == "mark-deadend":
        result = graph.mark_deadend(args.value or "")
    elif args.command == "list-intents":
        result = [item.__dict__ if hasattr(item, "__dict__") else {"id": item.id, "description": item.description, "status": item.status, "claimed_by": item.claimed_by, "created_at": item.created_at} for item in graph.list_intents()]
    elif args.command == "claim":
        result = graph.claim_intent(args.value or "")
    elif args.command == "claim-resource":
        result = graph.claim_resource(args.value or "")
    elif args.command == "release-resource":
        result = graph.release_resource(args.value or "")
    elif args.command == "write-poc":
        result = graph.write_poc(args.value or "")
    else:
        result = graph.write_flag(args.value or "", worker_output=args.output)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
