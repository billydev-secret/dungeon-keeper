import json
import random
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

HEAT_LABELS = {1: "🌶️ Flirty", 2: "🌶️🌶️ Spicy", 3: "🌶️🌶️🌶️ Filthy", 4: "💀 Unhinged"}
HEAT_ICONS = {1: "🌶️", 2: "🌶️🌶️", 3: "🌶️🌶️🌶️", 4: "💀"}
RECENT_USE_WINDOW = 5


async def get_axes(db) -> dict:
    """
    Return {
        "pos":     [{"value": str, "min_tier": int}, ...],
        "domains": {pos: [{"value": str, "min_tier": int}, ...]},
        "forms":   {pos: [{"value": str, "min_tier": int}, ...]},
    }
    """
    rows = await db.fetchall("SELECT axis, value, parent_pos, min_tier FROM legitlibs_blank_axes")
    result = {"pos": [], "domains": {}, "forms": {}}
    for r in rows:
        if r["axis"] == "pos":
            result["pos"].append({"value": r["value"], "min_tier": r["min_tier"]})
        elif r["axis"] == "domain":
            result["domains"].setdefault(r["parent_pos"], []).append(
                {"value": r["value"], "min_tier": r["min_tier"]}
            )
        elif r["axis"] == "form":
            result["forms"].setdefault(r["parent_pos"], []).append(
                {"value": r["value"], "min_tier": r["min_tier"]}
            )
    return result


async def get_prompts(db) -> dict:
    """
    Return {(pos, domain, form, tier): {"prompt": str, "examples": [str], "length_cap": int|None}}.
    domain and form are None (not "") for rows where they don't apply.
    """
    rows = await db.fetchall(
        "SELECT pos, domain, form, tier, prompt, examples, length_cap FROM legitlibs_blank_prompts"
    )
    result = {}
    for r in rows:
        key = (r["pos"], r["domain"], r["form"], r["tier"])
        result[key] = {
            "prompt": r["prompt"],
            "examples": json.loads(r["examples"]),
            "length_cap": r["length_cap"],
        }
    return result


def resolve_blank(prompts: dict, pos: str, domain: str | None, form: str | None, tier: int) -> dict | None:
    """Walk the fallback chain to find the most-specific prompt row for this blank.

    Lookup order for (P, D, F) at tier T:
        For t in T, T-1, ..., 1:
            Try (P, D, F), (P, D, None), (P, None, F), (P, None, None)
            Return the first row found.

    length_cap inherits from the next row in the chain with a non-None cap.
    """
    specificity_chain = [(domain, form), (domain, None), (None, form), (None, None)]

    for t in range(tier, 0, -1):
        for (d, f) in specificity_chain:
            row = prompts.get((pos, d, f, t))
            if row is None:
                continue
            if row["length_cap"] is not None:
                return row
            cap = _lookup_cap(prompts, pos, d, f, t)
            return {"prompt": row["prompt"], "examples": row["examples"], "length_cap": cap}
    return None


def _lookup_cap(prompts: dict, pos: str, domain: str | None, form: str | None, tier: int) -> int | None:
    """Walk the fallback chain looking only for a non-None length_cap."""
    specificity_chain = [(domain, form), (domain, None), (None, form), (None, None)]
    for t in range(tier, 0, -1):
        for (d, f) in specificity_chain:
            row = prompts.get((pos, d, f, t))
            if row and row["length_cap"] is not None:
                return row["length_cap"]
    return None


async def get_template_by_id(db, template_id: str) -> dict | None:
    """Fetch a published template by ID (for gameplay use)."""
    row = await db.fetchone(
        "SELECT * FROM legitlibs_templates WHERE template_id = ? AND status = 'published'",
        (template_id,),
    )
    if not row:
        return None
    return _row_to_template(row)


async def get_template_for_preview(db, template_id: str) -> dict | None:
    """Fetch any non-archived template by ID (for mod preview/authoring)."""
    row = await db.fetchone(
        "SELECT * FROM legitlibs_templates WHERE template_id = ? AND status != 'archived'",
        (template_id,),
    )
    if not row:
        return None
    return _row_to_template(row)


async def pick_template(
    db,
    guild_id: int,
    tier: int,
    tag: str | None = None,
    template_id: str | None = None,
) -> dict | None:
    """Select a published template for a round, applying variety controls."""
    if template_id:
        return await get_template_by_id(db, template_id)

    # Fetch recently used templates for this guild
    recent_rows = await db.fetchall(
        "SELECT template_id FROM legitlibs_recent_use WHERE guild_id = ? ORDER BY used_at DESC LIMIT ?",
        (guild_id, RECENT_USE_WINDOW),
    )
    recent_ids = {r["template_id"] for r in recent_rows}

    # Query published templates at this tier or below
    rows = await db.fetchall(
        "SELECT * FROM legitlibs_templates WHERE status = 'published' AND tier <= ?",
        (tier,),
    )
    if not rows:
        return None

    candidates = [_row_to_template(r) for r in rows]

    if tag:
        tag_lower = tag.lower()
        tagged = [t for t in candidates if tag_lower in [tg.lower() for tg in t["tags"]]]
        if tagged:
            candidates = tagged

    # Prefer templates not recently used; fall back to full list if needed
    fresh = [t for t in candidates if t["template_id"] not in recent_ids]
    pool = fresh if fresh else candidates

    return random.choice(pool)


async def mark_template_used(db, guild_id: int, template_id: str):
    """Record this template as recently used for the guild and increment use_count."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO legitlibs_recent_use (guild_id, template_id, used_at)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, template_id) DO UPDATE SET used_at = excluded.used_at
        """,
        (guild_id, template_id, now),
    )
    await db.execute(
        "UPDATE legitlibs_templates SET use_count = use_count + 1, last_used_at = ? WHERE template_id = ?",
        (now, template_id),
    )


async def get_channel_max_tier(db, channel_id: int) -> int:
    row = await db.fetchone(
        "SELECT max_tier FROM legitlibs_channel_config WHERE channel_id = ?", (channel_id,)
    )
    return row["max_tier"] if row else 4


async def seed_templates_from_file(db, path: str, author_id: int):
    """Load templates_seed.json into the DB if no published templates exist yet."""
    row = await db.fetchone(
        "SELECT COUNT(*) AS cnt FROM legitlibs_templates WHERE status = 'published'"
    )
    if row and row["cnt"] > 0:
        return

    try:
        import json as _json
        with open(path, "r", encoding="utf-8") as f:
            templates = _json.load(f)
    except FileNotFoundError:
        log.warning("templates_seed.json not found at %s — skipping seed.", path)
        return
    except Exception as e:
        log.error("Failed to load templates_seed.json: %s", e)
        return

    imported = 0
    for t in templates:
        try:
            await db.execute(
                """
                INSERT OR IGNORE INTO legitlibs_templates
                    (template_id, title, body, tier, tags, status, player_min, player_max,
                     blanks, author_id, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t["template_id"], t["title"], t["body"], t["tier"],
                    json.dumps(t.get("tags", [])), t.get("status", "published"),
                    t.get("player_min", 2), t.get("player_max", 99),
                    json.dumps(t["blanks"]), t.get("author_id", author_id),
                    t.get("notes", ""),
                ),
            )
            imported += 1
        except Exception as e:
            log.error("Failed to seed template %s: %s", t.get("template_id"), e)

    log.info("Seeded %d templates from %s.", imported, path)


def _row_to_template(row) -> dict:
    return {
        "template_id": row["template_id"],
        "title": row["title"],
        "body": row["body"],
        "tier": row["tier"],
        "tags": json.loads(row["tags"]) if row["tags"] else [],
        "status": row["status"],
        "player_min": row["player_min"],
        "player_max": row["player_max"],
        "blanks": json.loads(row["blanks"]) if row["blanks"] else [],
        "author_id": row["author_id"],
        "notes": row["notes"] or "",
        "use_count": row["use_count"],
    }
