"""Writeup facade used by both waiting and terminal outcomes."""

from app.services.run_finalizer import run_finalizer


class WriteupBuilder:
    async def build_partial_wp(self, session, run, reason: str) -> dict:
        return await run_finalizer.build_wp(session, run, reason)

    async def build_unsolved_wp(self, session, run, reason: str) -> dict:
        return await run_finalizer.build_wp(session, run, reason)


writeup_builder = WriteupBuilder()
