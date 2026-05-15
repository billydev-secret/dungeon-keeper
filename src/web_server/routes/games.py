"""Games admin endpoints — question bank, prompts, history, LegitLibs, config."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from web_server.auth import AuthenticatedUser
from web_server.deps import get_ctx, require_perms, run_query

log = logging.getLogger("dungeonkeeper.games")

router = APIRouter()

_PROMPT_CONFIG_PATH = (
    Path(__file__).parent.parent.parent / "bot_modules" / "games" / "prompt_config.json"
)

VALID_GAME_TYPES = {"wyr", "nhie", "mlt", "rushmore", "price", "clapback", "ama"}


# ── Pydantic models ─────────────────────────────────────────────────────────


class BankCreateBody(BaseModel):
    game_type: str
    category: str
    question_text: str


class BankUpdateBody(BaseModel):
    question_text: Optional[str] = None
    category: Optional[str] = None


class BankBulkBody(BaseModel):
    game_type: str
    category: str
    lines: list[str]


class BankImportItem(BaseModel):
    game_type: str
    category: str
    question_text: str


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


class AuditChannelBody(BaseModel):
    channel_id: str


class LegitLibsTemplateBody(BaseModel):
    title: str
    body: str
    tier: int
    tags: Optional[str] = None
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
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            total_q = conn.execute(
                "SELECT COUNT(*) FROM games_question_bank"
            ).fetchone()[0]

            games_played = conn.execute(
                "SELECT COUNT(*) FROM games_game_history"
            ).fetchone()[0]

            rounds_played_row = conn.execute(
                "SELECT COALESCE(SUM(round_count), 0) FROM games_game_history"
            ).fetchone()
            rounds_played = rounds_played_row[0] if rounds_played_row else 0

            # Unique players: host_ids + player_ids from payload JSON
            host_rows = conn.execute(
                "SELECT DISTINCT host_id FROM games_game_history WHERE host_id IS NOT NULL"
            ).fetchall()
            player_ids: set[str] = {str(r[0]) for r in host_rows}

            payload_rows = conn.execute(
                "SELECT payload FROM games_game_history WHERE payload IS NOT NULL"
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

            # Bank by type: {game_type: {sfw: N, nsfw: N}}
            bank_rows = conn.execute(
                "SELECT game_type, category, COUNT(*) FROM games_question_bank "
                "GROUP BY game_type, category"
            ).fetchall()
            bank_by_type: dict[str, dict[str, int]] = {}
            for gt, cat, cnt in bank_rows:
                if gt not in bank_by_type:
                    bank_by_type[gt] = {"sfw": 0, "nsfw": 0}
                bank_by_type[gt][cat] = cnt

            # Games by type: {game_type: N}
            hist_rows = conn.execute(
                "SELECT game_type, COUNT(*) FROM games_game_history GROUP BY game_type"
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
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
    game_type: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
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
            if category:
                clauses.append("category = ?")
                params.append(category)
            if search:
                clauses.append("question_text LIKE ?")
                params.append(f"%{search}%")

            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            total = conn.execute(
                f"SELECT COUNT(*) FROM games_question_bank {where}", params
            ).fetchone()[0]

            offset = (page - 1) * per_page
            rows = conn.execute(
                f"""SELECT question_id, game_type, category, question_text, added_at
                    FROM games_question_bank {where}
                    ORDER BY question_id DESC
                    LIMIT ? OFFSET ?""",
                [*params, per_page, offset],
            ).fetchall()

            questions = [
                {
                    "question_id": r[0],
                    "game_type": r[1],
                    "category": r[2],
                    "question_text": r[3],
                    "added_at": r[4],
                }
                for r in rows
            ]
            total_pages = max(1, -(-total // per_page))
            return {
                "questions": questions,
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
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    if body.game_type not in VALID_GAME_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid game_type: {body.game_type}")
    if body.category not in ("sfw", "nsfw"):
        raise HTTPException(status_code=400, detail="category must be 'sfw' or 'nsfw'")

    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            cur = conn.execute(
                "INSERT INTO games_question_bank (game_type, category, question_text) VALUES (?, ?, ?)",
                (body.game_type, body.category, body.question_text.strip()),
            )
            conn.commit()
            return {"question_id": cur.lastrowid}

    return await run_query(_q)


@router.put("/bank/{question_id}")
async def update_question(
    request: Request,
    question_id: int,
    body: BankUpdateBody,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    if body.category and body.category not in ("sfw", "nsfw"):
        raise HTTPException(status_code=400, detail="category must be 'sfw' or 'nsfw'")

    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            existing = conn.execute(
                "SELECT question_id FROM games_question_bank WHERE question_id = ?",
                (question_id,),
            ).fetchone()
            if not existing:
                return None

            sets = []
            params: list[object] = []
            if body.question_text is not None:
                sets.append("question_text = ?")
                params.append(body.question_text.strip())
            if body.category is not None:
                sets.append("category = ?")
                params.append(body.category)

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
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
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
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    if body.game_type not in VALID_GAME_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid game_type: {body.game_type}")
    if body.category not in ("sfw", "nsfw"):
        raise HTTPException(status_code=400, detail="category must be 'sfw' or 'nsfw'")

    lines = [line.strip() for line in body.lines if line.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="No non-empty lines provided")

    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            conn.executemany(
                "INSERT INTO games_question_bank (game_type, category, question_text) VALUES (?, ?, ?)",
                [(body.game_type, body.category, line) for line in lines],
            )
            conn.commit()
            return {"added": len(lines)}

    return await run_query(_q)


@router.get("/bank/export")
async def export_bank(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
    game_type: Optional[str] = Query(None),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            if game_type:
                rows = conn.execute(
                    "SELECT game_type, category, question_text FROM games_question_bank "
                    "WHERE game_type = ? ORDER BY question_id",
                    (game_type,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT game_type, category, question_text FROM games_question_bank "
                    "ORDER BY game_type, question_id"
                ).fetchall()
            return [
                {"game_type": r[0], "category": r[1], "question_text": r[2]}
                for r in rows
            ]

    return await run_query(_q)


@router.post("/bank/import")
async def import_bank(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    raw = await request.json()
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="Body must be a JSON array")

    items: list[tuple[str, str, str]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail=f"Item {i} is not an object")
        gt = entry.get("game_type", "")
        cat = entry.get("category", "")
        text = entry.get("question_text", "").strip()
        if gt not in VALID_GAME_TYPES:
            raise HTTPException(status_code=400, detail=f"Item {i}: invalid game_type '{gt}'")
        if cat not in ("sfw", "nsfw"):
            raise HTTPException(status_code=400, detail=f"Item {i}: category must be sfw or nsfw")
        if not text:
            continue
        items.append((gt, cat, text))

    if not items:
        return {"imported": 0}

    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            conn.executemany(
                "INSERT INTO games_question_bank (game_type, category, question_text) VALUES (?, ?, ?)",
                items,
            )
            conn.commit()
            return {"imported": len(items)}

    return await run_query(_q)


# ── Prompts ──────────────────────────────────────────────────────────────────


@router.get("/prompts")
async def get_prompts(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    return _load_prompt_config()


@router.put("/prompts/global")
async def update_global_prompts(
    request: Request,
    body: PromptsGlobalBody,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
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
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
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
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
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
    user_prompt = body.custom_prompt if body.custom_prompt else base_user_prompt

    results = []
    errors = 0
    for _i in range(count):
        text = await generate_text(system_prompt, user_prompt, max_tokens=max_tokens)
        if text:
            results.append(text)
        else:
            errors += 1

    response: dict = {"results": results}
    if errors:
        response["error"] = f"{errors} generation(s) failed; check ANTHROPIC_API_KEY"
    return response


# ── Game history ──────────────────────────────────────────────────────────────


@router.get("/history")
async def get_history(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    game_type: Optional[str] = Query(None),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            clauses = []
            params: list[object] = []
            if game_type:
                clauses.append("game_type = ?")
                params.append(game_type)

            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
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
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
    tier: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            clauses = []
            params: list[object] = []
            if tier is not None:
                clauses.append("tier = ?")
                params.append(tier)
            if status:
                clauses.append("status = ?")
                params.append(status)

            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"""SELECT template_id, title, tier, status, tags,
                           player_min, player_max, use_count,
                           (SELECT COUNT(*) FROM legitlibs_blank_axes WHERE 1=0) AS blanks_count
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
                })
            return {"templates": templates}

    return await run_query(_q)


@router.get("/legitlibs/templates/{template_id}")
async def get_ll_template(
    request: Request,
    template_id: int,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
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


@router.post("/legitlibs/templates")
async def create_ll_template(
    request: Request,
    body: LegitLibsTemplateBody,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            cur = conn.execute(
                """INSERT INTO legitlibs_templates
                   (title, body, tier, tags, status, player_min, player_max, blanks, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    body.title,
                    body.body,
                    body.tier,
                    body.tags,
                    body.status,
                    body.player_min,
                    body.player_max,
                    body.blanks,
                    body.notes,
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
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            existing = conn.execute(
                "SELECT template_id FROM legitlibs_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
            if not existing:
                return None

            sets = []
            params: list[object] = []
            for field, value in body.model_dump(exclude_none=True).items():
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
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            existing = conn.execute(
                "SELECT template_id FROM legitlibs_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
            if not existing:
                return None
            conn.execute(
                "DELETE FROM legitlibs_templates WHERE template_id = ?", (template_id,)
            )
            conn.commit()
            return {}

    result = await run_query(_q)
    if result is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return result


@router.get("/legitlibs/axes")
async def get_ll_axes(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
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
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    import json as _json
    import re as _re
    from bot_modules.games.utils.ai_client import generate_text
    from bot_modules.cogs.games_legitlibs.validation import validate_template

    raw_text = body.raw_text.strip()
    if not raw_text:
        raise HTTPException(400, "raw_text is required")
    if len(raw_text) > 2000:
        raise HTTPException(400, "Text too long (max 2000 characters)")

    tier = max(1, min(4, body.tier))

    noun_domains = ["place", "person"] + (["body"] if tier >= 2 else []) + (["kink"] if tier >= 4 else [])
    verb_domains = ["intimate"] if tier >= 3 else []
    domain_lines = f"noun domains: {', '.join(noun_domains)}"
    if verb_domains:
        domain_lines += f"; verb domains: {', '.join(verb_domains)}"

    system_prompt = f"""You are a Mad Libs template editor. Given a passage of text and a heat tier (1=Flirty, 2=Spicy, 3=Filthy, 4=Unhinged), choose 4–8 words or short phrases that would be fun fill-in-the-blank slots. Replace each chosen word/phrase with a sequential numeric marker {{1}}, {{2}}, … and output a JSON object. Output 4–8 blanks total even if the passage is long.

Rules:
- Choose nouns, verbs, adjectives, adverbs, numbers, or exclamations. Prefer concrete, evocative words.
- Do NOT blank articles (a, an, the), prepositions, pronouns, or conjunctions.
- Keep the surrounding text grammatically intact (replace the entire inflected form).
- Marker IDs must be sequential integers starting at 1 and unique.
- Available pos values: noun, verb, adjective, adverb, exclamation, number, wildcard
- Use wildcard only when the slot is intentionally open-ended (any word goes).
- Available {domain_lines}
- Available verb forms: ing, past, infinitive; noun forms: plural
- Only include domain or form in the blank object when clearly applicable.
- Output ONLY valid JSON — no markdown fences, no commentary.

JSON format:
{{"body": "<text with markers>", "blanks": [{{"id": "1", "pos": "noun"}}, ...]}}"""

    user_prompt = f"Tier: {tier}\n\nText: {raw_text}"

    raw = await generate_text(system_prompt, user_prompt, max_tokens=900, temperature=0.2)
    if not raw:
        raise HTTPException(502, "AI prep failed — check ANTHROPIC_API_KEY and try again")

    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        parsed = _json.loads(cleaned)
    except _json.JSONDecodeError:
        match = _re.search(r"\{.*\}", cleaned, _re.DOTALL)
        if match:
            try:
                parsed = _json.loads(match.group())
            except _json.JSONDecodeError:
                raise HTTPException(502, "AI returned malformed JSON — try again")
        else:
            raise HTTPException(502, "AI returned malformed JSON — try again")

    if not isinstance(parsed.get("body"), str) or not isinstance(parsed.get("blanks"), list):
        raise HTTPException(502, "AI response missing required fields — try again")

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
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
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
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            rows = conn.execute(
                "SELECT channel_id, added_by, added_at FROM games_allowed_channels ORDER BY added_at DESC"
            ).fetchall()
            return {
                "channels": [
                    {"channel_id": r[0], "added_by": r[1], "added_at": r[2]}
                    for r in rows
                ]
            }

    return await run_query(_q)


@router.post("/config/channels")
async def add_allowed_channel(
    request: Request,
    body: ChannelAddBody,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO games_allowed_channels (channel_id) VALUES (?)",
                (body.channel_id,),
            )
            conn.commit()
            return {}

    return await run_query(_q)


@router.delete("/config/channels/{channel_id}")
async def remove_allowed_channel(
    request: Request,
    channel_id: str,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            conn.execute(
                "DELETE FROM games_allowed_channels WHERE channel_id = ?", (channel_id,)
            )
            conn.commit()
            return {}

    return await run_query(_q)


# ── Audit channel config ──────────────────────────────────────────────────────


@router.get("/config/audit")
async def get_audit_channel(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            row = conn.execute(
                "SELECT guild_id, channel_id FROM games_audit_channel LIMIT 1"
            ).fetchone()
            if not row:
                return None
            return {"guild_id": row[0], "channel_id": row[1]}

    return await run_query(_q)


@router.put("/config/audit")
async def set_audit_channel(
    request: Request,
    body: AuditChannelBody,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            existing = conn.execute(
                "SELECT guild_id FROM games_audit_channel LIMIT 1"
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE games_audit_channel SET channel_id = ? WHERE guild_id = ?",
                    (body.channel_id, existing[0]),
                )
            else:
                guild_id = ctx.guild_id if hasattr(ctx, "guild_id") else 0
                conn.execute(
                    "INSERT INTO games_audit_channel (guild_id, channel_id) VALUES (?, ?)",
                    (guild_id, body.channel_id),
                )
            conn.commit()
            return {}

    return await run_query(_q)
