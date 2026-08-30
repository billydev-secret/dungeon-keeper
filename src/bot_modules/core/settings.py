from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AutoDeleteSettings:
    """Auto-delete feature configuration constants."""

    # How often to poll for auto-delete tasks (seconds)
    poll_seconds: int = 60
    # Pause between individual message deletions (seconds — ~1/s to stay under Discord's per-channel bucket)
    delete_pause_seconds: float = 1.1
    # Pause between bulk-delete requests (rate limit is ~1/s per channel)
    bulk_delete_pause_seconds: float = 1.1
    # Pause between bulk role/permission modifications (seconds)
    role_modify_pause_seconds: float = 0.25
    # Max channels processed concurrently during startup catch-up
    # (per-channel rate buckets don't conflict, so this safely N-folds throughput)
    startup_concurrency: int = 6


# Default instance
AUTO_DELETE_SETTINGS = AutoDeleteSettings()
