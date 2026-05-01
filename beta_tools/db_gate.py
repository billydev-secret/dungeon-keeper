"""Layer 4 of the beta tools safety rails: every sidecar DB write goes through this gate.

Refuses to execute in any non-dev environment. Plan 1 doesn't write to the DB,
but later plans depend on this wrapper being available and tested.
"""

from __future__ import annotations

import logging

from config import Config

log = logging.getLogger("beta_tools.db_gate")


async def beta_write(db, query: str, params: tuple = (), *, cfg: Config) -> None:
    """Execute a write against db, only if cfg.is_dev. Raises RuntimeError otherwise."""
    if not cfg.is_dev:
        raise RuntimeError(
            f"beta_tools write attempted in non-dev environment (env={cfg.env!r}). "
            "This indicates a config error or accidental import in a prod path."
        )
    await db.execute(query, params)
