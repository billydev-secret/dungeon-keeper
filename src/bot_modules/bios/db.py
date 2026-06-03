"""CRUD helpers for the bios cog — sync sqlite3, called via asyncio.to_thread.

The wizard, REST routes, and on_member_remove cleanup all funnel
through these helpers so the row/column shape is owned in exactly one
place. Templates are lazily created on first field write; field rows
are soft-retired (active=0) so old `bio_field_values.field_id`
references stay valid.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from bot_modules.bios.logic import BioField, BioQuestion, FieldType


# ── Template + fields ─────────────────────────────────────────────────


@dataclass(frozen=True)
class BioTemplate:
    id: int
    guild_id: int
    version: int


def get_or_create_template(conn: sqlite3.Connection, guild_id: int) -> BioTemplate:
    """Return the guild's template row, creating it on first call."""
    row = conn.execute(
        "SELECT id, guild_id, version FROM bio_templates WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    if row is not None:
        return BioTemplate(id=row["id"], guild_id=row["guild_id"], version=row["version"])
    cur = conn.execute(
        "INSERT INTO bio_templates (guild_id, version, active) VALUES (?, 1, 1)",
        (guild_id,),
    )
    return BioTemplate(id=int(cur.lastrowid or 0), guild_id=guild_id, version=1)


def get_template(conn: sqlite3.Connection, guild_id: int) -> BioTemplate | None:
    """Return the guild's template, or None if it hasn't been created."""
    row = conn.execute(
        "SELECT id, guild_id, version FROM bio_templates WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    if row is None:
        return None
    return BioTemplate(id=row["id"], guild_id=row["guild_id"], version=row["version"])


def bump_template_version(conn: sqlite3.Connection, template_id: int) -> None:
    conn.execute(
        "UPDATE bio_templates SET version = version + 1 WHERE id = ?",
        (template_id,),
    )


def _row_to_field(row: sqlite3.Row) -> BioField:
    try:
        choices = tuple(json.loads(row["choices"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        choices = ()
    return BioField(
        id=row["id"],
        label=row["label"],
        field_type=row["field_type"],
        choices=tuple(str(c) for c in choices),
        required=bool(row["required"]),
        is_headline=bool(row["is_headline"]),
        sort_order=row["sort_order"],
        max_len=row["max_len"],
    )


def list_fields(
    conn: sqlite3.Connection, template_id: int, *, active_only: bool = True
) -> list[BioField]:
    sql = "SELECT id, label, field_type, choices, required, is_headline, sort_order, max_len FROM bio_fields WHERE template_id = ?"
    if active_only:
        sql += " AND active = 1"
    sql += " ORDER BY sort_order, id"
    rows = conn.execute(sql, (template_id,)).fetchall()
    return [_row_to_field(r) for r in rows]


def list_fields_admin(conn: sqlite3.Connection, template_id: int) -> list[dict]:
    """Admin view (includes inactive + the `active` flag and `key`)."""
    rows = conn.execute(
        "SELECT id, key, label, field_type, choices, required, is_headline, sort_order, active, max_len "
        "FROM bio_fields WHERE template_id = ? ORDER BY sort_order, id",
        (template_id,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            choices = json.loads(r["choices"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            choices = []
        out.append(
            {
                "id": r["id"],
                "key": r["key"] or "",
                "label": r["label"],
                "field_type": r["field_type"],
                "choices": [str(c) for c in choices],
                "required": bool(r["required"]),
                "is_headline": bool(r["is_headline"]),
                "sort_order": r["sort_order"],
                "active": bool(r["active"]),
                "max_len": r["max_len"],
            }
        )
    return out


def _next_sort_order(conn: sqlite3.Connection, template_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) AS m FROM bio_fields WHERE template_id = ?",
        (template_id,),
    ).fetchone()
    return int(row["m"]) + 1


def create_field(
    conn: sqlite3.Connection,
    template_id: int,
    *,
    label: str,
    field_type: FieldType,
    choices: list[str],
    required: bool,
    is_headline: bool,
    max_len: int,
    key: str = "",
) -> int:
    sort_order = _next_sort_order(conn, template_id)
    if is_headline:
        conn.execute(
            "UPDATE bio_fields SET is_headline = 0 WHERE template_id = ?",
            (template_id,),
        )
    cur = conn.execute(
        "INSERT INTO bio_fields (template_id, key, label, field_type, choices, required, is_headline, sort_order, active, max_len) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (
            template_id,
            key,
            label,
            field_type,
            json.dumps(list(choices)),
            int(bool(required)),
            int(bool(is_headline)),
            sort_order,
            int(max_len),
        ),
    )
    return int(cur.lastrowid or 0)


def update_field(
    conn: sqlite3.Connection,
    field_id: int,
    *,
    template_id: int,
    label: str | None = None,
    field_type: FieldType | None = None,
    choices: list[str] | None = None,
    required: bool | None = None,
    is_headline: bool | None = None,
    max_len: int | None = None,
    active: bool | None = None,
) -> None:
    sets: list[str] = []
    args: list = []
    if label is not None:
        sets.append("label = ?")
        args.append(label)
    if field_type is not None:
        sets.append("field_type = ?")
        args.append(field_type)
    if choices is not None:
        sets.append("choices = ?")
        args.append(json.dumps(list(choices)))
    if required is not None:
        sets.append("required = ?")
        args.append(int(bool(required)))
    if max_len is not None:
        sets.append("max_len = ?")
        args.append(int(max_len))
    if active is not None:
        sets.append("active = ?")
        args.append(int(bool(active)))
    if is_headline is True:
        conn.execute(
            "UPDATE bio_fields SET is_headline = 0 WHERE template_id = ?",
            (template_id,),
        )
        sets.append("is_headline = 1")
    elif is_headline is False:
        sets.append("is_headline = 0")
    if not sets:
        return
    args.append(field_id)
    conn.execute(
        f"UPDATE bio_fields SET {', '.join(sets)} WHERE id = ?",
        args,
    )


def reorder_fields(
    conn: sqlite3.Connection, template_id: int, ordered_ids: list[int]
) -> None:
    """Reassign sort_order to match the given list of field ids."""
    for sort_order, field_id in enumerate(ordered_ids):
        conn.execute(
            "UPDATE bio_fields SET sort_order = ? WHERE id = ? AND template_id = ?",
            (sort_order, field_id, template_id),
        )


def soft_retire_field(conn: sqlite3.Connection, field_id: int) -> None:
    conn.execute("UPDATE bio_fields SET active = 0 WHERE id = ?", (field_id,))


def has_active_headline(conn: sqlite3.Connection, template_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM bio_fields WHERE template_id = ? AND active = 1 AND is_headline = 1 LIMIT 1",
        (template_id,),
    ).fetchone()
    return row is not None


def has_any_active_field(conn: sqlite3.Connection, template_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM bio_fields WHERE template_id = ? AND active = 1 LIMIT 1",
        (template_id,),
    ).fetchone()
    return row is not None


# ── Question pool ─────────────────────────────────────────────────────


def list_active_questions(
    conn: sqlite3.Connection, guild_id: int
) -> list[BioQuestion]:
    rows = conn.execute(
        "SELECT id, prompt, weight FROM bio_questions WHERE guild_id = ? AND active = 1 ORDER BY id",
        (guild_id,),
    ).fetchall()
    return [
        BioQuestion(id=r["id"], prompt=r["prompt"], weight=int(r["weight"]))
        for r in rows
    ]


def list_questions_admin(conn: sqlite3.Connection, guild_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, prompt, weight, active FROM bio_questions WHERE guild_id = ? ORDER BY id",
        (guild_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "prompt": r["prompt"],
            "weight": int(r["weight"]),
            "active": bool(r["active"]),
        }
        for r in rows
    ]


def create_question(
    conn: sqlite3.Connection, guild_id: int, *, prompt: str, weight: int = 1
) -> int:
    cur = conn.execute(
        "INSERT INTO bio_questions (guild_id, prompt, weight, active) VALUES (?, ?, ?, 1)",
        (guild_id, prompt, max(1, int(weight))),
    )
    return int(cur.lastrowid or 0)


def update_question(
    conn: sqlite3.Connection,
    question_id: int,
    *,
    prompt: str | None = None,
    weight: int | None = None,
    active: bool | None = None,
) -> None:
    sets: list[str] = []
    args: list = []
    if prompt is not None:
        sets.append("prompt = ?")
        args.append(prompt)
    if weight is not None:
        sets.append("weight = ?")
        args.append(max(1, int(weight)))
    if active is not None:
        sets.append("active = ?")
        args.append(int(bool(active)))
    if not sets:
        return
    args.append(question_id)
    conn.execute(f"UPDATE bio_questions SET {', '.join(sets)} WHERE id = ?", args)


def soft_retire_question(conn: sqlite3.Connection, question_id: int) -> None:
    conn.execute("UPDATE bio_questions SET active = 0 WHERE id = ?", (question_id,))


def get_question(conn: sqlite3.Connection, question_id: int) -> BioQuestion | None:
    row = conn.execute(
        "SELECT id, prompt, weight FROM bio_questions WHERE id = ? AND active = 1",
        (question_id,),
    ).fetchone()
    if row is None:
        return None
    return BioQuestion(id=row["id"], prompt=row["prompt"], weight=int(row["weight"]))


# ── Posted bios ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class StoredBio:
    user_id: int
    guild_id: int
    message_id: int
    channel_id: int
    created_at: str
    updated_at: str
    field_values: dict[int, tuple[str, str]]  # field_id → (label, value)
    answers: dict[int, tuple[int, str, str]]  # slot → (question_id, text, answer)


def get_user_bio(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> StoredBio | None:
    """Return the user's stored bio with field values and question answers."""
    bio_row = conn.execute(
        "SELECT user_id, guild_id, message_id, channel_id, created_at, updated_at "
        "FROM bios WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    ).fetchone()
    if bio_row is None:
        return None
    value_rows = conn.execute(
        "SELECT field_id, field_label, value FROM bio_field_values WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    ).fetchall()
    answer_rows = conn.execute(
        "SELECT slot, question_id, question_text, answer FROM bio_answers WHERE user_id = ? AND guild_id = ? ORDER BY slot",
        (user_id, guild_id),
    ).fetchall()
    return StoredBio(
        user_id=bio_row["user_id"],
        guild_id=bio_row["guild_id"],
        message_id=bio_row["message_id"],
        channel_id=bio_row["channel_id"],
        created_at=bio_row["created_at"],
        updated_at=bio_row["updated_at"],
        field_values={r["field_id"]: (r["field_label"], r["value"]) for r in value_rows},
        answers={
            r["slot"]: (r["question_id"], r["question_text"], r["answer"])
            for r in answer_rows
        },
    )


def upsert_bio(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    user_id: int,
    message_id: int,
    channel_id: int,
    field_rows: list[tuple[int, str, str]],  # (field_id, field_label, value)
    answer_rows: list[tuple[int, int, str, str]],  # (slot, question_id, q_text, answer)
) -> None:
    """Atomic: upsert `bios`, replace `bio_field_values` + `bio_answers`."""
    conn.execute(
        """
        INSERT INTO bios (user_id, guild_id, message_id, channel_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id, guild_id) DO UPDATE SET
            message_id = excluded.message_id,
            channel_id = excluded.channel_id,
            updated_at = CURRENT_TIMESTAMP
        """,
        (user_id, guild_id, message_id, channel_id),
    )
    conn.execute(
        "DELETE FROM bio_field_values WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    )
    conn.execute(
        "DELETE FROM bio_answers WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    )
    if field_rows:
        conn.executemany(
            "INSERT INTO bio_field_values (user_id, guild_id, field_id, field_label, value) VALUES (?, ?, ?, ?, ?)",
            [(user_id, guild_id, fid, label, value) for (fid, label, value) in field_rows],
        )
    if answer_rows:
        conn.executemany(
            "INSERT INTO bio_answers (user_id, guild_id, slot, question_id, question_text, answer) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (user_id, guild_id, slot, qid, qtext, ans)
                for (slot, qid, qtext, ans) in answer_rows
            ],
        )


def delete_user_bio(conn: sqlite3.Connection, guild_id: int, user_id: int) -> None:
    """Permanently remove the user's bio row and all their snapshotted
    values/answers. Used by explicit admin deletion, NOT by member-leave
    cleanup (which archives instead, see :func:`archive_user_bio`)."""
    conn.execute(
        "DELETE FROM bio_field_values WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    )
    conn.execute(
        "DELETE FROM bio_answers WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    )
    conn.execute(
        "DELETE FROM bios WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    )


def archive_user_bio(conn: sqlite3.Connection, guild_id: int, user_id: int) -> None:
    """Mark the user's bio as archived (member has left).

    Clears ``message_id`` / ``channel_id`` to 0 as a sentinel — the
    Discord embed has been deleted, but the snapshotted values and
    answers stay so the bio can be resurrected on rejoin.
    """
    conn.execute(
        "UPDATE bios SET message_id = 0, channel_id = 0, "
        "updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    )


def update_bio_message_ref(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    user_id: int,
    message_id: int,
    channel_id: int,
) -> None:
    """Used by the edit-mode 404 → repost path."""
    conn.execute(
        "UPDATE bios SET message_id = ?, channel_id = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE user_id = ? AND guild_id = ?",
        (message_id, channel_id, user_id, guild_id),
    )
