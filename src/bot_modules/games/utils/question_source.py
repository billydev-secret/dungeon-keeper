import json
import os
import random
import logging
from bot_modules.games.utils.ai_client import generate_text

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "prompt_config.json")


# ── Config loader ──────────────────────────────────────────────────
def _load_config() -> dict:
    """Load prompt config from JSON file (re-reads each call so edits take effect)."""
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ── AI generation prompts ───────────────────────────────────────────

def _first_line(text: str) -> str | None:
    for line in text.strip().splitlines():
        line = line.strip().lstrip("-•*0123456789. ").strip('"').strip()
        if line and not line.startswith("#"):
            return line
    return None


def _parse_wyr(text: str) -> tuple[str, str] | None:
    line = _first_line(text)
    if not line or "|" not in line:
        return None
    a, b = line.split("|", 1)
    a, b = a.strip(), b.strip()
    return (a, b) if a and b else None


def _system(game_descriptor: str, category: str) -> str:
    cfg = _load_config()
    tone = cfg["nsfw_tone"] if category == "nsfw" else cfg["sfw_tone"]
    return (
        f"You write {game_descriptor} for a Discord party game.\n\n"
        f"{cfg['audience']}\n\n"
        f"{tone}"
    )


def get_ai_config(game_type: str, category: str = "sfw") -> tuple[str, str, int] | None:
    cfg = _load_config()
    game_cfg = cfg["games"].get(game_type)
    if not game_cfg:
        return None
    return _system(game_cfg["descriptor"], category), game_cfg["user_prompt"], game_cfg["max_tokens"]


async def _ai_generate(game_type: str, category: str = "sfw") -> str | None:
    cfg = get_ai_config(game_type, category)
    if not cfg:
        return None
    system, user, max_tokens = cfg
    text = await generate_text(system, user, max_tokens=max_tokens)
    if not text:
        log.warning("AI fallback failed for game_type=%s", game_type)
        return None
    return _first_line(text)


# ── Public API ──────────────────────────────────────────────────────


async def get_wyr_question(db, tags: list[str] | None = None) -> tuple[str, str] | None:
    """Returns (option_a, option_b) from the bank, or AI fallback.

    When a tag filter is supplied but nothing matches, returns None (no AI fallback).
    """
    result = await _get_bank_question(db, "wyr", tags=tags)
    if result and "|" in result:
        a, b = result.split("|", 1)
        return a.strip(), b.strip()
    if tags:  # filtered miss → suppress AI fallback, signal no-match
        return None

    # AI fallback (unfiltered only)
    cfg = get_ai_config("wyr", "sfw")
    if cfg:
        text = await generate_text(cfg[0], cfg[1], max_tokens=cfg[2])
        if text:
            parsed = _parse_wyr(text)
            if parsed:
                return parsed
    return None


async def get_nhie_statement(db, tags: list[str] | None = None) -> str | None:
    result = await _get_bank_question(db, "nhie", tags=tags)
    if result is not None:
        return result
    if tags:
        return None
    return await _ai_generate("nhie", "sfw")


async def get_mlt_prompt(db, tags: list[str] | None = None) -> str | None:
    result = await _get_bank_question(db, "mlt", tags=tags)
    if result is not None:
        return result
    if tags:
        return None
    return await _ai_generate("mlt", "sfw")


async def get_rushmore_topic(db, tags: list[str] | None = None) -> str | None:
    result = await _get_bank_question(db, "rushmore", tags=tags)
    if result is not None:
        return result
    if tags:
        return None
    return await _ai_generate("rushmore", "sfw")


async def get_price_scenario(db, tags: list[str] | None = None) -> str | None:
    result = await _get_bank_question(db, "price", tags=tags)
    if result is not None:
        return result
    if tags:
        return None
    return await _ai_generate("price", "sfw")


async def get_clapback_prompt(
    db, exclude: list[str] | None = None, tags: list[str] | None = None,
    allow_nsfw: bool = False,
) -> str | None:
    """Returns a Clapback prompt from the bank, or AI fallback if bank exhausted.

    A tag-filtered miss returns None (no AI fallback).
    """
    result = await _get_bank_question(
        db, "clapback", exclude=exclude, tags=tags, allow_nsfw=allow_nsfw,
    )
    if result is not None:
        return result
    if tags:
        return None
    return await _ai_generate("clapback", "sfw")


async def get_photo_prompt(db, tags: list[str] | None = None) -> str | None:
    """Return a random Photo Challenge prompt from the bank, or None if empty.

    Bank-only by design: photo prompts are curated in the web Games Studio
    (no AI fallback at launch). The cog posts a "no prompts available" notice
    when this returns None.
    """
    return await _get_bank_question(db, "photo", tags=tags)


async def has_clapback_prompts(db) -> bool:
    """Returns True if at least 1 Clapback prompt exists in the bank.

    Note: AI fallback is always available, so games can still run with an empty bank.
    """
    row = await db.fetchone(
        "SELECT 1 FROM games_question_bank WHERE game_type = 'clapback' LIMIT 1",
    )
    return row is not None


def _parse_tags(tags_json) -> set[str]:
    """Parse a row's JSON tags column into a set, tolerating bad data."""
    try:
        return set(json.loads(tags_json or "[]"))
    except (json.JSONDecodeError, TypeError):
        return set()


async def _get_bank_question(
    db,
    game_type: str,
    exclude: list[str] | None = None,
    tags: list[str] | None = None,
    allow_nsfw: bool = False,
) -> str | None:
    """Fetch a random bank question for game_type, applying tag rules.

    Tag rules (in precedence order):
      1. Rows tagged 'nsfw' are excluded UNLESS *allow_nsfw* is set or 'nsfw' is
         among the requested tags.
      2. If a non-empty tag filter is requested: keep rows whose tags intersect it
         (ANY-match).
      3. If no tag filter: keep all remaining rows.
    """
    rows = await db.fetchall(
        "SELECT question_text, tags FROM games_question_bank WHERE game_type = ?",
        (game_type,),
    )
    requested = set(tags or [])
    opted_nsfw = allow_nsfw or ("nsfw" in requested)

    candidates: list[str] = []
    for text, tags_json in rows:
        row_tags = _parse_tags(tags_json)
        if "nsfw" in row_tags and not opted_nsfw:   # rule 1 — wins over intersection
            continue
        if requested and not (row_tags & requested):  # rule 2 — ANY-match
            continue
        candidates.append(text)                        # rule 3 — keep

    if exclude:
        candidates = [c for c in candidates if c not in exclude]
    return random.choice(candidates) if candidates else None


async def has_matching_questions(
    db, game_type: str, tags: list[str] | None, allow_nsfw: bool = False,
) -> bool:
    """True if at least one bank row matches the tag filter (same rules as
    _get_bank_question). Used by slash commands to refuse-on-empty-filtered-pool."""
    return await _get_bank_question(
        db, game_type, tags=tags, allow_nsfw=allow_nsfw,
    ) is not None


async def get_ffa_prompt(db, kind: str = "random", tags: list[str] | None = None):
    """Return (label, text) for an FFA Truth-or-Dare card, or None on a filtered miss.

    'truth'/'dare'/'nsfw' are reserved tags. *kind* ('truth'|'dare'|'random') is a
    required dimension; the host's remaining (free) tags are ANY-match; 'nsfw' stays
    opt-in. Falls back to the code prompt bank only when no tag filter was supplied.
    """
    from bot_modules.games_ffa.prompts import pick_prompt, TRUTH, DARE

    rows = await db.fetchall(
        "SELECT question_text, tags FROM games_question_bank WHERE game_type = ?",
        ("ffa",),
    )
    requested = set(tags or [])
    opted_nsfw = "nsfw" in requested
    free = requested - {"truth", "dare", "nsfw"}    # host's non-reserved filter terms
    want = {"truth"} if kind == "truth" else {"dare"} if kind == "dare" else set()

    candidates: list[tuple[str, str]] = []
    for text, tags_json in rows:
        row_tags = _parse_tags(tags_json)
        if "nsfw" in row_tags and not opted_nsfw:
            continue
        if want and not (want & row_tags):           # kind requirement (AND)
            continue
        if free and not (free & row_tags):           # host free tags (ANY-match)
            continue
        candidates.append((DARE if "dare" in row_tags else TRUTH, text))

    if candidates:
        return random.choice(candidates)
    if requested:                                    # host set a tag filter → refuse
        return None
    return pick_prompt(kind, False)                  # unfiltered empty → code fallback
