"""AI quest-idea generation — pure prompt building and response parsing.

The Bank Manager's "Generate ideas" button hits an Anthropic model (the same
cloud path the party games use, :func:`bot_modules.games.utils.ai_client.generate_text`)
and gets back a batch of quest suggestions the manager reviews and one-click
loads into the New-quest form. Nothing here creates a quest — a suggestion is
inert until the manager submits it.

Everything in this module is deterministic on its inputs (no discord, no db, no
network): the prompt builder and the tolerant parser. The model hands back JSON
often enough to be worth parsing, malformed often enough that we must degrade
gracefully — a line that isn't valid JSON still yields a title-only suggestion
rather than nothing. Both paths are table-testable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from bot_modules.economy.quests import reward_band

# The three quest flavours the library understands, with the shape the model
# should aim for. Community quests are a whole-server goal, so they also carry
# a suggested target count.
_QTYPE_BRIEF: dict[str, str] = {
    "daily": (
        "a small, quick task a member can do in one sitting today "
        "(react, post, greet, answer a prompt)"
    ),
    "weekly": (
        "a meatier goal worth chipping at across the week "
        "(host something, hit a streak, help several people)"
    ),
    "community": (
        "a single server-wide goal everyone contributes to, with a numeric "
        "target the whole community works toward together"
    ),
}

# How many ideas one click asks for; kept small so a batch is one cheap call and
# the results list stays scannable.
DEFAULT_COUNT = 5
MAX_COUNT = 10

# Structured generation — lower than the games' 0.9 chatty default (matches the
# legitlibs ai-prep route), since we want parseable JSON, not maximum surprise.
TEMPERATURE = 0.4
MAX_TOKENS = 900


@dataclass(frozen=True)
class QuestIdea:
    """One generated suggestion. Every field is advisory — the manager edits
    freely before creating, and ``community_target`` is only meaningful for the
    community qtype (None otherwise)."""

    title: str
    description: str
    criteria: str
    reward: int
    community_target: int | None = None

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "criteria": self.criteria,
            "reward": self.reward,
            "community_target": self.community_target,
        }


def build_system_prompt(currency_name: str) -> str:
    """The role framing for the generator, naming the guild's currency so
    rewards read in-world (``120 gold`` rather than ``120 currency``)."""
    return (
        "You design engagement quests for a Discord community's economy. "
        f"The server currency is called \"{currency_name}\". Quests should be "
        "concrete, achievable, and fun — the kind of thing a member reads and "
        "immediately knows how to do. Avoid anything that needs a moderator to "
        "adjudicate unless the task is inherently judged."
    )


def build_user_prompt(qtype: str, count: int, theme: str = "") -> str:
    """Build the batch request for ``count`` ideas of one quest type.

    Asks for a strict JSON array so :func:`parse_quest_ideas` can load it, and
    steers rewards into the advisory band for the type so suggestions don't
    trip the dashboard's out-of-band warning.
    """
    if qtype not in _QTYPE_BRIEF:
        raise ValueError(f"unknown quest type: {qtype!r}")
    brief = _QTYPE_BRIEF[qtype]
    band = reward_band(qtype)
    band_line = (
        f"Suggested reward is {band[0]}–{band[1]}; stay in that range."
        if band
        else "Pick a reward that fits the effort."
    )
    target_field = (
        '\n  "community_target": <integer count the server works toward>,'
        if qtype == "community"
        else ""
    )
    theme_line = f"\nTheme to lean into: {theme.strip()}." if theme.strip() else ""

    return (
        f"Generate {count} distinct {qtype} quest ideas. Each is {brief}.{theme_line}\n"
        f"{band_line}\n\n"
        "Return ONLY a JSON array, no prose, no markdown fence. Each element:\n"
        "{\n"
        '  "title": <short name, <=80 chars>,\n'
        '  "description": <one sentence pitching it to members>,\n'
        '  "criteria": <what counts as done, shown on the claim card>,'
        f'\n  "reward": <integer>,{target_field}\n'
        "}"
    )


# ── parsing ────────────────────────────────────────────────────────────────

# The model sometimes wraps the array in a ```json fence despite instructions;
# strip a leading/trailing fence before attempting a whole-string JSON load.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _coerce_reward(value, qtype: str) -> int:
    """Best-effort int for a reward field, falling back to the band low (or 0)."""
    try:
        n = int(value)
        return n if n >= 0 else 0
    except (TypeError, ValueError):
        band = reward_band(qtype)
        return band[0] if band else 0


def _coerce_target(value) -> int | None:
    try:
        n = int(value)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _idea_from_obj(obj: dict, qtype: str) -> QuestIdea | None:
    """Build a QuestIdea from a parsed JSON object, or None if it has no title.

    A titleless object is worthless (nothing to show in the results list), so it
    is dropped rather than surfaced as a blank row.
    """
    title = str(obj.get("title") or "").strip()
    if not title:
        return None
    return QuestIdea(
        title=title[:256],
        description=str(obj.get("description") or "").strip()[:2000],
        criteria=str(obj.get("criteria") or "").strip()[:2000],
        reward=_coerce_reward(obj.get("reward"), qtype),
        community_target=(
            _coerce_target(obj.get("community_target")) if qtype == "community" else None
        ),
    )


def _title_only_ideas(text: str, qtype: str) -> list[QuestIdea]:
    """Last-resort salvage when JSON parsing fails entirely.

    Treat each non-empty, non-fence line as a bare title so the manager still
    gets ideas to load and flesh out, rather than an empty results panel. The
    reward defaults to the band low.
    """
    band = reward_band(qtype)
    default_reward = band[0] if band else 0
    ideas: list[QuestIdea] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-•*").strip()
        line = re.sub(r"^\d+[.)]\s*", "", line).strip().strip('"').strip()
        if not line or line.startswith("#") or line in ("[", "]", "{", "}"):
            continue
        ideas.append(QuestIdea(title=line[:256], description="", criteria="", reward=default_reward))
    return ideas


def parse_quest_ideas(text: str, qtype: str, limit: int = MAX_COUNT) -> list[QuestIdea]:
    """Parse a model response into quest ideas, degrading gracefully.

    Order of attempts:
      1. Strip a markdown fence and JSON-load the whole thing as an array.
      2. Extract the first ``[...]`` slice and JSON-load that (handles leading
         prose the model added despite instructions).
      3. Fall back to title-only salvage, one idea per line.

    Non-dict elements inside a valid array are skipped; a whole-response failure
    never returns empty when there is any usable text. Capped at ``limit``.
    """
    if not text or not text.strip():
        return []

    candidates: list[str] = []
    stripped = _FENCE_RE.sub("", text.strip())
    candidates.append(stripped)
    match = re.search(r"\[.*\]", stripped, re.DOTALL)
    if match:
        candidates.append(match.group(0))

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, list):
            continue
        ideas = [
            idea
            for obj in data
            if isinstance(obj, dict) and (idea := _idea_from_obj(obj, qtype)) is not None
        ]
        if ideas:
            return ideas[:limit]

    return _title_only_ideas(text, qtype)[:limit]
