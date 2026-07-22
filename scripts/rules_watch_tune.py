#!/usr/bin/env python3
"""Rules Watch prompt/context tuning harness.

Replays the labeled eval set through candidate (prompt x context) variants
against the remote GPU llama-server and scores precision/recall.

Window construction mirrors monitor.py: last _WINDOW_SIZE messages in the
channel, oldest first, "[HH:MM] name [reply] : text". Rapport context is
computed strictly from messages BEFORE the case timestamp (no lookahead).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone

import httpx

DB = "/home/ben/discord-bots/dungeon-keeper/dungeonkeeper.db"
EVAL = "/home/ben/discord-bots/dungeon-keeper/tests/data/rules_watch_eval.jsonl"
URL = "http://192.168.174.133:8080"
GUILD = 1469491362444480666
WINDOW = 8

NSFW_CHANNELS = {
    "🔥│flash-channel", "🫦│spicy-chat", "🤳│selfies",
    "🫦│photo-challenge", "🫦│spicy-games",
}

# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

RULES = """\
Server rules (check all messages against these):
  Rule 1 — Adults only (21+). NSFW material is permitted.
  Rule 2 — Be good to others: Harassment, coercion, threats, demeaning behavior, and
    discriminatory language including slurs are not allowed. Boundaries must be respected
    immediately. The space is built on consent, respect, and accountability.
  Rule 3 — Keep things in the right channels: explicit content only in designated areas.
  Rule 4 — Keep the focus on this server; no cross-server drama.
  Rule 5 — DMs are opt-in. Use the DM permissions bot and wait for consent. This extends
    to contacting members on other platforms (Reddit, etc.) without permission.
  Rule 6 — Settle disputes in tickets, not public chat."""

# A: the live prompt, verbatim in structure (the baseline we are trying to beat)
P_BASELINE = f"""\
You are a recall-leaning moderation guard for an adult Discord community. Your job is to surface
messages that may warrant human review — you flag generously and let moderators dismiss false
positives. False negatives (missing a real problem) are much worse than false positives here.

{RULES}

You will receive a conversation window (multiple recent messages in a channel), oldest first.

Evaluate the MOST RECENT message (the last one in the window) in the context of the whole window.
Flag if:
- The message contains a slur or identity attack (always a violation regardless of consent)
- The message applies pressure, coercion, or threats
- The message continues unwanted contact with someone who has expressed disinterest
- There is escalating one-sided directed behavior in the window
- The message violates channel designation (Rule 3)

Respond with ONLY valid JSON, no markdown fences, in this exact format:
{{"verdict": "flag", "rule": "2", "reason": "brief reason", "confidence": 0.85}}
or
{{"verdict": "ok", "rule": null, "reason": null, "confidence": 0.1}}

"confidence" is your certainty that this is a genuine concern (0.0–1.0).
"rule" is the primary rule implicated (as a string: "1", "2", "3", "4", "5", "6", or null).
No other output."""

# B: de-anchored — removes "flag generously", demotes slur, drops confidence exemplars
P_NEUTRAL = f"""\
You are a moderation guard for an adult (21+) Discord community where explicit sexual content
and sexual compliments are EXPLICITLY ALLOWED and normal in designated channels. Being explicit
is NOT a violation. Profanity and consensual kink vocabulary are NOT slurs.

{RULES}

You will receive a conversation window (multiple recent messages in a channel), oldest first.
Evaluate ONLY the MOST RECENT message, in the context of the window.

Flag ONLY if one of these is true:
- Pressure, coercion, or a demand after someone declined or pulled back
- Continued sexual attention toward someone who is not reciprocating
- A hard slur (racial, homophobic, transphobic, ableist). NOT profanity, NOT kink terms.
- Naming someone's off-platform account or content that they did not bring up here
- Pushing to move to DMs without consent, or after a refusal

Do NOT flag: explicit talk in an explicit channel, mutual flirting, compliments that the
recipient welcomes, jokes, or profanity.

Respond with ONLY valid JSON, no markdown fences:
{{"verdict": "flag"|"ok", "rule": "<number or null>", "reason": "<brief or null>", "confidence": <0.0-1.0>}}
No other output."""

# C: Dona's community rubric (green/yellow/red), verbatim concepts
P_RUBRIC = f"""\
You are a moderation guard for an adult (21+) Discord community. Explicit sexual content and
sexual compliments are ALLOWED and normal in designated channels. Explicitness is NEVER by
itself a violation. Profanity and consensual kink vocabulary are NOT slurs.

This community's own etiquette guide defines the line:

RAPPORT BEFORE FLIRTING. The progression is: casual chat -> light flirting -> explicit,
and explicit only after mutual interest is clear. Jumping straight to explicit or aggressive
flirting with someone you don't know is the core problem.

GREEN-LIGHT COMPLIMENTS (fine): focus on style, energy, confidence, general aesthetic.
  "You look absolutely incredible in that outfit!"  "Love your vibe!"

RED-LIGHT COMPLIMENTS (concerning): focus heavily on specific anatomy, demand a reaction,
or assume immediate sexual access.
  "I want to see what's under that outfit, slide into my DMs."
  "You have the perfect body for [act], let me show you."
  "Damn, your [body part] is driving me crazy, send more."

READING THE RECIPIENT:
  GREEN — fast replies, matching the flirtatious tone, asking questions back.
  YELLOW — short/one-word answers, replying to the casual parts while ignoring the flirty
    parts, or taking a long time to reply while active elsewhere. Means: back off.
  RED — saying stop, ignoring entirely, cold/dry responses. Means: stop immediately.

ALSO A VIOLATION: naming someone's off-platform account or content unprompted; pushing to
DMs without consent; pressing after any refusal; hard slurs (racial/homophobic/transphobic).

{RULES}

You will receive a conversation window, oldest first. Evaluate ONLY the MOST RECENT message
in the context of the window. Flag it if it is red-light behavior, or continues after a
yellow/red signal from the recipient.

Respond with ONLY valid JSON, no markdown fences:
{{"verdict": "flag"|"ok", "rule": "<number or null>", "reason": "<brief or null>", "confidence": <0.0-1.0>}}
No other output."""

PROMPTS = {"baseline": P_BASELINE, "neutral": P_NEUTRAL, "rubric": P_RUBRIC}

# D: rubric + few-shot (examples are real messages, none in the eval set)
P_FEWSHOT = P_RUBRIC.replace(
    "Respond with ONLY valid JSON, no markdown fences:",
    """Worked examples from this server (these are NOT in your input; learn the distinction):

MSG: "Thanks for the lovely view!"  -> {"verdict":"ok"}
   Appreciation of something offered. No demand, no anatomy, no claim on the person.

MSG: "Hey Chi! Lovely smile"  -> {"verdict":"ok"}
   Green-light: style/energy, not anatomy.

MSG: "Woahhhh that is a gorgeous rump"  -> {"verdict":"ok"}
   Explicit-adjacent, but in a channel for it, to someone who posted it and welcomes it.

MSG: "Your hair, your lips, your skin, your perfect fucking titties... I'm blown away"
   -> {"verdict":"flag","rule":"2"}
   Red-light: anatomical itemization directed at someone with little rapport.

MSG: "Fuuuck, I cannot wait babe"  -> {"verdict":"flag","rule":"2"}
   Endearment + anticipation toward someone known ~2 days. Assumes access.

MSG: "Sooo, ummm... need any snuggles?"  -> {"verdict":"flag","rule":"2"}
   Proposition to a near-stranger; converts her post into an opening for himself.

Respond with ONLY valid JSON, no markdown fences:""")

PROMPTS["fewshot"] = P_FEWSHOT


# E: the ORIGINAL prompt (which Nemo scored best on) with two surgical fixes --
#    demote the slur anchor, and state that explicitness is permitted here.
P_BASELINE_PLUS = P_BASELINE.replace(
    "You are a recall-leaning moderation guard for an adult Discord community.",
    "You are a recall-leaning moderation guard for an adult (21+) Discord community\n"
    "where explicit sexual content and sexual compliments are ALLOWED and NORMAL in\n"
    "designated channels. Explicitness is NEVER by itself a violation, and profanity\n"
    "and consensual kink vocabulary are NOT slurs.",
).replace(
    "- The message contains a slur or identity attack (always a violation regardless of consent)\n"
    "- The message applies pressure, coercion, or threats",
    "- The message applies pressure, coercion, or threats\n"
    "- The message contains a HARD slur (racial, homophobic, transphobic, ableist).\n"
    "  Profanity, crude sexual language and kink terms are NOT slurs.",
)

# F: Dona's canonical taxonomy, verbatim concepts, full traffic-light framing.
P_DONA = f"""\
You are a moderation guard for an adult (21+) Discord community. Explicit sexual
content and sexual compliments are ALLOWED and NORMAL in designated channels.
Explicitness is NEVER by itself a violation. Profanity and consensual kink
vocabulary are NOT slurs. You are looking for PRESSURE and CONSENT problems.

This community's own etiquette guide is the standard:

RAPPORT BEFORE FLIRTING. Progression: casual chat -> light flirting -> explicit,
and explicit only after mutual interest is crystal clear. Jumping straight to
explicit or aggressive flirting with someone you don't know is the core problem.

GREEN-LIGHT COMPLIMENTS (fine): style, energy, confidence, general aesthetic.
  "You look absolutely incredible in that outfit! The styling is perfect."
  "Your confidence in these photos is amazing. Love your vibe!"

RED-LIGHT COMPLIMENTS (concerning): focus heavily on specific anatomy, demand a
reaction, or assume immediate sexual access.
  "I want to see what's under that outfit, slide into my DMs."
  "You have the perfect body for [explicit act], let me show you."
  "Damn, your [specific body part] is driving me crazy, send more."

READING THE RECIPIENT:
  GREEN  - fast replies, matching the flirtatious tone, asking questions back.
  YELLOW - short/one-word answers, replying to the casual parts while ignoring
           the flirty parts, or slow replies while active elsewhere. Back off.
  RED    - saying stop, ignoring entirely, cold/dry responses. Stop immediately.

ALSO VIOLATIONS: naming someone's off-platform account or content unprompted;
asking to move to DMs without consent or after a refusal; pressing after any
refusal; hard slurs.

ACCEPT 'NO' GRACEFULLY: pushing for explanations, getting defensive, or whining
after being told no is itself a violation.

{RULES}

You will receive a conversation window, oldest first. Evaluate ONLY the MOST
RECENT message in the context of the window. Flag it if it is red-light
behaviour, or if it continues after a yellow or red signal from the recipient.

Respond with ONLY valid JSON, no markdown fences:
{{"verdict": "flag"|"ok", "rule": "<number or null>", "reason": "<brief or null>", "confidence": <0.0-1.0>}}
No other output."""

# G: traffic-light output rather than binary. green -> ok; yellow/red -> flag.
P_TRAFFIC = P_DONA.replace(
    '''Respond with ONLY valid JSON, no markdown fences:
{"verdict": "flag"|"ok", "rule": "<number or null>", "reason": "<brief or null>", "confidence": <0.0-1.0>}
No other output.''',
    '''Rate the most recent message:
  "green"  - welcome here; rapport supports it, or the recipient is reciprocating
  "yellow" - coming in ahead of the rapport curve; worth a gentle nudge
  "red"    - pressure, coercion, or continuing after a stop signal

Respond with ONLY valid JSON, no markdown fences:
{"verdict": "green"|"yellow"|"red", "rule": "<number or null>", "reason": "<brief>", "confidence": <0.0-1.0>}
No other output.''')

# H: short reasoning before the verdict -- small models often gain from this.
P_COT = P_DONA.replace(
    '''Respond with ONLY valid JSON, no markdown fences:
{"verdict": "flag"|"ok", "rule": "<number or null>", "reason": "<brief or null>", "confidence": <0.0-1.0>}
No other output.''',
    '''Think briefly, then decide. Respond with ONLY valid JSON, no markdown fences:
{"rapport": "<what the window shows about their relationship, one clause>",
 "recipient": "green"|"yellow"|"red"|"unknown",
 "verdict": "flag"|"ok", "rule": "<number or null>", "reason": "<brief or null>",
 "confidence": <0.0-1.0>}
No other output.''')

PROMPTS["baseline_plus"] = P_BASELINE_PLUS
PROMPTS["dona"] = P_DONA
PROMPTS["traffic"] = P_TRAFFIC
PROMPTS["cot"] = P_COT


# I: Rule 2 only. Short by design -- elaborate rubrics made the model go quiet.
#    Names the three mechanisms every model missed: attention-pressure,
#    conditional worth, and third-party insertion.
P_RULE2 = """\
You are a moderation guard for an adult (21+) Discord community. Explicit sexual
content and sexual compliments are ALLOWED and NORMAL in designated channels.
Explicitness is NEVER a violation. Profanity and kink vocabulary are NOT slurs.

You are checking exactly ONE rule:

Rule 2 - Be good to others. Harassment, coercion, threats and demeaning
behaviour are not allowed. Boundaries must be respected immediately.

Flag the most recent message if it does any of these:
- Presses on after the other person declined, deflected, joked it off, went
  quiet, or pulled back
- Makes someone responsible for the author's feelings: guilt over not replying,
  sulking to get reassurance, "have I made you mad", "I guess nobody cares"
- Grades or rates a person against the author's stated preferences
- Directs sexual attention at someone who is not returning it
- Drops a sexual comment into a conversation between two other people
- Demands a reaction, or assumes sexual access to someone

Otherwise it is ok. Sexual talk between people who are both enjoying it is ok,
however explicit.

Respond with ONLY valid JSON, no markdown fences:
{"verdict": "flag"|"ok", "rule": "2"|null, "reason": "<brief or null>", "confidence": <0.0-1.0>}
No other output."""

PROMPTS["rule2"] = P_RULE2



# --------------------------------------------------------------------------
# Context building
# --------------------------------------------------------------------------

def connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def names(con) -> dict[int, str]:
    return {
        r["user_id"]: (r["display_name"] or r["username"] or f"User {r['user_id']}")
        for r in con.execute(
            "SELECT user_id, display_name, username FROM known_users WHERE guild_id=?",
            (GUILD,),
        )
    }


def build_window(con, nm, channel_id: int, ts: int, message_id: int) -> str:
    rows = con.execute(
        "SELECT message_id, author_id, content, reply_to_id, ts FROM messages "
        "WHERE guild_id=? AND channel_id=? AND ts <= ? AND content IS NOT NULL AND content != '' "
        "ORDER BY ts DESC, message_id DESC LIMIT ?",
        (GUILD, channel_id, ts, WINDOW),
    ).fetchall()
    rows = list(reversed(rows))
    # guarantee the case message is last
    rows = [r for r in rows if r["message_id"] != message_id]
    case = con.execute(
        "SELECT message_id, author_id, content, reply_to_id, ts FROM messages WHERE message_id=?",
        (message_id,),
    ).fetchone()
    rows.append(case)
    rows = rows[-WINDOW:]

    id_to_author = {r["message_id"]: r["author_id"] for r in rows}
    lines = []
    for r in rows:
        t = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%H:%M")
        who = nm.get(r["author_id"], f"User {r['author_id']}")
        text = (r["content"] or "").replace("\n", " ")[:400]
        note = ""
        if r["reply_to_id"] and r["reply_to_id"] in id_to_author:
            note = f" [↩ replying to {nm.get(id_to_author[r['reply_to_id']], '?')}]"
        lines.append(f"[{t}] {who}{note}: {text}")
    return "\n".join(lines)


def pair_context(con, nm, author_id: int, channel_id: int, ts: int, message_id: int) -> str:
    """Rapport facts computed ONLY from messages strictly before `ts`."""
    row = con.execute(
        "SELECT reply_to_id FROM messages WHERE message_id=?", (message_id,)
    ).fetchone()
    target = None
    if row and row["reply_to_id"]:
        t = con.execute(
            "SELECT author_id FROM messages WHERE message_id=?", (row["reply_to_id"],)
        ).fetchone()
        if t:
            target = t["author_id"]
    if target is None:
        # most recent other speaker in channel before ts
        t = con.execute(
            "SELECT author_id FROM messages WHERE guild_id=? AND channel_id=? AND ts < ? "
            "AND author_id != ? AND content IS NOT NULL AND content != '' "
            "ORDER BY ts DESC LIMIT 1",
            (GUILD, channel_id, ts, author_id),
        ).fetchone()
        target = t["author_id"] if t else None
    if target is None:
        return "No specific recipient identified."

    out = con.execute(
        "SELECT count(*) c, min(m.ts) f FROM messages m JOIN messages p ON p.message_id=m.reply_to_id "
        "WHERE m.guild_id=? AND m.author_id=? AND p.author_id=? AND m.ts < ?",
        (GUILD, author_id, target, ts),
    ).fetchone()
    inb = con.execute(
        "SELECT count(*) c, min(m.ts) f FROM messages m JOIN messages p ON p.message_id=m.reply_to_id "
        "WHERE m.guild_id=? AND m.author_id=? AND p.author_id=? AND m.ts < ?",
        (GUILD, target, author_id, ts),
    ).fetchone()
    n_out, n_in = out["c"] or 0, inb["c"] or 0
    firsts = [x for x in (out["f"], inb["f"]) if x]
    days = (ts - min(firsts)) / 86400.0 if firsts else 0.0
    recip = (n_in / n_out) if n_out else 0.0

    tname = nm.get(target, "the recipient")
    if n_out == 0 and n_in == 0:
        rel = f"{tname} and the author have NEVER exchanged a reply before."
    else:
        rel = (
            f"Author has sent {n_out} replies to {tname}; {tname} has sent {n_in} back "
            f"(reciprocity {recip:.2f}). They first interacted {days:.1f} days ago."
        )
    return rel


def daily_context(con, author_id: int, ts: int) -> str:
    day = 86400
    r = con.execute(
        "SELECT count(*) n, count(DISTINCT p.author_id) recips FROM messages m "
        "LEFT JOIN messages p ON p.message_id=m.reply_to_id "
        "WHERE m.guild_id=? AND m.author_id=? AND m.ts BETWEEN ? AND ?",
        (GUILD, author_id, ts - day, ts),
    ).fetchone()
    return f"In the past 24h this author sent {r['n']} messages to {r['recips'] or 0} distinct recipients."


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

JSON_RE = re.compile(r"\{.*?\}", re.S)


async def call(client: httpx.AsyncClient, system: str, user: str) -> dict:
    try:
        r = await client.post(
            f"{URL}/v1/chat/completions",
            json={
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": 256,
                "temperature": 0.0,
            },
            timeout=120,
        )
        txt = r.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        return {"verdict": "error", "err": str(exc)[:80]}
    txt = txt.strip()
    m = JSON_RE.search(txt)
    if not m:
        return {"verdict": "unparsed", "raw": txt[:120]}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"verdict": "unparsed", "raw": txt[:120]}


async def run_variant(cases, pname, ctx_mode, conc=12):
    system = PROMPTS[pname]
    sem = asyncio.Semaphore(conc)
    results = []

    async with httpx.AsyncClient() as client:
        async def one(c):
            async with sem:
                user = c["window"]
                if ctx_mode in ("pair", "full"):
                    user = f"{user}\n\n[RELATIONSHIP CONTEXT] {c['pair']}"
                if ctx_mode == "full":
                    user += f"\n[ACTIVITY] {c['daily']}"
                nsfw = " [NSFW channel — explicit content is permitted here]" if c["channel"] in NSFW_CHANNELS else ""
                user = f"Channel: {c['channel']}{nsfw}\n\n{user}\n\nEvaluate the MOST RECENT message."
                out = await call(client, system, user)
                return c, out

        for fut in asyncio.as_completed([one(c) for c in cases]):
            c, out = await fut
            v = str(out.get("verdict", "")).lower()
            if v in ("green", "yellow", "red"):
                out["light"] = v
                out["verdict"] = "ok" if v == "green" else "flag"
            results.append((c, out))
    return results


def score(results):
    tp = fp = tn = fn = bad = 0
    misses, falses = [], []
    for c, o in results:
        v = o.get("verdict")
        if v in ("error", "unparsed"):
            bad += 1
            continue
        flagged = v == "flag"
        truth = c["label"] == "violation"
        if flagged and truth:
            tp += 1
        elif flagged and not truth:
            fp += 1
            falses.append(c)
        elif not flagged and truth:
            fn += 1
            misses.append(c)
        else:
            tn += 1
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    tpr = tp / (tp + fn) if tp + fn else 0.0
    tnr = tn / (tn + fp) if tn + fp else 0.0
    ba = (tpr + tnr) / 2
    return dict(tp=tp, fp=fp, tn=tn, fn=fn, bad=bad, precision=prec,
                recall=rec, f1=f1, tnr=tnr, ba=ba, misses=misses, falses=falses)


async def main():
    global URL
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompts", default="baseline,neutral,rubric")
    ap.add_argument("--contexts", default="none,pair,full")
    ap.add_argument("--show-errors", action="store_true")
    ap.add_argument("--url", default=URL)
    ap.add_argument("--tag", default="")
    ap.add_argument("--dump", default="")
    a = ap.parse_args()
    URL = a.url

    con = connect()
    nm = names(con)
    cases = []
    for line in open(EVAL, encoding="utf-8"):
        d = json.loads(line)
        row = con.execute(
            "SELECT channel_id, ts, author_id FROM messages WHERE message_id=?",
            (d["message_id"],),
        ).fetchone()
        if not row:
            continue
        d["window"] = build_window(con, nm, row["channel_id"], row["ts"], d["message_id"])
        d["pair"] = pair_context(con, nm, row["author_id"], row["channel_id"], row["ts"], d["message_id"])
        d["daily"] = daily_context(con, row["author_id"], row["ts"])
        cases.append(d)

    print(f"target: {URL}  {a.tag}")
    print(f"{len(cases)} cases loaded "
          f"({sum(1 for c in cases if c['label']=='violation')} violation / "
          f"{sum(1 for c in cases if c['label']=='ok')} ok)\n")

    print(f"{'prompt':10s} {'context':8s} {'TPR':>5s} {'TNR':>5s} {'BalAcc':>7s} "
          f"{'TP':>3s} {'FP':>3s} {'TN':>3s} {'FN':>3s}  flag-rate")
    print("-" * 72)
    best = None
    for pname in a.prompts.split(","):
        for ctx in a.contexts.split(","):
            res = await run_variant(cases, pname, ctx)
            s = score(res)
            n = s["tp"] + s["fp"] + s["tn"] + s["fn"]
            fr = (s["tp"] + s["fp"]) / n if n else 0
            print(f"{pname:10s} {ctx:8s} {s['recall']:5.2f} {s['tnr']:5.2f} "
                  f"{s['ba']:7.2f} {s['tp']:3d} {s['fp']:3d} {s['tn']:3d} {s['fn']:3d}"
                  f"  {fr:.0%}")
            if a.dump:
                recs = [{"message_id": c["message_id"], "label": c["label"],
                         "pattern": c["pattern"],
                         "pred": 1 if o.get("verdict") == "flag" else 0}
                        for c, o in res]
                with open(f"{a.dump}.{pname}.{ctx}.json", "w", encoding="utf-8") as fh:
                    json.dump(recs, fh)
            if best is None or s["ba"] > best[1]["ba"]:
                best = ((pname, ctx), s)

    (bp, bc), bs = best
    print(f"\nBEST by balanced accuracy: {bp} + {bc}   BA={bs['ba']:.2f} "
          f"(TPR={bs['recall']:.2f} TNR={bs['tnr']:.2f})")
    print("BA 0.50 == coin flip. Eval set is 65% violation, so a flag-everything "
          "policy scores P=0.65 for free.")
    if a.show_errors:
        print("\n-- false positives (flagged, but fine) --")
        for c in bs["falses"][:12]:
            print(f"  {c['author'][:16]:16s} {c['content'][:66]!r}")
        print("\n-- misses (real, not flagged) --")
        for c in bs["misses"][:12]:
            print(f"  [{c['pattern']:20s}] {c['author'][:14]:14s} {c['content'][:56]!r}")
    return 0


sys.exit(asyncio.run(main()))
