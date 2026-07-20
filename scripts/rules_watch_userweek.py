#!/usr/bin/env python3
"""User-week detection experiment for Rules Watch.

The per-message framing in docs/rules_watch_tuning.md §12 is the wrong unit:
rate and breadth only exist as aggregates, and the mod-facing card (§11) is a
user-week artifact. This builds (user, week) features over the WHOLE corpus and
evaluates whether the relational signal separates weeks that drew human
attention from weeks that did not.

Label = "did this user-week warrant a human look?" — a complaint, a ticket, a
mod-chat thread, or an action. Cleared cases (bigprop03, Heli, JayGuerrero) count
as POSITIVE: the system's job is to surface, not to decide. Sources are the
tickets, 💛│golden-girls and 🏢│mod-chat, all dated.

Usage:
  python scripts/rules_watch_userweek.py [--min-msgs 20] [--features a,b,c]
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

DB = Path(__file__).resolve().parent.parent / "dungeonkeeper.db"
GUILD = 1469491362444480666

NSFW = {"🔥│flash-channel", "🫦│spicy-chat", "🤳│selfies",
        "🫦│photo-challenge", "🫦│spicy-games"}

ANATOMY = re.compile(r"\b(tits?|titties|boobs?|boobies|ass|pussy|cock|dick|bush|"
                     r"nipples?|rump|booty|curves|thighs)\b", re.I)
WANT = re.compile(r"\b(i want|i need|let me|can i|may i|wish i|i'd love to|show me|"
                  r"send me|get back here|come back|need any)\b", re.I)
ENDEAR = re.compile(r"\b(babe|baby|hun|honey|darling|gorgeous|sweetheart|cutie|"
                    r"sweetie)\b", re.I)

# (user_id, YYYY-MM-DD inside the concerning week, short provenance)
POSITIVES = [
    (416829953355808808, "2026-07-16", "bagel DM'd Billy; 'ahead of the rapport curve'"),
    (1496432727623335966, "2026-06-17", "ticket #23, Chi-Gal first confrontation"),
    (1496432727623335966, "2026-06-30", "Loaf+lily report; lily ticket #29"),
    (1496432727623335966, "2026-07-16", "Birdie ticket #39 -> ban"),
    (1415760529678536744, "2026-07-07", "mimi 'WOW' incident, self-censor"),
    (1415760529678536744, "2026-07-11", "Reddit naming, ticket #34 -> ban"),
    (718710903159390291, "2026-07-07", "ticket #33, unsolicited DM to bagel"),
    (1417979592765079625, "2026-05-14", "ticket #15 -> ban"),
    (597111823514468352, "2026-07-16", "'small brains big boobs'; promotion withheld"),
    (1420550841991041086, "2026-07-17", "'fantasy list'; promotion withheld"),
    (409107856739008512, "2026-07-07", "ticket #32 unsolicited DM (no action)"),
    (490886726076727296, "2026-07-12", "golden-girls discussion (cleared)"),
    (872080258084511755, "2026-07-16", "golden-girls 'pushy/over-eager' (cleared)"),
    (1524147431397265551, "2026-07-16", "golden-girls cross-server wariness (cleared)"),
    (1431330604154359819, "2026-04-28", "cat-bot exit / mod-chat discomfort"),
    (1498840448939065355, "2026-05-06", "Ivana Dee mod-chat 'keep an eye on Nate'"),
    (1394961716156039278, "2026-07-08", "named by lily for Reddit DM bypass"),
]

FEATURES = ["msgs", "directed", "recipients", "nsfw_share", "new_pair_share",
            "median_recip", "median_days_known", "reply_back_rate",
            "anatomy_rate", "want_rate", "endear_rate", "tenure_days",
            "onesided_targets"]

# Tenure-relative set. Every one of these is DEFINED for a first-week member
# (unlike median_recip / median_days_known, which are 0 for everyone new and so
# collapse into an "is new" detector -- see §12.2b). Each is z-scored within the
# member's tenure bucket, so the question becomes "is this person more intense
# than others at the same point in their membership", not "is this person new".
REL_FEATURES = ["directed_share", "breadth_per_directed", "msgs_per_recipient",
                "reply_back_rate", "onesided_share", "nsfw_share",
                "anatomy_rate", "want_rate", "endear_rate"]


def weekkey(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%W")


def load(con):
    rows = con.execute(
        "SELECT m.message_id, m.author_id, m.content, m.reply_to_id, m.ts, "
        "COALESCE(c.channel_name,'') ch FROM messages m "
        "LEFT JOIN known_channels c ON c.channel_id=m.channel_id AND c.guild_id=m.guild_id "
        "WHERE m.guild_id=? AND m.content IS NOT NULL AND m.content!='' ORDER BY m.ts",
        (GUILD,),
    ).fetchall()
    bots = {r[0] for r in con.execute(
        "SELECT user_id FROM known_users WHERE guild_id=? AND is_bot=1", (GUILD,))}
    return [r for r in rows if r["author_id"] not in bots], bots


def build(min_msgs: int):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    msgs, bots = load(con)
    by_id = {m["message_id"]: m for m in msgs}

    first_seen: dict[int, float] = {}
    for m in msgs:
        first_seen.setdefault(m["author_id"], m["ts"])

    # directed edges, chronological
    edges = []  # (ts, src, dst)
    for m in msgs:
        p = by_id.get(m["reply_to_id"]) if m["reply_to_id"] else None
        if p and p["author_id"] != m["author_id"]:
            edges.append((m["ts"], m["author_id"], p["author_id"]))

    # cumulative pair counts, and pair first-contact, as of a given index
    pair_n: dict[tuple[int, int], int] = defaultdict(int)
    pair_first: dict[tuple[int, int], float] = {}
    edge_i = 0

    weeks = sorted({weekkey(m["ts"]) for m in msgs})
    msgs_by_week: dict[str, list] = defaultdict(list)
    for m in msgs:
        msgs_by_week[weekkey(m["ts"])].append(m)
    edges_by_week: dict[str, list] = defaultdict(list)
    for e in edges:
        edges_by_week[weekkey(e[0])].append(e)

    out = []
    for wk in weeks:
        wmsgs = msgs_by_week[wk]
        wedges = edges_by_week[wk]
        wk_start = min(m["ts"] for m in wmsgs)

        # advance cumulative pair state to the START of this week (no lookahead)
        while edge_i < len(edges) and edges[edge_i][0] < wk_start:
            ts, s, d = edges[edge_i]
            pair_n[(s, d)] += 1
            pair_first.setdefault((s, d), ts)
            pair_first.setdefault((d, s), ts)
            edge_i += 1

        by_author = defaultdict(list)
        for m in wmsgs:
            by_author[m["author_id"]].append(m)
        out_edges = defaultdict(list)
        in_edges = defaultdict(int)
        for ts, s, d in wedges:
            out_edges[s].append((d, ts))
            in_edges[(d, s)] += 1

        for uid, ums in by_author.items():
            if len(ums) < min_msgs:
                continue
            outs = out_edges.get(uid, [])
            targets = defaultdict(int)
            for d, _ in outs:
                targets[d] += 1

            recips, days, onesided, newpair = [], [], 0, 0
            for t, n in targets.items():
                prior_out = pair_n[(uid, t)]
                prior_in = pair_n[(t, uid)]
                recips.append(prior_in / prior_out if prior_out else 0.0)
                f = pair_first.get((uid, t))
                dk = (wk_start - f) / 86400.0 if f else 0.0
                days.append(dk)
                if dk < 14:
                    newpair += n
                back = in_edges.get((uid, t), 0)
                if n >= 5 and back <= 1:
                    onesided += 1

            text = " ".join((m["content"] or "") for m in ums)
            nchars = max(len(text), 1)
            back_total = sum(in_edges.get((uid, t), 0) for t in targets)

            out.append({
                "user": uid, "week": wk,
                "msgs": float(len(ums)),
                "directed": float(len(outs)),
                "recipients": float(len(targets)),
                "nsfw_share": sum(1 for m in ums if m["ch"] in NSFW) / len(ums),
                "new_pair_share": (newpair / len(outs)) if outs else 0.0,
                "median_recip": float(np.median(recips)) if recips else 0.0,
                "median_days_known": float(np.median(days)) if days else 0.0,
                "reply_back_rate": (back_total / len(outs)) if outs else 0.0,
                "anatomy_rate": len(ANATOMY.findall(text)) / nchars * 1000,
                "want_rate": len(WANT.findall(text)) / nchars * 1000,
                "endear_rate": len(ENDEAR.findall(text)) / nchars * 1000,
                "tenure_days": max(0.0, (wk_start - first_seen[uid]) / 86400.0),
                "onesided_targets": float(onesided),
                # tenure-relative ratios (defined even with no pair history)
                "directed_share": (len(outs) / len(ums)) if ums else 0.0,
                "breadth_per_directed": (len(targets) / len(outs)) if outs else 0.0,
                "msgs_per_recipient": (len(outs) / len(targets)) if targets else 0.0,
                "onesided_share": (onesided / len(targets)) if targets else 0.0,
            })
    return out, con


def fit(X, y, l2=2.0, iters=2500, lr=0.3, w_pos=1.0):
    Xb = np.hstack([np.ones((len(X), 1)), X])
    w = np.zeros(Xb.shape[1])
    sw = np.where(y == 1, w_pos, 1.0)
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-Xb @ w))
        g = Xb.T @ (sw * (p - y)) / sw.sum()
        g[1:] += l2 * w[1:] / len(y)
        w -= lr * g
    return w


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-msgs", type=int, default=20)
    ap.add_argument("--features", default="")
    ap.add_argument("--relative", action="store_true",
                    help="tenure-matched z-scored features")
    ap.add_argument("--group-cv", action="store_true",
                    help="hold out whole users, not rows")
    a = ap.parse_args()

    rows, con = build(a.min_msgs)
    names = {r["user_id"]: (r["display_name"] or r["username"]) for r in con.execute(
        "SELECT user_id, display_name, username FROM known_users WHERE guild_id=?", (GUILD,))}

    pos = set()
    for uid, day, _why in POSITIVES:
        ts = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        pos.add((uid, weekkey(ts)))

    y = np.array([1.0 if (r["user"], r["week"]) in pos else 0.0 for r in rows])
    matched = {(r["user"], r["week"]) for r in rows if (r["user"], r["week"]) in pos}
    if a.relative:
        feats = a.features.split(",") if a.features else REL_FEATURES
    else:
        feats = a.features.split(",") if a.features else FEATURES
    X = np.array([[r[f] for f in feats] for r in rows])

    if a.relative:
        # z-score each feature WITHIN the member's tenure bucket (weeks since
        # first message, capped at 8+), so newcomers are compared to newcomers.
        bucket = np.array([min(max(int(r["tenure_days"] // 7), 0), 8) for r in rows])
        Xs = np.zeros_like(X)
        for b in np.unique(bucket):
            m = bucket == b
            if m.sum() < 5:
                Xs[m] = 0.0
                continue
            mu, sd = X[m].mean(0), X[m].std(0)
            sd[sd == 0] = 1.0
            Xs[m] = (X[m] - mu) / sd
        print(f"tenure buckets: {len(np.unique(bucket))} "
              f"(sizes {np.bincount(bucket).tolist()})")
    else:
        mu, sd = X.mean(0), X.std(0)
        sd[sd == 0] = 1.0
        Xs = (X - mu) / sd

    print(f"{len(rows)} user-weeks (>= {a.min_msgs} msgs), "
          f"{int(y.sum())} positive ({y.mean():.1%})")
    miss = [(uid, wk) for (uid, wk) in pos if (uid, wk) not in matched]
    if miss:
        print(f"  {len(miss)} labelled weeks below the activity floor / absent: "
              + ", ".join(f"{names.get(u, u)}@{w}" for u, w in miss))
    print(f"features ({len(feats)}): {', '.join(feats)}\n")

    # Stratified k-fold, repeated, class-weighted for the ~1% base rate.
    # Out-of-fold scores only -- no row is ever scored by a model that saw it.
    w_pos = float((y == 0).sum() / max((y == 1).sum(), 1))
    K, REPEATS = 10, 5
    acc = np.zeros(len(y))
    users = np.array([r["user"] for r in rows])
    for rep in range(REPEATS):
        rng = np.random.default_rng(1234 + rep)
        if a.group_cv:
            # hold out whole USERS: no model ever scores a week belonging to a
            # user it trained on. Positive users are spread across folds first.
            folds = np.empty(len(y), dtype=int)
            pos_u = np.array(sorted({u for u, yy in zip(users, y) if yy == 1}))
            neg_u = np.array(sorted(set(users.tolist()) - set(pos_u.tolist())))
            rng.shuffle(pos_u)
            rng.shuffle(neg_u)
            assign = {}
            for i, u in enumerate(pos_u):
                assign[u] = i % K
            for i, u in enumerate(neg_u):
                assign[u] = i % K
            for i, u in enumerate(users):
                folds[i] = assign[u]
        else:
            folds = np.empty(len(y), dtype=int)
            for cls in (0.0, 1.0):
                idx = np.where(y == cls)[0]
                rng.shuffle(idx)
                folds[idx] = np.arange(len(idx)) % K
        for k in range(K):
            te = folds == k
            tr = ~te
            if y[tr].sum() == 0:
                continue
            w = fit(Xs[tr], y[tr], w_pos=w_pos)
            Xb = np.hstack([np.ones((te.sum(), 1)), Xs[te]])
            acc[te] += 1.0 / (1.0 + np.exp(-(Xb @ w)))
    score = acc / REPEATS

    order = np.argsort(-score)
    print("ranking quality (how far up the list do the known-bad weeks land?)")
    for k in (10, 20, 50, 100):
        hits = int(y[order[:k]].sum())
        print(f"  top {k:3d} of {len(rows)}: {hits:2d}/{int(y.sum())} positives "
              f"({hits/max(y.sum(),1):.0%} recall, {hits/k:.0%} precision)")
    ranks = [int(np.where(order == i)[0][0]) + 1 for i in range(len(y)) if y[i] == 1]
    print(f"\n  median rank of a positive: {int(np.median(ranks))} of {len(rows)}")
    print(f"  best {min(ranks)}, worst {max(ranks)}")

    print("\ntop 15 ranked user-weeks:")
    for i in order[:15]:
        r = rows[i]
        mark = "**" if y[i] == 1 else "  "
        print(f" {mark} {score[i]:.2f} {str(names.get(r['user'], r['user']))[:20]:20s} {r['week']} "
              f"msgs={int(r['msgs']):4d} recips={int(r['recipients']):3d} "
              f"newpair={r['new_pair_share']:.2f} recip={r['median_recip']:.2f} "
              f"days={r['median_days_known']:.0f}")

    w = fit(Xs, y, w_pos=w_pos)
    print("\nstandardised weights:")
    for n, wi in sorted(zip(feats, w[1:]), key=lambda kv: -abs(kv[1])):
        print(f"  {n:20s} {wi:+.3f}")


if __name__ == "__main__":
    main()
