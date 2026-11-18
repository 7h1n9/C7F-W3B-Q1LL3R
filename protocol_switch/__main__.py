"""``python -m protocol_switch`` entry point."""

from __future__ import annotations

import sys

from .config import Settings
from .server import create_app


def main(argv: list[str] | None = None) -> int:
    del argv
    settings = Settings.from_env()
    if not settings.upstream_base_url:
        print(
            "SWITCH_UPSTREAM_BASE_URL is not set; the service will start but "
            "answer 503 on every model path.",
            file=sys.stderr,
        )
    import uvicorn

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
