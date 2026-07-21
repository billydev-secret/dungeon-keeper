"""Recompute historical xp_events.amount and member_xp.total_xp/level under
the algorithm currently configured in the database.

All four algorithmic XP sources (text, reply, image_react, voice) are linear
in their per-source coefficient, so this is a per-row rescale rather than a
full replay from `messages` / `reaction_log`.

Per-source rule:
  - text         : amount * (target_word_xp / live_word_xp_at_T)
  - reply        : amount * (target_reply_bonus / live_reply_bonus_at_T)
  - image_react  : amount * (target_image_react / live_image_react_at_T)
  - voice        : amount * (target_award / live_award_at_T)
                          * (live_interval_at_T / target_interval)
  - grant        : unchanged

For text/reply/image_react there is exactly one historical step (Mar 20
2026 14:04:37 PT) where the three coefficients changed atomically. Git
time is precise enough.

For voice, the live `voice_award_xp` cannot be reliably read from git
because two production transitions (the brief A=1.0 window 2026-03-28
14:48 -> 2026-03-29 ~14:17, and the 1.15 override that took effect
~2026-04-13) were dashboard config edits, not commits. We instead infer
A_T per row from the row itself: every voice row was emitted as
`intervals_due * voice_award_xp_at_T`, and `voice_award_xp_at_T` belongs
to a small known set, so divisibility uniquely (or near-uniquely) recovers
it. The single voice_interval_seconds transition (600->60 at 2026-03-28
14:45:03 PT) IS clean in the data and we use git time for it.

Run dry-run first (default), inspect output, then re-run with --commit.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bot_modules.core.xp_system import XpSettings, level_for_xp, load_xp_settings  # noqa: E402

PT = dt.timezone(dt.timedelta(hours=-7))  # PDT during the relevant window
MAR20_CUTOVER = dt.datetime(2026, 3, 20, 14, 4, 37, tzinfo=PT).timestamp()
MAR28_INTERVAL_CUTOVER = dt.datetime(2026, 3, 28, 14, 45, 3, tzinfo=PT).timestamp()

PRE_MAR20_TEXT = 0.25
PRE_MAR20_REPLY = 1.0
PRE_MAR20_IMAGE_REACT = 0.5
PRE_INTERVAL_CUTOVER_VOICE_INTERVAL = 600
POST_INTERVAL_CUTOVER_VOICE_INTERVAL = 60

VOICE_AWARD_CANDIDATES = [20.0, 13.34, 6.67, 1.67, 1.15, 1.0]
EPS = 0.015  # 1.5 cents — accommodates the 0.01 rounding done at write time

# Date prior for tie-breaking the rare amount=20.0 rows that could be
# either 1×20.0 (pre Mar 20) or 20×1.0 (post Mar 28 14:48). We use git
# time for the unambiguous Mar 20 boundary; the 20×1.0 case only matters
# after Mar 28 14:48 ish.
PRE_MAR20_VOICE_AWARD_FAMILY = {20.0}
POST_INTERVAL_VOICE_AWARD_FAMILY = {13.34, 6.67, 1.67, 1.15, 1.0}


def infer_voice_award(amount: float, created_at: float) -> float | None:
    """Recover the per-tick voice_award_xp that was live when this row was emitted.

    Returns None if no candidate divides cleanly.
    """
    if created_at < MAR20_CUTOVER:
        family = PRE_MAR20_VOICE_AWARD_FAMILY
    elif created_at < MAR28_INTERVAL_CUTOVER:
        family = {6.67, 13.34}
    else:
        family = POST_INTERVAL_VOICE_AWARD_FAMILY

    matches: list[tuple[float, int, float]] = []
    for cand in VOICE_AWARD_CANDIDATES:
        if cand not in family:
            continue
        if cand <= 0:
            continue
        n = round(amount / cand)
        if n < 1:
            continue
        residual = abs(amount - n * cand)
        if residual <= EPS:
            matches.append((cand, n, residual))

    if not matches:
        return None

    # Prefer the largest A_T (smallest intervals_due) — most parsimonious.
    matches.sort(key=lambda m: (-m[0], m[2]))
    return matches[0][0]


def transform_amount(
    source: str,
    amount: float,
    created_at: float,
    target: XpSettings,
) -> tuple[float, str | None]:
    """Return (new_amount, warning). new_amount is rounded to 2dp."""
    if amount <= 0:
        return 0.0, None

    if source == "text":
        if created_at < MAR20_CUTOVER:
            new = amount * (target.message_word_xp / PRE_MAR20_TEXT)
        else:
            new = amount
        return round(new, 2), None

    if source == "reply":
        if created_at < MAR20_CUTOVER:
            new = amount * (target.reply_bonus_xp / PRE_MAR20_REPLY)
        else:
            new = amount
        return round(new, 2), None

    if source == "image_react":
        if created_at < MAR20_CUTOVER:
            new = amount * (target.image_reaction_received_xp / PRE_MAR20_IMAGE_REACT)
        else:
            new = amount
        return round(new, 2), None

    if source == "voice":
        a_t = infer_voice_award(amount, created_at)
        i_t = (
            PRE_INTERVAL_CUTOVER_VOICE_INTERVAL
            if created_at < MAR28_INTERVAL_CUTOVER
            else POST_INTERVAL_CUTOVER_VOICE_INTERVAL
        )
        if a_t is None:
            return amount, f"voice row amount={amount} at {dt.datetime.fromtimestamp(created_at)} did not match any candidate; left unchanged"
        new = amount * (target.voice_award_xp / a_t) * (i_t / target.voice_interval_seconds)
        return round(new, 2), None

    if source == "grant":
        return amount, None

    return amount, f"unknown source {source!r}; left unchanged"


def load_target_settings(conn: sqlite3.Connection, guild_id: int) -> XpSettings:
    return load_xp_settings(conn, guild_id=guild_id)


def primary_guild_id(conn: sqlite3.Connection) -> int:
    counts: dict[int, int] = defaultdict(int)
    for (gid,) in conn.execute("SELECT guild_id FROM xp_events NOT INDEXED"):
        counts[gid] += 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def fmt_dt(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def main() -> int:
    doc = __doc__ or ""
    parser = argparse.ArgumentParser(description=doc.split("\n\n")[0])
    parser.add_argument("--db", default="dungeonkeeper.db", help="path to db")
    parser.add_argument("--commit", action="store_true", help="apply changes (default: dry run)")
    parser.add_argument("--guild-id", type=int, default=None, help="override guild for target settings")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"db not found: {db_path}", file=sys.stderr)
        return 2

    if args.commit:
        conn = sqlite3.connect(str(db_path))
    else:
        # Read-only via URI; immutable=1 lets us read past index corruption.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row

    target_guild = args.guild_id or primary_guild_id(conn)
    target = load_target_settings(conn, target_guild)
    print(f"Target guild: {target_guild}")
    print("Target coefficients:")
    print(f"  message_word_xp            = {target.message_word_xp}")
    print(f"  reply_bonus_xp             = {target.reply_bonus_xp}")
    print(f"  image_reaction_received_xp = {target.image_reaction_received_xp}")
    print(f"  voice_award_xp             = {target.voice_award_xp}")
    print(f"  voice_interval_seconds     = {target.voice_interval_seconds}")
    print(f"  level_curve_factor         = {target.level_curve_factor}")
    print()
    print(f"Mar-20 coefficient cutover:    {fmt_dt(MAR20_CUTOVER)} PT")
    print(f"Mar-28 voice-interval cutover: {fmt_dt(MAR28_INTERVAL_CUTOVER)} PT")
    print()

    rows = list(
        conn.execute(
            "SELECT id, guild_id, user_id, source, amount, created_at "
            "FROM xp_events NOT INDEXED"
        )
    )
    print(f"Loaded {len(rows):,} xp_events rows.")

    new_amount_by_id: dict[int, float] = {}
    per_user_old: dict[tuple[int, int], float] = defaultdict(float)
    per_user_new: dict[tuple[int, int], float] = defaultdict(float)
    per_source_old: dict[str, float] = defaultdict(float)
    per_source_new: dict[str, float] = defaultdict(float)
    warnings: list[str] = []
    voice_unmatched_count = 0

    for rid, guild_id, user_id, source, amount, created_at in rows:
        new_amt, warn = transform_amount(source, amount, created_at, target)
        new_amount_by_id[rid] = new_amt
        per_user_old[(guild_id, user_id)] += amount
        per_user_new[(guild_id, user_id)] += new_amt
        per_source_old[source] += amount
        per_source_new[source] += new_amt
        if warn is not None:
            voice_unmatched_count += 1
            if len(warnings) < 10:
                warnings.append(warn)

    print()
    print("=== Per-source totals ===")
    print(f"  {'source':<13} {'old':>14} {'new':>14} {'d':>14} {'d%':>8}")
    for src in ("text", "reply", "image_react", "voice", "grant"):
        if src not in per_source_old:
            continue
        o, n = per_source_old[src], per_source_new[src]
        d = n - o
        pct = (n / o - 1) * 100 if o > 0 else 0.0
        print(f"  {src:<13} {o:>14,.2f} {n:>14,.2f} {d:>+14,.2f} {pct:>+7.1f}%")
    total_o = sum(per_source_old.values())
    total_n = sum(per_source_new.values())
    pct_t = (total_n / total_o - 1) * 100 if total_o > 0 else 0.0
    print(f"  {'TOTAL':<13} {total_o:>14,.2f} {total_n:>14,.2f} {total_n-total_o:>+14,.2f} {pct_t:>+7.1f}%")

    if voice_unmatched_count:
        print()
        print(f"WARNING: {voice_unmatched_count} voice rows did not match any candidate A_T (left unchanged):")
        for w in warnings:
            print(f"  - {w}")

    member_rows = list(
        conn.execute("SELECT guild_id, user_id, total_xp, level FROM member_xp")
    )
    member_rows.sort(key=lambda r: -r[2])

    drops = unchanged = rises = 0
    cross_below_grant: list[tuple[int, int, int, float, float]] = []
    grant_level = target.role_grant_level

    print()
    print(f"=== Top 25 members: before/after (level curve factor = {target.level_curve_factor}) ===")
    print(f"  {'user_id':>20} {'lvl->':>10} {'old_xp':>12} {'new_xp':>12} {'dxp':>11} {'d%':>7}")
    for i, (gid, uid, old_total, old_level) in enumerate(member_rows):
        new_total = round(per_user_new.get((gid, uid), 0.0), 2)
        new_level = level_for_xp(new_total, target)
        if new_level < old_level:
            drops += 1
        elif new_level > old_level:
            rises += 1
        else:
            unchanged += 1
        if old_level >= grant_level and new_level < grant_level:
            cross_below_grant.append((uid, old_level, new_level, old_total, new_total))
        if i < 25:
            arrow = f"{old_level}->{new_level}"
            d = new_total - old_total
            pct = (new_total / old_total - 1) * 100 if old_total > 0 else 0.0
            print(f"  {uid:>20} {arrow:>10} {old_total:>12,.2f} {new_total:>12,.2f} {d:>+11,.2f} {pct:>+6.1f}%")

    print()
    print(f"Level summary: {drops} dropped, {unchanged} unchanged, {rises} rose (n={len(member_rows)})")

    if cross_below_grant:
        print()
        print(f"=== Cross below role_grant_level={grant_level} (these may have a granted role to clean up manually): {len(cross_below_grant)} ===")
        for uid, ol, nl, ot, nt in cross_below_grant[:30]:
            print(f"  user={uid}  level: {ol} -> {nl}  xp: {ot:.2f} -> {nt:.2f}")
        if len(cross_below_grant) > 30:
            print(f"  ... +{len(cross_below_grant)-30} more")

    if not args.commit:
        print()
        print("=== DRY RUN — no changes written. Re-run with --commit to apply. ===")
        return 0

    print()
    print("=== APPLYING CHANGES ===")
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        cur.executemany(
            "UPDATE xp_events SET amount = ? WHERE id = ?",
            [(amt, rid) for rid, amt in new_amount_by_id.items()],
        )
        new_member_rows = []
        for (gid, uid), tot in per_user_new.items():
            tot = round(tot, 2)
            lvl = level_for_xp(tot, target)
            new_member_rows.append((tot, lvl, gid, uid))
        # announced_level rides along with level: a recompute under new
        # coefficients is bookkeeping, not an achievement. Leaving it behind
        # would make everyone whose level rose here get the difference
        # announced into the level-up channel on their next message.
        cur.executemany(
            "UPDATE member_xp SET total_xp = ?, level = ?, announced_level = ? "
            "WHERE guild_id = ? AND user_id = ?",
            [(tot, lvl, lvl, gid, uid) for tot, lvl, gid, uid in new_member_rows],
        )
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise

    print("Committed. Verifying invariant SUM(xp_events.amount) == member_xp.total_xp ...")
    sums: dict[tuple[int, int], float] = defaultdict(float)
    for gid, uid, amt in conn.execute(
        "SELECT guild_id, user_id, amount FROM xp_events NOT INDEXED"
    ):
        sums[(gid, uid)] += amt
    bad = 0
    for gid, uid, total_xp, _lvl in conn.execute(
        "SELECT guild_id, user_id, total_xp, level FROM member_xp"
    ):
        s = round(sums.get((gid, uid), 0.0), 2)
        if abs(total_xp - s) > 0.5:
            bad += 1
            if bad <= 5:
                print(f"  MISMATCH user={uid}: member.total_xp={total_xp} SUM(events)={s}")
    if bad == 0:
        print(f"  OK — all {len(member_rows)} members consistent.")
    else:
        print(f"  {bad} members inconsistent.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
