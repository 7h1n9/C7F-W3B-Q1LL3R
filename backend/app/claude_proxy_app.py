"""The standalone Claude proxy as its own FastAPI app.

Why not reuse ``app.main``
--------------------------
The proxy is started as a *child process of the backend* when auto-start is on
(see :func:`app.main.lifespan`). If it imported ``app.main``, every child would
enter the same lifespan and start its own child -- an unbounded process tree,
each generation holding a socket and a token. This module is the fix: it builds
an app that contains exactly the proxy router and nothing that can re-enter the
launcher.

That also makes the child cheap. ``app.main`` opens the database, syncs builtin
skills, seeds policies and starts three background loops; none of that is
meaningful for a stateless translator whose only dependency is the model configs
one request at a time. Keeping the app minimal keeps startup fast and the blast
radius of the child small.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api.claude_proxy import router as claude_proxy_router


def create_proxy_app() -> FastAPI:
    """Build the proxy-only application.

    A factory rather than a module-level singleton so tests can build one on
    demand without a listening socket, matching how ``app.main`` exposes its app.
    """

    app = FastAPI(
        title="Claude protocol proxy",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(claude_proxy_router)
    return app


app = create_proxy_app()
