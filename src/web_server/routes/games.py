"""Games admin endpoints — question bank, prompts, history, LegitLibs, config."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_game_host, require_perms, run_query

log = logging.getLogger("dungeonkeeper.games")

router = APIRouter()

_PROMPT_CONFIG_PATH = (
    Path(__file__).parent.parent.parent / "bot_modules" / "games" / "prompt_config.json"
)

VALID_GAME_TYPES = {"wyr", "nhie", "mlt", "rushmore", "price", "clapback", "ama", "photo", "ffa", "traditional", "pen_pals"}

# The cross-game "global pool" lives in games_question_bank under this
# reserved game_type. Gameplay never sees it (every game selects by its own
# type); it's a staging area the dashboard can send questions to and import
# from into any game's bank.
GLOBAL_POOL_TYPE = "global"

# Traditional Truth-or-Dare stores exactly one of these four category tags on
# every question. The tag *is* the category (matching the cog's CATEGORIES), so
# a bank round can serve each player a question in a category they opted into.
TRADITIONAL_CATEGORIES = ("sfw_truth", "sfw_dare", "nsfw_truth", "nsfw_dare")

ALL_GAME_TYPES = [
    "wyr", "nhie", "mlt", "rushmore", "price", "clapback", "ama",
    "traditional", "mfk", "compliment", "ffa", "photo", "ttl", "hottakes",
    "story", "fantasies", "risky_roller",
]


# ── Pydantic models ─────────────────────────────────────────────────────────


class BankCreateBody(BaseModel):
    game_type: str
    tags: list[str] = []
    question_text: str


class BankUpdateBody(BaseModel):
    question_text: Optional[str] = None
    tags: Optional[list[str]] = None


class BankBulkBody(BaseModel):
    game_type: str
    tags: list[str] = []
    lines: list[str]


class BankImportItem(BaseModel):
    game_type: str
    tags: list[str] = []
    question_text: str


class PoolImportBody(BaseModel):
    game_type: str
    question_ids: list[int]
    tags: Optional[list[str]] = None


class PromptsGlobalBody(BaseModel):
    audience: str
    sfw_tone: str
    nsfw_tone: str


class PromptsGameBody(BaseModel):
    descriptor: Optional[str] = None
    user_prompt: Optional[str] = None
    max_tokens: Optional[int] = None


class GenerateBody(BaseModel):
    game_type: str
    category: str
    count: int = 5
    custom_prompt: Optional[str] = None


class ChannelAddBody(BaseModel):
    channel_id: str


class LegitLibsMaxTierBody(BaseModel):
    max_tier: int = Field(ge=1, le=4)


class AuditChannelBody(BaseModel):
    channel_id: str


class GameConfigBody(BaseModel):
    enabled: Optional[bool] = None
    options: Optional[dict] = None


class LegitLibsTemplateBody(BaseModel):
    title: str
    body: str
    tier: int
    tags: str = ""
    status: str = "draft"
    player_min: Optional[int] = None
    player_max: Optional[int] = None
    blanks: Optional[str] = None
    notes: Optional[str] = None


class LegitLibsTemplateUpdateBody(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    tier: Optional[int] = None
    tags: Optional[str] = None
    status: Optional[str] = None
    player_min: Optional[int] = None
    player_max: Optional[int] = None
    blanks: Optional[str] = None
    notes: Optional[str] = None


class LegitLibsResolveBody(BaseModel):
    blanks: list[dict]
    tier: int


class LegitLibsAIPrepBody(BaseModel):
    raw_text: str
    tier: int = 2


# ── Helpers ─────────────────────────────────────────────────────────────────


def _norm_tags(raw) -> list[str]:
    """Dedupe + strip a list of tag strings, preserving first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for t in (raw or []):
        t = str(t).strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _check_bank_type(game_type: str) -> None:
    """Reject unknown bank game_types. The global pool is a valid bank slot
    (so full-bank exports containing pool rows round-trip through import)."""
    if game_type not in VALID_GAME_TYPES and game_type != GLOBAL_POOL_TYPE:
        raise HTTPException(status_code=400, detail=f"Invalid game_type: {game_type}")


def _pool_tags(game_type: str, tags: list[str]) -> list[str]:
    """Translate a question's tags for storage in the global pool.

    Traditional's four category tags are meaningless outside that game, so
    they're dropped — but the NSFW half of the information is preserved as
    the reserved ``nsfw`` tag every game understands.
    """
    if game_type != "traditional":
        return tags
    out = [t for t in tags if t not in TRADITIONAL_CATEGORIES]
    if any(t.startswith("nsfw_") for t in tags) and "nsfw" not in out:
        out.append("nsfw")
    return out


def _validate_traditional_tags(game_type: str, tags: list[str]) -> None:
    """Enforce the Traditional Truth-or-Dare tag contract.

    Every traditional question must carry exactly one of the four category
    tags and nothing else, so a bank round can reliably match it to a
    player's opted-in category. No-op for any other game type.
    """
    if game_type != "traditional":
        return
    if len(tags) != 1 or tags[0] not in TRADITIONAL_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Traditional questions require exactly one category tag "
                "(one of: " + ", ".join(TRADITIONAL_CATEGORIES) + ")."
            ),
        )


def _parse_tags_col(raw) -> list[str]:
    """Parse a stored JSON tags column into a list, tolerating bad data."""
    try:
        val = json.loads(raw or "[]")
        return [str(t) for t in val] if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _load_prompt_config() -> dict:
    if _PROMPT_CONFIG_PATH.exists():
        return json.loads(_PROMPT_CONFIG_PATH.read_text(encoding="utf-8"))
    return {"audience": "", "sfw_tone": "", "nsfw_tone": "", "games": {}}


def _save_prompt_config(cfg: dict) -> None:
    _PROMPT_CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── Stats ────────────────────────────────────────────────────────────────────


@router.get("/stats")
async def get_stats(
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            # games_question_bank is intentionally global/unscoped for now
            # (shared cross-guild library — see migration 122's open question).
            total_q = conn.execute(
                "SELECT COUNT(*) FROM games_question_bank"
            ).fetchone()[0]

            games_played = conn.execute(
                "SELECT COUNT(*) FROM games_game_history WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()[0]

            rounds_played_row = conn.execute(
                "SELECT COALESCE(SUM(round_count), 0) FROM games_game_history WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            rounds_played = rounds_played_row[0] if rounds_played_row else 0

            # Unique players: host_ids + player_ids from payload JSON
            host_rows = conn.execute(
                "SELECT DISTINCT host_id FROM games_game_history"
                " WHERE guild_id = ? AND host_id IS NOT NULL",
                (guild_id,),
            ).fetchall()
            player_ids: set[str] = {str(r[0]) for r in host_rows}

            payload_rows = conn.execute(
                "SELECT payload FROM games_game_history"
                " WHERE guild_id = ? AND payload IS NOT NULL",
                (guild_id,),
            ).fetchall()
            for row in payload_rows:
                try:
                    data = json.loads(row[0])
                    if isinstance(data, dict):
                        pids = data.get("player_ids") or data.get("players") or []
                        if isinstance(pids, list):
                            for pid in pids:
                                player_ids.add(str(pid))
                except (json.JSONDecodeError, TypeError):
                    pass

            unique_players = len(player_ids)

            # Bank by type: {game_type: {sfw: N, nsfw: N}} — nsfw is now the
            # reserved tag, so count it by parsing each row's tags.
            bank_rows = conn.execute(
                "SELECT game_type, tags FROM games_question_bank"
            ).fetchall()
            bank_by_type: dict[str, dict[str, int]] = {}
            for gt, raw in bank_rows:
                is_nsfw = "nsfw" in _parse_tags_col(raw)
                d = bank_by_type.setdefault(gt, {"sfw": 0, "nsfw": 0})
                d["nsfw" if is_nsfw else "sfw"] += 1

            # Games by type: {game_type: N}
            hist_rows = conn.execute(
                "SELECT game_type, COUNT(*) FROM games_game_history"
                " WHERE guild_id = ? GROUP BY game_type",
                (guild_id,),
            ).fetchall()
            games_by_type: dict[str, int] = {gt: cnt for gt, cnt in hist_rows}

            return {
                "total_questions": total_q,
                "games_played": games_played,
                "rounds_played": rounds_played,
                "unique_players": unique_players,
                "bank_by_type": bank_by_type,
                "games_by_type": games_by_type,
            }

    return await run_query(_q)


# ── Question bank ────────────────────────────────────────────────────────────


@router.get("/bank")
async def list_bank(
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
    game_type: Optional[str] = Query(None),
    tag: Optional[List[str]] = Query(None),
    search: Optional[str] = Query(None),
    match: str = Query("all"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            clauses = []
            params: list[object] = []
            if game_type:
                clauses.append("game_type = ?")
                params.append(game_type)
            if search:
                clauses.append("question_text LIKE ?")
                params.append(f"%{search}%")

            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"""SELECT question_id, game_type, tags, question_text, added_at
                    FROM games_question_bank {where}
                    ORDER BY question_id DESC""",
                params,
            ).fetchall()

            # Tag filtering happens in Python (no fragile SQL LIKE on JSON);
            # pagination is recomputed from the filtered set. With multiple
            # requested tags, match="all" keeps rows having every tag (AND);
            # match="any" keeps rows sharing at least one tag (OR).
            requested = {t for t in (tag or []) if t}
            any_match = match == "any"
            items = []
            for r in rows:
                tags = _parse_tags_col(r[2])
                if requested:
                    inter = requested & set(tags)
                    if any_match:
                        if not inter:
                            continue
                    elif inter != requested:
                        continue
                items.append(
                    {
                        "question_id": r[0],
                        "game_type": r[1],
                        "tags": tags,
                        "question_text": r[3],
                        "added_at": r[4],
                    }
                )

            total = len(items)
            offset = (page - 1) * per_page
            page_items = items[offset:offset + per_page]
            total_pages = max(1, -(-total // per_page))
            return {
                "questions": page_items,
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
            }

    return await run_query(_q)


@router.post("/bank")
async def create_question(
    request: Request,
    body: BankCreateBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    _check_bank_type(body.game_type)

    ctx = get_ctx(request)
    tags = _norm_tags(body.tags)
    _validate_traditional_tags(body.game_type, tags)
    tags_json = json.dumps(tags)

    def _q():
        with ctx.open_db() as conn:
            cur = conn.execute(
                "INSERT INTO games_question_bank (game_type, tags, question_text) VALUES (?, ?, ?)",
                (body.game_type, tags_json, body.question_text.strip()),
            )
            conn.commit()
            return {"question_id": cur.lastrowid}

    return await run_query(_q)


@router.put("/bank/{question_id}")
async def update_question(
    request: Request,
    question_id: int,
    body: BankUpdateBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            existing = conn.execute(
                "SELECT game_type FROM games_question_bank WHERE question_id = ?",
                (question_id,),
            ).fetchone()
            if not existing:
                return None

            sets = []
            params: list[object] = []
            if body.question_text is not None:
                sets.append("question_text = ?")
                params.append(body.question_text.strip())
            if body.tags is not None:
                tags = _norm_tags(body.tags)
                _validate_traditional_tags(existing[0], tags)
                sets.append("tags = ?")
                params.append(json.dumps(tags))

            if sets:
                params.append(question_id)
                conn.execute(
                    f"UPDATE games_question_bank SET {', '.join(sets)} WHERE question_id = ?",
                    params,
                )
                conn.commit()
            return {}

    result = await run_query(_q)
    if result is None:
        raise HTTPException(status_code=404, detail="Question not found")
    return result


@router.delete("/bank/{question_id}")
async def delete_question(
    request: Request,
    question_id: int,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            existing = conn.execute(
                "SELECT question_id FROM games_question_bank WHERE question_id = ?",
                (question_id,),
            ).fetchone()
            if not existing:
                return None
            conn.execute(
                "DELETE FROM games_question_bank WHERE question_id = ?", (question_id,)
            )
            conn.commit()
            return {}

    result = await run_query(_q)
    if result is None:
        raise HTTPException(status_code=404, detail="Question not found")
    return result


@router.post("/bank/bulk")
async def bulk_add_questions(
    request: Request,
    body: BankBulkBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    _check_bank_type(body.game_type)

    lines = [line.strip() for line in body.lines if line.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="No non-empty lines provided")

    ctx = get_ctx(request)
    tags = _norm_tags(body.tags)
    _validate_traditional_tags(body.game_type, tags)
    tags_json = json.dumps(tags)

    def _q():
        with ctx.open_db() as conn:
            conn.executemany(
                "INSERT INTO games_question_bank (game_type, tags, question_text) VALUES (?, ?, ?)",
                [(body.game_type, tags_json, line) for line in lines],
            )
            conn.commit()
            return {"added": len(lines)}

    return await run_query(_q)


# ── Global pool ──────────────────────────────────────────────────────────────
# Pool rows are ordinary bank rows under GLOBAL_POOL_TYPE, so listing, editing
# and deleting them reuses the /bank endpoints (game_type=global). Only the
# two copy directions need dedicated routes.


@router.post("/bank/{question_id}/pool")
async def send_to_pool(
    request: Request,
    question_id: int,
    _: AuthenticatedUser = Depends(require_game_host),
):
    """Copy one bank question into the global pool (the original stays put).

    Duplicate texts already in the pool are not re-added — the response says
    which happened so the UI can report it.
    """
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            row = conn.execute(
                "SELECT game_type, tags, question_text FROM games_question_bank"
                " WHERE question_id = ?",
                (question_id,),
            ).fetchone()
            if not row:
                return None
            game_type, tags_raw, text = row
            if game_type == GLOBAL_POOL_TYPE:
                return {"error": "Question is already in the global pool"}
            text = text.strip()
            dup = conn.execute(
                "SELECT 1 FROM games_question_bank"
                " WHERE game_type = ? AND TRIM(question_text) = ? LIMIT 1",
                (GLOBAL_POOL_TYPE, text),
            ).fetchone()
            if dup:
                return {"sent": False, "duplicate": True}
            tags = _pool_tags(game_type, _parse_tags_col(tags_raw))
            conn.execute(
                "INSERT INTO games_question_bank (game_type, tags, question_text) VALUES (?, ?, ?)",
                (GLOBAL_POOL_TYPE, json.dumps(tags), text),
            )
            conn.commit()
            return {"sent": True, "duplicate": False}

    result = await run_query(_q)
    if result is None:
        raise HTTPException(status_code=404, detail="Question not found")
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/bank/pool/import")
async def import_from_pool(
    request: Request,
    body: PoolImportBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    """Copy selected global-pool questions into a game's bank (pool keeps them).

    ``tags``, when given, replaces each copy's tags (Traditional requires it:
    exactly one category tag); when omitted the pool row's tags carry over.
    Questions whose text already exists in the target bank are skipped.
    """
    _check_bank_type(body.game_type)
    if body.game_type == GLOBAL_POOL_TYPE:
        raise HTTPException(status_code=400, detail="Cannot import the pool into itself")
    if not body.question_ids:
        raise HTTPException(status_code=400, detail="No questions selected")

    override = _norm_tags(body.tags) if body.tags is not None else None
    if body.game_type == "traditional" or override is not None:
        _validate_traditional_tags(body.game_type, override or [])

    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            placeholders = ",".join("?" for _ in body.question_ids)
            rows = conn.execute(
                f"SELECT tags, question_text FROM games_question_bank"
                f" WHERE game_type = ? AND question_id IN ({placeholders})",
                [GLOBAL_POOL_TYPE, *body.question_ids],
            ).fetchall()
            existing = {
                (t or "").strip()
                for (t,) in conn.execute(
                    "SELECT question_text FROM games_question_bank WHERE game_type = ?",
                    (body.game_type,),
                ).fetchall()
            }
            to_add: list[tuple[str, str, str]] = []
            skipped = 0
            for tags_raw, text in rows:
                text = text.strip()
                if text in existing:
                    skipped += 1
                    continue
                existing.add(text)
                tags = override if override is not None else _parse_tags_col(tags_raw)
                to_add.append((body.game_type, json.dumps(tags), text))
            if to_add:
                conn.executemany(
                    "INSERT INTO games_question_bank (game_type, tags, question_text) VALUES (?, ?, ?)",
                    to_add,
                )
                conn.commit()
            return {"imported": len(to_add), "skipped": skipped}

    return await run_query(_q)


@router.get("/bank/export")
async def export_bank(
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
    game_type: Optional[str] = Query(None),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            if game_type:
                rows = conn.execute(
                    "SELECT game_type, tags, question_text FROM games_question_bank "
                    "WHERE game_type = ? ORDER BY question_id",
                    (game_type,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT game_type, tags, question_text FROM games_question_bank "
                    "ORDER BY game_type, question_id"
                ).fetchall()
            return [
                {"game_type": r[0], "tags": _parse_tags_col(r[1]), "question_text": r[2]}
                for r in rows
            ]

    return await run_query(_q)


@router.post("/bank/import")
async def import_bank(
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
):
    raw = await request.json()
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="Body must be a JSON array")

    items: list[tuple[str, str, str]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail=f"Item {i} is not an object")
        gt = entry.get("game_type", "")
        text = entry.get("question_text", "").strip()
        if gt not in VALID_GAME_TYPES and gt != GLOBAL_POOL_TYPE:
            raise HTTPException(status_code=400, detail=f"Item {i}: invalid game_type '{gt}'")
        if not text:
            continue
        # Old exports without a "tags" key default to []. Legacy "category":"nsfw"
        # is still honored as the nsfw tag for backward compatibility.
        tags = _norm_tags(entry.get("tags", []))
        if not tags and entry.get("category") == "nsfw":
            tags = ["nsfw"]
        _validate_traditional_tags(gt, tags)
        items.append((gt, json.dumps(tags), text))

    if not items:
        return {"imported": 0}

    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            conn.executemany(
                "INSERT INTO games_question_bank (game_type, tags, question_text) VALUES (?, ?, ?)",
                items,
            )
            conn.commit()
            return {"imported": len(items)}

    return await run_query(_q)


@router.get("/bank/tags")
async def list_bank_tags(
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
    game_type: Optional[str] = Query(None),
):
    """Distinct tags currently in use (optionally for one game_type) — autocomplete."""
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            if game_type:
                rows = conn.execute(
                    "SELECT tags FROM games_question_bank WHERE game_type = ?",
                    (game_type,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT tags FROM games_question_bank").fetchall()
            seen: set[str] = set()
            for (raw,) in rows:
                for t in _parse_tags_col(raw):
                    t = t.strip()
                    if t:
                        seen.add(t)
            return {"tags": sorted(seen)}

    return await run_query(_q)


# ── Prompts ──────────────────────────────────────────────────────────────────


@router.get("/prompts")
async def get_prompts(
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
):
    return _load_prompt_config()


@router.put("/prompts/global")
async def update_global_prompts(
    request: Request,
    body: PromptsGlobalBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    cfg = _load_prompt_config()
    cfg["audience"] = body.audience
    cfg["sfw_tone"] = body.sfw_tone
    cfg["nsfw_tone"] = body.nsfw_tone
    _save_prompt_config(cfg)
    return {}


@router.put("/prompts/game/{game_type}")
async def update_game_prompt(
    request: Request,
    game_type: str,
    body: PromptsGameBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    if game_type not in VALID_GAME_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid game_type: {game_type}")

    cfg = _load_prompt_config()
    games = cfg.setdefault("games", {})
    entry = games.setdefault(game_type, {})

    if body.descriptor is not None:
        entry["descriptor"] = body.descriptor
    if body.user_prompt is not None:
        entry["user_prompt"] = body.user_prompt
    if body.max_tokens is not None:
        entry["max_tokens"] = body.max_tokens

    _save_prompt_config(cfg)
    return {}


# ── AI generation ─────────────────────────────────────────────────────────────


@router.post("/generate")
async def generate_questions(
    request: Request,
    body: GenerateBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    from bot_modules.games.utils.ai_client import generate_text

    if body.game_type not in VALID_GAME_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid game_type: {body.game_type}")
    if body.category not in ("sfw", "nsfw"):
        raise HTTPException(status_code=400, detail="category must be 'sfw' or 'nsfw'")

    count = max(1, min(20, body.count))
    cfg = _load_prompt_config()

    audience = cfg.get("audience", "")
    tone = cfg.get("nsfw_tone" if body.category == "nsfw" else "sfw_tone", "")
    game_cfg = cfg.get("games", {}).get(body.game_type, {})
    descriptor = game_cfg.get("descriptor", body.game_type)
    base_user_prompt = game_cfg.get("user_prompt", f"Generate one {descriptor} question.")
    max_tokens = game_cfg.get("max_tokens", 200)

    system_prompt = f"{audience}\n\n{tone}"
    raw_prompt = body.custom_prompt if body.custom_prompt else base_user_prompt
    batch_mode = "{N}" in raw_prompt and not body.custom_prompt
    user_prompt = raw_prompt.replace("{N}", str(count)) if batch_mode else raw_prompt

    results = []
    errors = 0
    iterations = 1 if batch_mode else count
    for _i in range(iterations):
        text = await generate_text(system_prompt, user_prompt, max_tokens=max_tokens)
        if text:
            results.extend(_split_generated(text))
        else:
            errors += 1

    response: dict = {"results": results}
    if errors:
        response["error"] = f"{errors} generation(s) failed; check ANTHROPIC_API_KEY"
    return response


def _split_generated(text: str) -> list[str]:
    """Split a multi-line AI response into individual question strings."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip leading list markers: "1.", "2)", "-", "•", "*"
        line = line.lstrip("-•* \t")
        line = re.sub(r"^\d+[.)]\s*", "", line)
        line = line.strip('"').strip()
        if line:
            lines.append(line)
    return lines if lines else [text.strip()]


# ── Game history ──────────────────────────────────────────────────────────────


@router.get("/history")
async def get_history(
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    game_type: Optional[str] = Query(None),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            clauses = ["guild_id = ?"]
            params: list[object] = [guild_id]
            if game_type:
                clauses.append("game_type = ?")
                params.append(game_type)

            where = "WHERE " + " AND ".join(clauses)
            total = conn.execute(
                f"SELECT COUNT(*) FROM games_game_history {where}", params
            ).fetchone()[0]

            offset = (page - 1) * per_page
            rows = conn.execute(
                f"""SELECT history_id, game_type, player_count, round_count, started_at, ended_at
                    FROM games_game_history {where}
                    ORDER BY history_id DESC
                    LIMIT ? OFFSET ?""",
                [*params, per_page, offset],
            ).fetchall()

            result_rows = [
                {
                    "history_id": r[0],
                    "game_type": r[1],
                    "player_count": r[2],
                    "round_count": r[3],
                    "started_at": r[4],
                    "ended_at": r[5],
                }
                for r in rows
            ]
            total_pages = max(1, -(-total // per_page))
            return {
                "rows": result_rows,
                "total": total,
                "page": page,
                "total_pages": total_pages,
            }

    return await run_query(_q)


# ── LegitLibs templates ───────────────────────────────────────────────────────


@router.get("/legitlibs/templates")
async def list_ll_templates(
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
    tier: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            # This guild's own templates plus the shared global pool (guild_id 0).
            clauses = ["(guild_id = ? OR guild_id = 0)"]
            params: list[object] = [guild_id]
            if tier is not None:
                clauses.append("tier = ?")
                params.append(tier)
            if status:
                clauses.append("status = ?")
                params.append(status)

            where = "WHERE " + " AND ".join(clauses)
            rows = conn.execute(
                f"""SELECT template_id, title, tier, status, tags,
                           player_min, player_max, use_count, guild_id
                    FROM legitlibs_templates {where}
                    ORDER BY template_id DESC""",
                params,
            ).fetchall()

            # Count blanks from the blanks JSON column
            templates = []
            for r in rows:
                # Get the blanks column to count entries
                blanks_row = conn.execute(
                    "SELECT blanks FROM legitlibs_templates WHERE template_id = ?",
                    (r[0],),
                ).fetchone()
                blanks_count = 0
                if blanks_row and blanks_row[0]:
                    try:
                        b = json.loads(blanks_row[0])
                        blanks_count = len(b) if isinstance(b, list) else 0
                    except (json.JSONDecodeError, TypeError):
                        pass
                templates.append({
                    "template_id": r[0],
                    "title": r[1],
                    "tier": r[2],
                    "status": r[3],
                    "tags": [t.strip() for t in r[4].split(",") if t.strip()] if r[4] else [],
                    "player_min": r[5],
                    "player_max": r[6],
                    "use_count": r[7],
                    "blanks_count": blanks_count,
                    # 0 = shared global pool; otherwise this guild owns it.
                    "is_global": r[8] == 0,
                })
            return {"templates": templates}

    return await run_query(_q)


@router.get("/legitlibs/templates/{template_id}")
async def get_ll_template(
    request: Request,
    template_id: int,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            row = conn.execute(
                "SELECT * FROM legitlibs_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            # Parse JSON fields
            for field in ("tags", "blanks"):
                if d.get(field) and isinstance(d[field], str):
                    try:
                        d[field] = json.loads(d[field])
                    except (json.JSONDecodeError, TypeError):
                        pass
            return d

    result = await run_query(_q)
    if result is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return result


def _players_from_blanks(blanks_json: str | None) -> tuple[int | None, int | None]:
    """Return (player_min, player_max) derived from a blanks JSON string.

    Each player fills 5–10 blanks, so:
      player_min = ceil(count / 10)  — keeps each player under 10
      player_max = floor(count / 5)  — keeps each player over 5
    """
    if not blanks_json:
        return None, None
    try:
        blanks = json.loads(blanks_json)
        count = len(blanks) if isinstance(blanks, list) else 0
    except (json.JSONDecodeError, TypeError):
        return None, None
    if count == 0:
        return None, None
    import math
    return math.ceil(count / 10), max(1, count // 5)


@router.post("/legitlibs/templates")
async def create_ll_template(
    request: Request,
    body: LegitLibsTemplateBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        player_min, player_max = _players_from_blanks(body.blanks)
        with ctx.open_db() as conn:
            # New templates belong to the creating guild; promoting to the shared
            # global pool (guild_id = 0) is a deliberate later action.
            cur = conn.execute(
                """INSERT INTO legitlibs_templates
                   (title, body, tier, tags, status, player_min, player_max, blanks, notes, guild_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    body.title,
                    body.body,
                    body.tier,
                    body.tags,
                    body.status,
                    player_min,
                    player_max,
                    body.blanks,
                    body.notes,
                    guild_id,
                ),
            )
            conn.commit()
            return {"template_id": cur.lastrowid}

    return await run_query(_q)


@router.put("/legitlibs/templates/{template_id}")
async def update_ll_template(
    request: Request,
    template_id: int,
    body: LegitLibsTemplateUpdateBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            # Only the owning guild (or anyone, for a shared global template) may
            # edit — a guild can't reach into another guild's templates.
            existing = conn.execute(
                "SELECT template_id FROM legitlibs_templates "
                "WHERE template_id = ? AND (guild_id = ? OR guild_id = 0)",
                (template_id, guild_id),
            ).fetchone()
            if not existing:
                return None

            fields = body.model_dump(exclude_none=True)
            if "blanks" in fields:
                p_min, p_max = _players_from_blanks(fields["blanks"])
                fields["player_min"] = p_min
                fields["player_max"] = p_max
            sets = []
            params: list[object] = []
            for field, value in fields.items():
                sets.append(f"{field} = ?")
                params.append(value)

            if sets:
                params.append(template_id)
                conn.execute(
                    f"UPDATE legitlibs_templates SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP WHERE template_id = ?",
                    params,
                )
                conn.commit()
            return {}

    result = await run_query(_q)
    if result is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return result


@router.delete("/legitlibs/templates/{template_id}")
async def delete_ll_template(
    request: Request,
    template_id: int,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            existing = conn.execute(
                "SELECT template_id FROM legitlibs_templates "
                "WHERE template_id = ? AND (guild_id = ? OR guild_id = 0)",
                (template_id, guild_id),
            ).fetchone()
            if not existing:
                return None
            conn.execute(
                "DELETE FROM legitlibs_templates WHERE template_id = ? "
                "AND (guild_id = ? OR guild_id = 0)",
                (template_id, guild_id),
            )
            conn.commit()
            return {}

    result = await run_query(_q)
    if result is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return result


class LegitLibsScopeBody(BaseModel):
    is_global: bool


@router.put("/legitlibs/templates/{template_id}/scope")
async def set_ll_template_scope(
    request: Request,
    template_id: int,
    body: LegitLibsScopeBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    """Promote a template to the shared global pool, or claim it back to this guild.

    ``is_global`` true sets ``guild_id = 0`` (every guild draws it); false sets it
    to the active guild (server-only). A guild may only re-scope a template it
    already owns or a global one — never another guild's.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    new_owner = 0 if body.is_global else guild_id

    def _q():
        with ctx.open_db() as conn:
            existing = conn.execute(
                "SELECT template_id FROM legitlibs_templates "
                "WHERE template_id = ? AND (guild_id = ? OR guild_id = 0)",
                (template_id, guild_id),
            ).fetchone()
            if not existing:
                return None
            conn.execute(
                "UPDATE legitlibs_templates "
                "SET guild_id = ?, updated_at = CURRENT_TIMESTAMP WHERE template_id = ?",
                (new_owner, template_id),
            )
            conn.commit()
            return {"is_global": new_owner == 0}

    result = await run_query(_q)
    if result is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return result


@router.get("/legitlibs/axes")
async def get_ll_axes(
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            rows = conn.execute(
                "SELECT axis, value, parent_pos, min_tier FROM legitlibs_blank_axes ORDER BY axis, value"
            ).fetchall()

            pos_values: list[dict] = []
            domains_by_pos: dict[str, list[dict]] = {}
            forms_by_pos: dict[str, list[dict]] = {}

            for r in rows:
                axis, value, parent_pos, min_tier = r[0], r[1], r[2], r[3]
                if axis == "pos":
                    pos_values.append({"value": value, "min_tier": min_tier})
                elif axis == "domain" and parent_pos:
                    domains_by_pos.setdefault(parent_pos, []).append({"value": value, "min_tier": min_tier})
                elif axis == "form" and parent_pos:
                    forms_by_pos.setdefault(parent_pos, []).append({"value": value, "min_tier": min_tier})

            return {
                "pos_values": pos_values,
                "domains_by_pos": domains_by_pos,
                "forms_by_pos": forms_by_pos,
            }

    return await run_query(_q)


@router.post("/legitlibs/ai-prep")
async def ll_ai_prep(
    request: Request,
    body: LegitLibsAIPrepBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    import json as _json
    import re as _re
    from bot_modules.games.utils.ai_client import generate_text
    from bot_modules.cogs.games_legitlibs.validation import validate_template

    raw_text = body.raw_text.strip()
    if not raw_text:
        raise HTTPException(400, "raw_text is required")
    if len(raw_text) > 8000:
        raise HTTPException(400, "Text too long (max 8000 characters)")

    tier = max(1, min(4, body.tier))

    noun_domains = ["place", "person"] + (["body"] if tier >= 2 else []) + (["kink"] if tier >= 4 else [])
    verb_domains = ["intimate"] if tier >= 3 else []
    domain_lines = f"noun domains: {', '.join(noun_domains)}"
    if verb_domains:
        domain_lines += f"; verb domains: {', '.join(verb_domains)}"

    paragraph_count = max(1, len([p for p in raw_text.split("\n") if p.strip()]))
    min_blanks = min(max(4, paragraph_count * 2), 24)
    max_blanks = min(max(min_blanks + 4, paragraph_count * 4), 32)

    system_prompt = f"""You are a Mad Libs template editor. Given a passage of text and a heat tier (1=Flirty, 2=Spicy, 3=Filthy, 4=Unhinged), identify {min_blanks}–{max_blanks} words or short phrases to become fill-in-the-blank slots. Aim for at least 2 blanks per paragraph so the whole passage stays funny.

Output ONLY a compact JSON object listing the blanks — do NOT echo or repeat the passage text.

Rules:
- "phrase" must be the exact word or phrase as it appears in the passage (use the inflected form; no surrounding punctuation).
- Choose nouns, verbs, adjectives, adverbs, numbers, or exclamations. Prefer concrete, evocative words.
- Do NOT choose articles (a, an, the), prepositions, pronouns, or conjunctions.
- Each blank must have a unique id (sequential integer string starting at "1").
- If the same proper noun appears multiple times, use ONE blank entry — it will replace all occurrences.
- Never reuse an id for two different words/concepts.
- pos must be one of: noun, verb, adjective, adverb, exclamation, number, wildcard. NEVER use a domain name as pos.
- Use wildcard only when intentionally open-ended.
- Available {domain_lines}. Domain is a sub-type of pos, set separately: e.g. a person's name → {{"pos": "noun", "domain": "person"}}; a city → {{"pos": "noun", "domain": "place"}}.
- Available verb forms: ing, past, infinitive; noun forms: plural
- Only include domain or form when clearly applicable.
- Output ONLY valid JSON — no markdown fences, no commentary.

JSON format:
{{"blanks": [{{"id": "1", "phrase": "wicked", "pos": "adjective"}}, {{"id": "2", "phrase": "Jim", "pos": "noun", "domain": "person"}}, ...]}}"""

    user_prompt = f"Tier: {tier}\n\nText: {raw_text}"

    # Output is just the blanks list. Budget ~150 chars/token conservatively, 120 chars per blank.
    max_out = min(4000, max(800, max_blanks * 120))
    raw = await generate_text(system_prompt, user_prompt, max_tokens=max_out, temperature=0.2)
    if not raw:
        raise HTTPException(502, "AI prep failed — check ANTHROPIC_API_KEY and try again")

    def _extract_json(text: str) -> dict | None:
        text = _re.sub(r"^```(?:json)?\s*", "", text.strip())
        text = _re.sub(r"\s*```$", "", text.strip()).strip()
        decoder = _json.JSONDecoder()
        try:
            obj, _ = decoder.raw_decode(text)
            return obj
        except _json.JSONDecodeError:
            pass
        start = text.find("{")
        if start != -1:
            try:
                obj, _ = decoder.raw_decode(text[start:])
                return obj
            except _json.JSONDecodeError:
                pass
        return None

    parsed = _extract_json(raw)
    if parsed is None:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "AI prep bad JSON (len=%d), start=%r, end=%r", len(raw), raw[:400], raw[-200:]
        )
        raise HTTPException(502, "AI returned malformed JSON — try again")

    if not isinstance(parsed.get("blanks"), list):
        raise HTTPException(502, "AI response missing required fields — try again")

    # Apply substitutions to build the body. Longest phrases first to avoid partial matches.
    blanks: list[dict] = parsed["blanks"]
    body_text = raw_text
    for blank in sorted(blanks, key=lambda b: len(b.get("phrase", "")), reverse=True):
        phrase = blank.get("phrase", "").strip()
        if not phrase:
            continue
        marker = "{" + str(blank.get("id", "")) + "}"
        body_text = _re.sub(r"\b" + _re.escape(phrase) + r"\b", marker, body_text, flags=_re.IGNORECASE)

    # Re-number markers by order of appearance so IDs are always sequential from 1.
    blanks_by_id = {str(b.get("id", "")): b for b in blanks}
    seen_order: list[str] = []
    for m in _re.finditer(r"\{(\d+)\}", body_text):
        old_id = m.group(1)
        if old_id not in seen_order:
            seen_order.append(old_id)
    remap = {old: str(new) for new, old in enumerate(seen_order, 1)}
    body_text = _re.sub(r"\{(\d+)\}", lambda m: "{" + remap.get(m.group(1), m.group(1)) + "}", body_text)
    new_blanks = []
    for old_id in seen_order:
        blank = blanks_by_id.get(old_id, {}).copy()
        blank.pop("phrase", None)
        blank["id"] = remap[old_id]
        new_blanks.append(blank)
    parsed["body"] = body_text
    parsed["blanks"] = new_blanks

    ctx = get_ctx(request)

    def _get_axes():
        with ctx.open_db() as conn:
            rows = conn.execute(
                "SELECT axis, value, parent_pos, min_tier FROM legitlibs_blank_axes"
            ).fetchall()
            axes: dict = {"pos": [], "domains": {}, "forms": {}}
            for r in rows:
                axis, value, parent_pos, min_tier = r[0], r[1], r[2], r[3]
                if axis == "pos":
                    axes["pos"].append({"value": value, "min_tier": min_tier})
                elif axis == "domain" and parent_pos:
                    axes["domains"].setdefault(parent_pos, []).append({"value": value, "min_tier": min_tier})
                elif axis == "form" and parent_pos:
                    axes["forms"].setdefault(parent_pos, []).append({"value": value, "min_tier": min_tier})
            return axes

    axes = await run_query(_get_axes)

    errors = validate_template(parsed["body"], parsed["blanks"], tier, axes)
    if errors:
        raise HTTPException(422, f"AI output failed validation: {'; '.join(errors)}")

    return {"body": parsed["body"], "blanks": parsed["blanks"]}


@router.post("/legitlibs/resolve")
async def resolve_ll_blanks(
    request: Request,
    body: LegitLibsResolveBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)

    def _q():
        from bot_modules.cogs.games_legitlibs.data import resolve_blank

        with ctx.open_db() as conn:
            rows = conn.execute(
                "SELECT pos, domain, form, tier, prompt, examples, length_cap"
                " FROM legitlibs_blank_prompts"
            ).fetchall()
            prompts = {
                (r["pos"], r["domain"], r["form"], r["tier"]): {
                    "prompt": r["prompt"],
                    "examples": json.loads(r["examples"]),
                    "length_cap": r["length_cap"],
                }
                for r in rows
            }
            out = []
            for b in body.blanks:
                pos = b.get("pos", "")
                domain = b.get("domain") or None
                form = b.get("form") or None
                parts = [pos or "?"]
                if domain:
                    parts.append(domain)
                if form:
                    parts.append(form)
                axis_label = " · ".join(parts).replace("_", " ")
                if not pos:
                    out.append({
                        "marker": b.get("id", "?"),
                        "axis_label": axis_label,
                        "prompt": None,
                        "examples_preview": "",
                        "error": "missing POS",
                    })
                    continue
                resolved = resolve_blank(prompts, pos, domain, form, body.tier)
                if resolved is None:
                    out.append({
                        "marker": b.get("id", "?"),
                        "axis_label": axis_label,
                        "prompt": None,
                        "examples_preview": "",
                        "error": "no prompt for this combination",
                    })
                else:
                    examples = resolved["examples"][:3]
                    out.append({
                        "marker": b.get("id", "?"),
                        "axis_label": axis_label,
                        "prompt": resolved["prompt"],
                        "examples_preview": ", ".join(f'"{e}"' for e in examples),
                        "error": None,
                    })
            return {"resolutions": out}

    return await run_query(_q)


# ── Channel config ────────────────────────────────────────────────────────────


@router.get("/config/channels")
async def get_allowed_channels(
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            rows = conn.execute(
                """
                SELECT a.channel_id, a.added_by, a.added_at, l.max_tier
                FROM games_allowed_channels a
                LEFT JOIN legitlibs_channel_config l ON l.channel_id = a.channel_id
                WHERE a.guild_id = ?
                ORDER BY a.added_at DESC
                """,
                (guild_id,),
            ).fetchall()
            return {
                "channels": [
                    {
                        # Snowflakes as strings: past 2^53 a bare JSON number
                        # rounds in the browser, and this id is written straight
                        # back into the per-channel tier/remove requests.
                        "channel_id": str(r[0]),
                        "added_by": str(r[1]),
                        "added_at": r[2],
                        "legitlibs_max_tier": r[3] if r[3] is not None else 4,
                    }
                    for r in rows
                ]
            }

    return await run_query(_q)


@router.post("/config/channels")
async def add_allowed_channel(
    request: Request,
    body: ChannelAddBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO games_allowed_channels (channel_id, guild_id)"
                " VALUES (?, ?)",
                (body.channel_id, guild_id),
            )
            conn.commit()
            return {}

    return await run_query(_q)


@router.delete("/config/channels/{channel_id}")
async def remove_allowed_channel(
    request: Request,
    channel_id: str,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            # Scope by guild so a host of guild A can't delete guild B's rows.
            conn.execute(
                "DELETE FROM games_allowed_channels WHERE channel_id = ? AND guild_id = ?",
                (channel_id, guild_id),
            )
            conn.commit()
            return {}

    return await run_query(_q)


@router.put("/config/channels/{channel_id}/legitlibs-max-tier")
async def set_legitlibs_channel_max_tier(
    request: Request,
    channel_id: str,
    body: LegitLibsMaxTierBody,
    user: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            conn.execute(
                """
                INSERT INTO legitlibs_channel_config (channel_id, max_tier, set_by)
                VALUES (?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    max_tier = excluded.max_tier,
                    set_by = excluded.set_by,
                    set_at = CURRENT_TIMESTAMP
                """,
                (channel_id, body.max_tier, user.user_id),
            )
            conn.commit()
            return {}

    return await run_query(_q)


# ── Per-game config ───────────────────────────────────────────────────────────


@router.get("/config/games")
async def get_all_game_configs(
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            rows = conn.execute(
                "SELECT game_type, enabled, options FROM games_game_config WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
            stored = {r[0]: {"enabled": bool(r[1]), "options": json.loads(r[2] or "{}")} for r in rows}
            games = {}
            for gt in ALL_GAME_TYPES:
                cfg = stored.get(gt, {})
                games[gt] = {
                    "enabled": cfg.get("enabled", True),
                    "options": cfg.get("options", {}),
                }
            return {"games": games}

    return await run_query(_q)


@router.get("/config/games/{game_type}")
async def get_game_config(
    request: Request,
    game_type: str,
    _: AuthenticatedUser = Depends(require_game_host),
):
    if game_type not in ALL_GAME_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown game_type: {game_type}")

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = conn.execute(
                "SELECT enabled, options FROM games_game_config WHERE guild_id = ? AND game_type = ?",
                (guild_id, game_type),
            ).fetchone()
            if not row:
                return {"game_type": game_type, "enabled": True, "options": {}}
            return {
                "game_type": game_type,
                "enabled": bool(row[0]),
                "options": json.loads(row[1] or "{}"),
            }

    return await run_query(_q)


@router.put("/config/games/{game_type}")
async def set_game_config(
    request: Request,
    game_type: str,
    body: GameConfigBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    if game_type not in ALL_GAME_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown game_type: {game_type}")

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = conn.execute(
                "SELECT enabled, options FROM games_game_config WHERE guild_id = ? AND game_type = ?",
                (guild_id, game_type),
            ).fetchone()

            if row:
                new_enabled = int(body.enabled) if body.enabled is not None else row[0]
                existing_opts = json.loads(row[1] or "{}")
                if body.options is not None:
                    existing_opts.update(body.options)
                conn.execute(
                    "UPDATE games_game_config SET enabled = ?, options = ?, updated_at = CURRENT_TIMESTAMP"
                    " WHERE guild_id = ? AND game_type = ?",
                    (new_enabled, json.dumps(existing_opts), guild_id, game_type),
                )
            else:
                new_enabled = int(body.enabled) if body.enabled is not None else 1
                new_opts = body.options or {}
                conn.execute(
                    "INSERT INTO games_game_config (guild_id, game_type, enabled, options) VALUES (?, ?, ?, ?)",
                    (guild_id, game_type, new_enabled, json.dumps(new_opts)),
                )
            conn.commit()
            return {}

    return await run_query(_q)


# ── Audit channel config ──────────────────────────────────────────────────────


@router.get("/config/audit")
async def get_audit_channel(
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = conn.execute(
                "SELECT guild_id, channel_id FROM games_audit_channel WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            if not row:
                return None
            return {"guild_id": row[0], "channel_id": row[1]}

    return await run_query(_q)


@router.put("/config/audit")
async def set_audit_channel(
    request: Request,
    body: AuditChannelBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            existing = conn.execute(
                "SELECT 1 FROM games_audit_channel WHERE guild_id = ? LIMIT 1",
                (guild_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE games_audit_channel SET channel_id = ? WHERE guild_id = ?",
                    (body.channel_id, guild_id),
                )
            else:
                conn.execute(
                    "INSERT INTO games_audit_channel (guild_id, channel_id) VALUES (?, ?)",
                    (guild_id, body.channel_id),
                )
            conn.commit()
            return {}

    return await run_query(_q)


# ── Game host role management ──────────────────────────────────────────────────


class EditorRoleBody(BaseModel):
    role_id: str


@router.get("/config/editor-role")
async def get_editor_role(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = conn.execute(
                "SELECT role_id FROM games_editor_role WHERE guild_id = ?", (guild_id,)
            ).fetchone()
            return {"role_id": str(row["role_id"])} if row else {"role_id": None}

    return await run_query(_q)


@router.put("/config/editor-role")
async def set_editor_role(
    request: Request,
    body: EditorRoleBody,
    user: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    try:
        role_id = int(body.role_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="role_id must be a numeric snowflake")

    def _q():
        with ctx.open_db() as conn:
            conn.execute(
                """INSERT INTO games_editor_role (guild_id, role_id, set_by)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET role_id = excluded.role_id,
                       set_by = excluded.set_by, set_at = CURRENT_TIMESTAMP""",
                (guild_id, role_id, user.user_id),
            )
            conn.commit()
            return {"role_id": str(role_id)}

    return await run_query(_q)


@router.delete("/config/editor-role")
async def clear_editor_role(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            conn.execute(
                "DELETE FROM games_editor_role WHERE guild_id = ?", (guild_id,)
            )
            conn.commit()
            return {}

    return await run_query(_q)
