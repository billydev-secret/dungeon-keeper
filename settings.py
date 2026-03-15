from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AutoDeleteSettings:
    """Auto-delete feature configuration constants."""

    # Maximum number of messages to pull in one batch
    max_messages: int = 400
    # Truncate each message content to this many characters
    max_chars_per_msg: int = 240
    # Cap total payload size to the model
    max_total_chars: int = 40_000
    # Minimum age for messages to be eligible for deletion (seconds)
    min_age_seconds: int = 60
    # Minimum interval between auto-delete runs (seconds)
    min_interval_seconds: int = 60
    # How often to poll for auto-delete tasks (seconds)
    poll_seconds: int = 60
    # Pause between individual message deletions (seconds)
    delete_pause_seconds: float = 0.35
    # Pause between bulk role/permission modifications (seconds)
    role_modify_pause_seconds: float = 0.25


def _default_run_keywords() -> dict[str, str]:
    return {
        "once": "once",
        "now": "once",
        "manual": "once",
        "off": "off",
        "disable": "off",
        "none": "off",
    }


def _default_named_intervals() -> dict[str, int]:
    return {
        "hourly": 60 * 60,
        "daily": 24 * 60 * 60,
        "weekly": 7 * 24 * 60 * 60,
    }


def _default_duration_pattern() -> re.Pattern[str]:
    return re.compile(
        r"(\d+)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)",
        re.IGNORECASE,
    )


@dataclass(frozen=True)
class AutoDeleteKeywords:
    """Keyword mappings for auto-delete commands."""

    run_keywords: dict[str, str] = field(default_factory=_default_run_keywords)
    named_intervals: dict[str, int] = field(default_factory=_default_named_intervals)
    duration_pattern: re.Pattern[str] = field(default_factory=_default_duration_pattern)


# Default instances
AUTO_DELETE_SETTINGS = AutoDeleteSettings()
AUTO_DELETE_KEYWORDS = AutoDeleteKeywords()
