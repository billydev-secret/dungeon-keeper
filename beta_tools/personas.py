"""Puppet persona configs.

Loaded from fixtures/beta_puppets.yaml at sidecar startup. Each persona maps
to one of the three puppet bot accounts (BETA_PUPPET_TOKEN_1..3 in env order).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_VALID_LENGTH_BIAS = {"short", "medium", "long"}


@dataclass(frozen=True)
class Persona:
    key: str
    display_name: str
    avatar_url: str
    activity_weight: float
    channel_affinities: dict[str, float]
    voice_likely: bool
    message_length_bias: str  # "short" | "medium" | "long"


def load_puppet_personas(path: str | Path) -> list[Persona]:
    """Load and validate the puppet persona list from a YAML file."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"expected a YAML list at top level, got {type(raw).__name__}")

    personas: list[Persona] = []
    seen_keys: set[str] = set()

    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"persona #{i} is not a mapping: {entry!r}")
        bias = entry.get("message_length_bias", "medium")
        if bias not in _VALID_LENGTH_BIAS:
            raise ValueError(
                f"persona #{i} has invalid message_length_bias={bias!r}; "
                f"must be one of {sorted(_VALID_LENGTH_BIAS)}"
            )
        key = entry["key"]
        if key in seen_keys:
            raise ValueError(f"duplicate persona key {key!r}")
        seen_keys.add(key)
        personas.append(Persona(
            key=key,
            display_name=entry["display_name"],
            avatar_url=entry["avatar_url"],
            activity_weight=float(entry["activity_weight"]),
            channel_affinities={str(k): float(v) for k, v in entry["channel_affinities"].items()},
            voice_likely=bool(entry["voice_likely"]),
            message_length_bias=bias,
        ))

    return personas
