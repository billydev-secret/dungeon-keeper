"""Layer 4 of the beta tools safety rails: every sidecar DB write goes through this gate.

Refuses to execute in any non-dev environment. Plan 1 doesn't write to the DB,
but later plans depend on this wrapper being available and tested.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from aiosqlite import Connection

from core.config import Config

log = logging.getLogger("beta_tools.db_gate")


async def beta_write(
    db: Connection, query: str, params: Sequence[Any] = (), *, cfg: Config,
) -> None:
    """Execute a write against db, only if cfg.is_dev. Raises RuntimeError otherwise.

    The cursor returned by db.execute is intentionally discarded — this is a write-only
    gate. Callers needing rowcount or lastrowid must not route through beta_write.
    """
    if not cfg.is_dev:
        raise RuntimeError(
            f"beta_tools write attempted in non-dev environment (env={cfg.env!r}). "
            "This indicates a config error or accidental import in a prod path."
        )
    log.debug("beta_write: %s", query)
    await db.execute(query, params)
