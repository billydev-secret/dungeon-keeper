#!/usr/bin/env python3
"""Feature-based classifier for Rules Watch, evaluated against the LLM baseline.

Extracts relational + lexical features for each case in the eval set (strictly
from data BEFORE the case timestamp -- no lookahead), then reports:

  1. a hand-set scoring rule, no training at all
  2. leave-one-out cross-validated logistic regression

Both are scored with balanced accuracy so they are directly comparable to the
LLM sweep in docs/rules_watch_tuning.md §12 (3B 0.52, 8B 0.58, Nemo-12B 0.61-0.66).

Usage:  python scripts/rules_watch_features.py [--features N] [--show-weights]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from pathlib import Path

import numpy as np

DB = Path(__file__).resolve().parent.parent / "dungeonkeeper.db"
EVAL = Path(__file__).resolve().parent.parent / "tests/data/rules_watch_eval.jsonl"
GUILD = 1469491362444480666

NSFW_CHANNELS = {
    "🔥│flash-channel", "🫦│spicy-chat", "🤳│selfies",
    "🫦│photo-challenge", "🫦│spicy-games",
}

# Lexicons. Deliberately small and readable -- these are the categories Dona's
# guide names as red-light (specific anatomy / demands a reaction / assumes
# sexual access), not a general profanity list.
ANATOMY = re.compile(
    r"\b(tits?|titties|boobs?|boobies|ass|pussy|cock|dick|bush|nipples?|"
    r"rump|booty|curves|thighs)\b", re.I)
WANT = re.compile(
    r"\b(i want|i need|let me|can i|may i|wish i|i'd love to|show me|send me|"
    r"get back here|come back|need any|gonna)\b", re.I)
ENDEARMENT = re.compile(
    r"\b(babe|baby|hun|honey|darling|gorgeous|sweetheart|cutie|sweetie)\b", re.I)

FEATURE_NAMES = [
    "recip",        # target->author / author->target, prior only
    "log_exch",     # log1p(prior exchanges between the pair)
    "days_known",   # days since first interaction between the pair
    "rate_24h",     # author's messages in prior 24h
    "breadth_24h",  # distinct reply-targets in prior 24h
    "nsfw_chan",    # channel permits explicit content
    "anatomy",      # anatomical term count
    "want",         # first-person want-verb count
    "endearment",   # endearment count
    "log_len",      # log1p(message length)
]

# Ranked by expected usefulness, for the --features N ablation.
FEATURE_ORDER = ["recip", "days_known", "anatomy", "want", "breadth_24h",
                 "log_exch", "endearment", "rate_24h", "nsfw_chan", "log_len"]


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def resolve_target(con, channel_id, author_id, ts, message_id):
    row = con.execute(
        "SELECT reply_to_id FROM messages WHERE message_id=?", (message_id,)
    ).fetchone()
    if row and row["reply_to_id"]:
        t = con.execute(
            "SELECT author_id FROM messages WHERE message_id=?", (row["reply_to_id"],)
        ).fetchone()
        if t:
            return t["author_id"]
    t = con.execute(
        "SELECT author_id FROM messages WHERE guild_id=? AND channel_id=? AND ts<? "
        "AND author_id!=? AND content IS NOT NULL AND content!='' "
        "ORDER BY ts DESC LIMIT 1",
        (GUILD, channel_id, ts, author_id),
    ).fetchone()
    return t["author_id"] if t else None


def features(con, case) -> dict[str, float]:
    row = con.execute(
        "SELECT channel_id, ts, author_id, content FROM messages WHERE message_id=?",
        (case["message_id"],),
    ).fetchone()
    ch, ts, author, content = (
        row["channel_id"], row["ts"], row["author_id"], row["content"] or ""
    )
    target = resolve_target(con, ch, author, ts, case["message_id"])

    n_out = n_in = 0
    first_ts = None
    if target is not None:
        o = con.execute(
            "SELECT COUNT(*) c, MIN(m.ts) f FROM messages m "
            "JOIN messages p ON p.message_id=m.reply_to_id "
            "WHERE m.guild_id=? AND m.author_id=? AND p.author_id=? AND m.ts<?",
            (GUILD, author, target, ts),
        ).fetchone()
        i = con.execute(
            "SELECT COUNT(*) c, MIN(m.ts) f FROM messages m "
            "JOIN messages p ON p.message_id=m.reply_to_id "
            "WHERE m.guild_id=? AND m.author_id=? AND p.author_id=? AND m.ts<?",
            (GUILD, target, author, ts),
        ).fetchone()
        n_out, n_in = o["c"] or 0, i["c"] or 0
        cands = [x for x in (o["f"], i["f"]) if x]
        first_ts = min(cands) if cands else None

    day = 86400
    act = con.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT p.author_id) r FROM messages m "
        "LEFT JOIN messages p ON p.message_id=m.reply_to_id "
        "WHERE m.guild_id=? AND m.author_id=? AND m.ts BETWEEN ? AND ?",
        (GUILD, author, ts - day, ts),
    ).fetchone()

    return {
        "recip": (n_in / n_out) if n_out else 0.0,
        "log_exch": math.log1p(n_out + n_in),
        "days_known": ((ts - first_ts) / day) if first_ts else 0.0,
        "rate_24h": float(act["n"] or 0),
        "breadth_24h": float(act["r"] or 0),
        "nsfw_chan": 1.0 if case["channel"] in NSFW_CHANNELS else 0.0,
        "anatomy": float(len(ANATOMY.findall(content))),
        "want": float(len(WANT.findall(content))),
        "endearment": float(len(ENDEARMENT.findall(content))),
        "log_len": math.log1p(len(content)),
    }


# ── logistic regression (numpy, L2, plain gradient descent) ────────────

def fit(X, y, l2=1.0, iters=4000, lr=0.1):
    Xb = np.hstack([np.ones((len(X), 1)), X])
    w = np.zeros(Xb.shape[1])
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-Xb @ w))
        grad = Xb.T @ (p - y) / len(y)
        grad[1:] += l2 * w[1:] / len(y)
        w -= lr * grad
    return w


def predict(w, X):
    Xb = np.hstack([np.ones((len(X), 1)), X])
    return 1.0 / (1.0 + np.exp(-Xb @ w))


def balanced_accuracy(y, pred):
    tp = int(((pred == 1) & (y == 1)).sum())
    tn = int(((pred == 0) & (y == 0)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    tpr = tp / (tp + fn) if tp + fn else 0.0
    tnr = tn / (tn + fp) if tn + fp else 0.0
    return (tpr + tnr) / 2, tpr, tnr, (tp, fp, tn, fn)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", type=int, default=0,
                    help="use only the top N features (0 = all)")
    ap.add_argument("--show-weights", action="store_true")
    a = ap.parse_args()

    con = connect()
    cases = [json.loads(line) for line in open(EVAL, encoding="utf-8")]
    feats = [features(con, c) for c in cases]
    y = np.array([1.0 if c["label"] == "violation" else 0.0 for c in cases])

    names = FEATURE_ORDER[: a.features] if a.features else FEATURE_NAMES
    X = np.array([[f[n] for n in names] for f in feats])
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1.0
    Xs = (X - mu) / sd

    print(f"{len(cases)} cases ({int(y.sum())} violation / {int(len(y)-y.sum())} ok)")
    print(f"features ({len(names)}): {', '.join(names)}\n")

    # 1. hand-set rule, no training
    rule = np.array([
        1.0 if (f["recip"] < 0.55 and f["days_known"] < 21
                and (f["anatomy"] + f["want"] + f["endearment"]) >= 1)
        else 0.0
        for f in feats
    ])
    ba, tpr, tnr, cm = balanced_accuracy(y, rule)
    print(f"hand-set rule          BalAcc={ba:.2f}  TPR={tpr:.2f} TNR={tnr:.2f}  "
          f"TP={cm[0]} FP={cm[1]} TN={cm[2]} FN={cm[3]}")

    # 2. leave-one-out CV logistic regression
    loo = np.zeros(len(y))
    for i in range(len(y)):
        m = np.ones(len(y), dtype=bool)
        m[i] = False
        w = fit(Xs[m], y[m])
        loo[i] = predict(w, Xs[i:i + 1])[0]
    pred = (loo >= 0.5).astype(float)
    ba, tpr, tnr, cm = balanced_accuracy(y, pred)
    print(f"logreg (leave-one-out) BalAcc={ba:.2f}  TPR={tpr:.2f} TNR={tnr:.2f}  "
          f"TP={cm[0]} FP={cm[1]} TN={cm[2]} FN={cm[3]}")

    print("\nLLM reference (same 57 cases): 3B 0.52 | 8B 0.58 | "
          "Nemo-12B IQ4_XS 0.61 | Nemo-12B Q4_K_M 0.66   (noise ±0.02)")

    if a.show_weights:
        w = fit(Xs, y)
        print("\nfull-fit standardised weights (sign = direction of 'violation'):")
        for n, wi in sorted(zip(names, w[1:]), key=lambda kv: -abs(kv[1])):
            print(f"  {n:12s} {wi:+.3f}")


if __name__ == "__main__":
    main()
