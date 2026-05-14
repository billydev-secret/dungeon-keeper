import json
import os
import random
import logging
from bot_modules.games.utils.ai_client import generate_text

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "prompt_config.json")


# ── Config loader ──────────────────────────────────────────────────
def _load_config() -> dict:
    """Load prompt config from JSON file (re-reads each call so edits take effect)."""
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ── AI generation prompts ───────────────────────────────────────────

def _first_line(text: str) -> str | None:
    for line in text.strip().splitlines():
        line = line.strip().lstrip("-•*0123456789. ").strip('"').strip()
        if line:
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


async def get_wyr_question(db, category: str = "sfw") -> tuple[str, str] | None:
    """Returns (option_a, option_b) from the bank, or AI fallback."""
    result = await _get_bank_question(db, "wyr", category=category)
    if result and "|" in result:
        a, b = result.split("|", 1)
        return a.strip(), b.strip()

    # AI fallback
    cfg = get_ai_config("wyr", category)
    if cfg:
        text = await generate_text(cfg[0], cfg[1], max_tokens=cfg[2])
        if text:
            parsed = _parse_wyr(text)
            if parsed:
                return parsed
    return None


async def get_nhie_statement(db, category: str = "sfw") -> str | None:
    return (
        await _get_bank_question(db, "nhie", category=category)
        or await _ai_generate("nhie", category)
    )


async def get_mlt_prompt(db, category: str = "sfw") -> str | None:
    return (
        await _get_bank_question(db, "mlt", category=category)
        or await _ai_generate("mlt", category)
    )


async def get_rushmore_topic(db, category: str = "sfw") -> str | None:
    return (
        await _get_bank_question(db, "rushmore", category=category)
        or await _ai_generate("rushmore", category)
    )


async def get_price_scenario(db, category: str = "sfw") -> str | None:
    return (
        await _get_bank_question(db, "price", category=category)
        or await _ai_generate("price", category)
    )


async def get_clapback_prompt(
    db, exclude: list[str] | None = None, category: str = "sfw",
) -> str | None:
    """Returns a Clapback prompt from the bank, or AI fallback if bank exhausted."""
    result = await _get_bank_question(db, "clapback", exclude=exclude, category=category)
    if result:
        return result
    return await _ai_generate("clapback", category)


async def has_clapback_prompts(db) -> bool:
    """Returns True if at least 1 Clapback prompt exists in the bank.

    Note: AI fallback is always available, so games can still run with an empty bank.
    """
    row = await db.fetchone(
        "SELECT 1 FROM games_question_bank WHERE game_type = 'clapback' LIMIT 1",
    )
    return row is not None


async def _get_bank_question(
    db,
    game_type: str,
    exclude: list[str] | None = None,
    category: str | None = None,
) -> str | None:
    """Fetch a random question from the question bank, optionally filtered by category."""
    if category:
        rows = await db.fetchall(
            "SELECT question_text FROM games_question_bank WHERE game_type = ? AND category = ?",
            (game_type, category),
        )
    else:
        rows = await db.fetchall(
            "SELECT question_text FROM games_question_bank WHERE game_type = ?",
            (game_type,),
        )
    if rows:
        candidates = [r[0] for r in rows]
        if exclude:
            candidates = [c for c in candidates if c not in exclude]
        if candidates:
            return random.choice(candidates)
    return None
