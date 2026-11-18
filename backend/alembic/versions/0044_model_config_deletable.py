"""Make every model configuration deletable.

``model_configs`` is referenced by five tables whose foreign keys all used the
MySQL default ``RESTRICT`` (``NO ACTION``).  Deleting a configuration that any
Run had ever used therefore failed with ``IntegrityError 1451``, which escaped
the API as a blanket HTTP 500 -- and there was no application-level delete path
at all, so a busy configuration could never be removed.

The rule these keys now express: a model configuration is operator-owned
editable state, while run history is an audit record.  Deleting a configuration
must never delete run *history* -- it only detaches it.

Two behaviours, chosen per child table:

* ``SET NULL`` for optional references.  A Run keeps its own ``engine_type``,
  ``hints_json`` and ``solver_mode``, so the engine identity survives a deleted
  config, and ``app/api/v1/runs.py`` already renders ``model else None``.
* ``CASCADE`` for ``model_skill_bindings``.  That column is ``NOT NULL``, and a
  skill binding to a model that no longer exists is meaningless rather than
  merely unattributed.

Every step is inspector-guarded so the migration is safe to replay.
"""

import sqlalchemy as sa

from alembic import op

revision = "0044_model_config_deletable"
down_revision = "0043_control_experiment_pairing"
branch_labels = None
depends_on = None

# child table -> what losing the referenced config should do to the child row.
ON_DELETE = {
    "solve_runs": "SET NULL",
    "run_attempts": "SET NULL",
    "agent_turns": "SET NULL",
    "challenge_conversations": "SET NULL",
    # NOT NULL column: see the module docstring.
    "model_skill_bindings": "CASCADE",
}


def _existing_fk(table: str, column: str) -> dict | None:
    """Return the foreign key on ``table.column``, whatever its name.

    The live schema carries MySQL auto-generated ``*_ibfk_N`` names while other
    environments may use the named form from the ORM, so look the key up by
    column rather than by name.
    """

    for constraint in sa.inspect(op.get_bind()).get_foreign_keys(table):
        if constraint.get("constrained_columns") == [column]:
            return constraint
    return None


def _retarget(table: str, column: str, ondelete: str) -> None:
    if table not in sa.inspect(op.get_bind()).get_table_names():
        return
    constraint = _existing_fk(table, column)
    if constraint is None:
        return
    if (constraint.get("options") or {}).get("ondelete", "").upper() == ondelete:
        return
    op.drop_constraint(constraint["name"], table, type_="foreignkey")
    op.create_foreign_key(f"fk_{table}_{column}", table, "model_configs", [column], ["id"], ondelete=ondelete)


def upgrade() -> None:
    if "model_configs" not in sa.inspect(op.get_bind()).get_table_names():
        return
    for table, ondelete in ON_DELETE.items():
        _retarget(table, "model_config_id", ondelete)


def downgrade() -> None:
    if "model_configs" not in sa.inspect(op.get_bind()).get_table_names():
        return
    for table in ON_DELETE:
        _retarget(table, "model_config_id", "RESTRICT")
