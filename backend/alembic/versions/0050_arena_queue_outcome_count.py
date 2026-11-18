"""Stop charging a challenge for attempts it never got to use.

The retry budget exists to retire a challenge that is genuinely broken: "a
container challenge that fails twice is more likely to be broken than unlucky".
It was implemented as a single counter incremented when an item was *claimed*,
which cannot tell the two apart -- and a process restart was the worst case:

* ``_process`` sets ``RUNNING`` and increments ``attempt_count``;
* the backend restarts (a code change, a crash, an operator stopping it);
* ``recover()`` returns the item to ``PENDING``;
* the scheduler picks it again and increments to 2;
* one more restart and it is at ``MAX_ATTEMPTS`` and **retired without ever
  having completed a solve**.

That is not a hypothetical: during a session with a handful of restarts, every
queued challenge reached the cap and would have been marked ``FAILED`` in turn,
for reasons that have nothing to do with the challenges.

The fix is to count only attempts that actually ran to a verdict
(``outcome_count``) and keep the claim count for the record
(``attempt_count``).  A restart now costs an interrupt count, not an attempt.
"""

import sqlalchemy as sa

from alembic import op

revision = "0050_arena_queue_outcome_count"
down_revision = "0049_arena_multi_engine"
branch_labels = None
depends_on = None

TABLE = "arena_solve_queue"
COLUMN = "outcome_count"


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(TABLE)}


def upgrade() -> None:
    if COLUMN in _existing_columns():
        return
    op.add_column(TABLE, sa.Column(COLUMN, sa.Integer(), nullable=True))
    # Backfill, and the rule differs by status because only one of the two is
    # recoverable:
    #
    # * Finished rows already had a verdict, so their claim count *is* their
    #   outcome count (or an over-count).  Keeping the old value preserves the
    #   judgement that was made about them rather than resurrecting challenges an
    #   operator already saw retired.
    # * Live rows (PENDING/RUNNING) have no verdict to preserve, and under the old
    #   counting a restart charged them an attempt they never got to use.  A fresh
    #   budget is the honest reading: what they show is interrupts, not failures.
    op.execute(
        f"UPDATE {TABLE} SET {COLUMN} = attempt_count "
        f"WHERE {COLUMN} IS NULL AND status IN ('SOLVED', 'UNSOLVED', 'FAILED', 'CANCELLED')"
    )
    op.execute(
        f"UPDATE {TABLE} SET {COLUMN} = 0 "
        f"WHERE {COLUMN} IS NULL AND status IN ('PENDING', 'RUNNING')"
    )
    op.execute(f"UPDATE {TABLE} SET {COLUMN} = 0 WHERE {COLUMN} IS NULL")
    op.alter_column(TABLE, COLUMN, existing_type=sa.Integer(), nullable=False, server_default="0")


def downgrade() -> None:
    if COLUMN in _existing_columns():
        op.drop_column(TABLE, COLUMN)