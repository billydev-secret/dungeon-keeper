import json
import os
import random
import logging
from typing import Any
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


def channel_allows_nsfw(channel) -> bool:
    """NSFW prompts are gated on Discord's channel age-restriction flag.

    Threads inherit their parent channel's flag. Any channel we can't resolve
    is treated as SFW (fail safe).
    """
    try:
        if hasattr(channel, "is_nsfw"):
            return bool(channel.is_nsfw())
        parent = getattr(channel, "parent", None)
        if parent is not None and hasattr(parent, "is_nsfw"):
            return bool(parent.is_nsfw())
    except Exception:
        log.exception("channel_allows_nsfw check")
    return False


async def get_wyr_question(
    db, tags: list[str] | None = None, allow_nsfw: bool = False
) -> tuple[str, str] | None:
    """Returns (option_a, option_b) from the bank, or AI fallback.

    When a tag filter is supplied but nothing matches, returns None (no AI fallback).
    """
    result = await _get_bank_question(db, "wyr", tags=tags, allow_nsfw=allow_nsfw)
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


async def get_nhie_statement(
    db, tags: list[str] | None = None, allow_nsfw: bool = False
) -> str | None:
    result = await _get_bank_question(db, "nhie", tags=tags, allow_nsfw=allow_nsfw)
    if result is not None:
        return result
    if tags:
        return None
    return await _ai_generate("nhie", "sfw")


async def get_mlt_prompt(
    db, tags: list[str] | None = None, allow_nsfw: bool = False
) -> str | None:
    result = await _get_bank_question(db, "mlt", tags=tags, allow_nsfw=allow_nsfw)
    if result is not None:
        return result
    if tags:
        return None
    return await _ai_generate("mlt", "sfw")


async def get_rushmore_topic(
    db, tags: list[str] | None = None, allow_nsfw: bool = False
) -> str | None:
    result = await _get_bank_question(db, "rushmore", tags=tags, allow_nsfw=allow_nsfw)
    if result is not None:
        return result
    if tags:
        return None
    return await _ai_generate("rushmore", "sfw")


async def get_price_scenario(
    db, tags: list[str] | None = None, allow_nsfw: bool = False
) -> str | None:
    result = await _get_bank_question(db, "price", tags=tags, allow_nsfw=allow_nsfw)
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


async def get_photo_prompt(
    db, tags: list[str] | None = None, allow_nsfw: bool = False
) -> str | None:
    """Return a random Photo Challenge prompt from the bank, or None if empty.

    Bank-only by design: photo prompts are curated in the web Games Studio
    (no AI fallback at launch). The cog logs and skips the round when this
    returns None — nothing is posted to the channel.
    """
    return await _get_bank_question(db, "photo", tags=tags, allow_nsfw=allow_nsfw)


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


def _recency_key(last_served_at: str | None) -> tuple[int, str]:
    """Sort key that puts never-served rows (NULL) before any timestamp."""
    return (0, "") if last_served_at is None else (1, last_served_at)


def _pick_least_recently_served(
    candidates: list[tuple[int, Any, str | None]], rng: random.Random | None = None,
) -> tuple[int, Any] | None:
    """Pick a round-robin candidate: least-recently-served first, ties random.

    *candidates* is a list of ``(question_id, payload, last_served_at)``
    triples; returns the ``(question_id, payload)`` chosen, or ``None`` if
    *candidates* is empty. Never-served rows (``last_served_at is None``)
    are always preferred over any previously-served row, so a fresh bank
    addition gets served before the pool starts repeating.
    """
    if not candidates:
        return None
    chooser = rng if rng is not None else random
    min_key = min(_recency_key(c[2]) for c in candidates)
    tied = [(qid, payload) for qid, payload, last in candidates if _recency_key(last) == min_key]
    return chooser.choice(tied)


async def _mark_served(db, question_id: int) -> None:
    await db.execute(
        "UPDATE games_question_bank SET last_served_at = CURRENT_TIMESTAMP WHERE question_id = ?",
        (question_id,),
    )


def _filter_bank_rows(
    rows, requested: set[str], allow_nsfw: bool,
) -> list[tuple[int, str, str | None]]:
    """Apply the shared nsfw/tag rules to raw bank rows.

    Tag rules (in precedence order):
      1. Rows tagged 'nsfw' are excluded unless *allow_nsfw* is True. NSFW is
         gated on the Discord channel's age-restriction flag (see
         :func:`channel_allows_nsfw`); a requested tag cannot re-enable it.
      2. If a non-empty tag filter is requested: keep rows whose tags intersect it
         (ANY-match).
      3. If no tag filter: keep all remaining rows.

    Returns ``(question_id, question_text, last_served_at)`` triples.
    """
    out: list[tuple[int, str, str | None]] = []
    for qid, text, tags_json, last_served_at in rows:
        row_tags = _parse_tags(tags_json)
        if "nsfw" in row_tags and not allow_nsfw:      # rule 1 — wins over intersection
            continue
        if requested and not (row_tags & requested):   # rule 2 — ANY-match
            continue
        out.append((qid, text, last_served_at))         # rule 3 — keep
    return out


async def _get_bank_question(
    db,
    game_type: str,
    exclude: list[str] | None = None,
    tags: list[str] | None = None,
    allow_nsfw: bool = False,
) -> str | None:
    """Serve a round-robin bank question for game_type, applying tag rules.

    Prefers the least-recently-served matching row (see
    :func:`_pick_least_recently_served`) and marks it served, so a small pool
    doesn't repeat a question until every row in it has been served once —
    including across separate game sessions.
    """
    rows = await db.fetchall(
        "SELECT question_id, question_text, tags, last_served_at FROM games_question_bank WHERE game_type = ?",
        (game_type,),
    )
    filtered = _filter_bank_rows(rows, set(tags or []), allow_nsfw)
    if exclude:
        filtered = [(qid, text, last) for qid, text, last in filtered if text not in exclude]

    picked = _pick_least_recently_served(filtered)
    if picked is None:
        return None
    qid, text = picked
    await _mark_served(db, qid)
    return text


# The four Traditional Truth-or-Dare categories double as the reserved bank
# tags: every traditional question carries exactly one of these (enforced by
# the web dashboard). The tag *is* the category, so selection is an exact match
# — an sfw category never serves an nsfw question and vice versa.
TRADITIONAL_CATEGORIES: tuple[str, ...] = (
    "sfw_truth", "sfw_dare", "nsfw_truth", "nsfw_dare",
)


async def get_traditional_question(
    db, category: str, exclude: list[str] | None = None,
) -> str | None:
    """Return a round-robin Traditional Truth-or-Dare bank question for *category*.

    *category* is one of :data:`TRADITIONAL_CATEGORIES`; a question matches
    only if its single category tag equals it exactly. *exclude* holds
    question texts already served this game so a bank round doesn't repeat.
    Among the remaining candidates, the least-recently-served row wins (see
    :func:`_pick_least_recently_served`), so a small pool doesn't repeat a
    question across separate games until every row has been served once.

    Bank-only by design (no AI fallback): returns None when the bank has no
    matching, unexcluded question — the cog reports the player as unserved.
    """
    if category not in TRADITIONAL_CATEGORIES:
        return None
    rows = await db.fetchall(
        "SELECT question_id, question_text, tags, last_served_at FROM games_question_bank WHERE game_type = ?",
        ("traditional",),
    )
    seen = set(exclude or ())
    candidates = [
        (qid, text, last)
        for qid, text, tags_json, last in rows
        if category in _parse_tags(tags_json) and text not in seen
    ]
    picked = _pick_least_recently_served(candidates)
    if picked is None:
        return None
    qid, text = picked
    await _mark_served(db, qid)
    return text


async def has_matching_questions(
    db, game_type: str, tags: list[str] | None, allow_nsfw: bool = False,
) -> bool:
    """True if at least one bank row matches the tag filter (same rules as
    _get_bank_question). Used by slash commands to refuse-on-empty-filtered-pool.

    Read-only: unlike the get_* serving functions, this must not mark a
    question served — it's just an existence check.
    """
    rows = await db.fetchall(
        "SELECT question_id, question_text, tags, last_served_at FROM games_question_bank WHERE game_type = ?",
        (game_type,),
    )
    return bool(_filter_bank_rows(rows, set(tags or []), allow_nsfw))


async def get_ffa_prompt(db, kind: str = "random", tags: list[str] | None = None,
                         allow_nsfw: bool = False, exclude: list[str] | None = None):
    """Return (label, text) for an FFA Truth-or-Dare card, or None on a filtered miss.

    'truth'/'dare'/'nsfw' are reserved tags. *kind* ('truth'|'dare'|'random') is a
    required dimension; the host's remaining (free) tags are ANY-match. NSFW cards
    are included by default (*allow_nsfw*); pass ``allow_nsfw=False`` for a clean
    game. Falls back to the code prompt bank only when no tag filter was supplied.

    *exclude* holds prompt texts already shown this game (the "Next" button's
    seen-set); matching bank rows are dropped so a game walks its selected set
    without repeats. Among the remaining candidates, the least-recently-served
    row wins so a small pool doesn't repeat across separate games. When every
    match is excluded the set is exhausted and this returns None — the caller
    resets the seen-set and re-rolls.
    """
    from bot_modules.games_ffa.prompts import pick_prompt, TRUTH, DARE

    rows = await db.fetchall(
        "SELECT question_id, question_text, tags, last_served_at FROM games_question_bank WHERE game_type = ?",
        ("ffa",),
    )
    requested = set(tags or [])
    opted_nsfw = allow_nsfw  # channel age-restriction is authoritative
    free = requested - {"truth", "dare", "nsfw"}    # host's non-reserved filter terms
    want = {"truth"} if kind == "truth" else {"dare"} if kind == "dare" else set()
    seen = set(exclude or ())

    candidates: list[tuple[int, tuple[str, str], str | None]] = []
    for qid, text, tags_json, last_served_at in rows:
        row_tags = _parse_tags(tags_json)
        if "nsfw" in row_tags and not opted_nsfw:
            continue
        if want and not (want & row_tags):           # kind requirement (AND)
            continue
        if free and not (free & row_tags):           # host free tags (ANY-match)
            continue
        if text in seen:                             # already shown this game
            continue
        candidates.append((qid, (DARE if "dare" in row_tags else TRUTH, text), last_served_at))

    picked = _pick_least_recently_served(candidates)
    if picked is not None:
        qid, label_text = picked
        await _mark_served(db, qid)
        return label_text
    if requested or seen:            # tag filter OR exhausted seen-set → refuse (no code fallback)
        return None
    return pick_prompt(kind, False)                  # unfiltered empty → code fallback
