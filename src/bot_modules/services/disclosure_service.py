"""Turn a subject access export into something a member can actually read.

``privacy_service.export_user_data`` answers Art 15 and Art 20 completely and
unreadably: a real subject in the main guild produces 281,869 rows of JSON
across 150-odd tables. That satisfies "give them their data" and fails Art 12(1),
which asks for it *"in a concise, transparent, intelligible and easily accessible
form, using clear and plain language."*

This module is the layer above. It joins two things that already exist:

* the **export**, which knows how many rows name this member and where; and
* ``docs/data_register.md``, which knows each table's purpose, retention and
  erasure decision.

Neither is a report. The join of the two is. The register supplies the facts and
``disclosure_copy`` supplies the language; nothing here invents either.

**The report never names a second member.** Categories built from tables in
``privacy_service.THIRD_PARTY_TABLES`` contribute a count and nothing else, so
Art 15(4) is satisfied by construction rather than by an operator remembering to
redact before sending. ``tests/test_disclosure_service.py`` asserts it.

Read-only throughout: the connection is opened ``mode=ro`` by the caller.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .disclosure_copy import (
    CATEGORIES,
    composed_values,
    FEATURE_TO_CATEGORY,
    SELF_AUTHORED_VALUES,
)
from .privacy_service import LIST_VALUED_MEMBER_COLUMNS, THIRD_PARTY_TABLES

log = logging.getLogger(__name__)

# Time columns worth trying, best first. A table with none of these simply
# reports no date span — guessing a span from an unrelated column would be
# worse than staying quiet.
_TIME_COLUMNS = (
    "ts", "created_at", "sent_at", "occurred_at", "awarded_at", "logged_at",
    "started_at", "added_at", "joined_at", "set_at", "claimed_at", "paid_at",
    "local_day", "day", "updated_at", "last_action_at",
)

_RETENTION_UNKNOWN = {"", "?", "n/a", "—", "-"}


# ---------------------------------------------------------------------------
# register parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegisterRow:
    """One row of the data register's processing table."""

    tables: tuple[str, ...]
    globs: tuple[str, ...]
    feature: str
    data_class: str
    retention: str
    purge: str
    processor: str
    notes: str


def parse_register(text: str) -> list[RegisterRow]:
    """Parse the processing table out of ``docs/data_register.md``.

    Column *positions* are the contract, not column names: the register has
    carried ``Table / store | Feature | Data class | Retention | Purge? |
    Processor | Notes`` since it was promoted out of ``docs/reviews/``, and the
    session that owns the file has confirmed the shape is stable.
    """
    marker = "| Table / store |"
    if marker not in text:
        raise ValueError("data_register.md: processing table not found")

    # Bound the scan to the processing table. The register continues past it
    # with "Deliberately not personal data" and "Processors", both of which are
    # narrower Markdown tables — reading on treats their 3-column rows as
    # malformed processing rows and warns about tables that are, correctly,
    # not there.
    body = text[text.index(marker) :]
    for heading in re.finditer(r"^#{2,} ", body, re.M):
        body = body[: heading.start()]
        break

    rows: list[RegisterRow] = []
    malformed: list[str] = []
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cells = _split_row(line)
        if cells[0].startswith("Table /") or set(cells[0]) <= set("- :"):
            continue
        if len(cells) < 7:
            # Short row: the columns cannot be trusted by position, and reading
            # the wrong cell would attribute one table's retention to another.
            if cells[0]:
                malformed.append(cells[0][:60])
            continue
        if len(cells) > 7:
            # Never seen in practice once escaped pipes are handled, but a stray
            # unescaped pipe would silently shift every column left. Refuse it
            # rather than publish a misattributed retention period.
            malformed.append(cells[0][:60])
            continue

        names, globs = _table_tokens(cells[0])
        if not names and not globs:
            continue
        rows.append(
            RegisterRow(
                tables=names,
                globs=globs,
                feature=cells[1],
                data_class=cells[2],
                retention=cells[3],
                purge=cells[4],
                processor=cells[5],
                notes=cells[6],
            )
        )
    if malformed:
        log.warning(
            "Disclosure: %d register row(s) could not be read by column and "
            "were skipped: %s",
            len(malformed),
            ", ".join(malformed),
        )
    parse_register.malformed = malformed  # type: ignore[attr-defined]
    return rows


# A Markdown cell escapes a literal pipe as ``\|``. The register has one row
# (``risky_pending_questions``) whose Notes contain SQL string concatenation —
# ``',' \|\| col \|\| ','`` — and splitting naively on "|" turns that 7-column
# row into 11 fields, shifting Processor and Notes onto fragments of the SQL.
# Retention and Purge survived by luck there; a row escaping a pipe any earlier
# would have shifted those too and reported one table's retention as another's.
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def _split_row(line: str) -> list[str]:
    """Split a Markdown table row on unescaped pipes, then unescape."""
    parts = _UNESCAPED_PIPE.split(line.strip())
    if parts and not parts[0].strip():
        parts = parts[1:]
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    return [c.replace("\\|", "|").strip() for c in parts]


def _table_tokens(cell: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Pull table names and ``prefix_*`` globs out of a register's first cell.

    The cell is prose as often as it is a list — ``econ_wallets, econ_ledger,
    econ_* (49 tables)`` and ``jails, warnings, tickets, ticket_participants,
    policy_tickets, role_grant audit`` are both real. Tokens that are plainly
    English ("audit", "files") are dropped by requiring an underscore, with a
    short allow-list for the single-word table names that genuinely exist.
    """
    singles = {
        "messages", "jails", "warnings", "tickets", "duels", "bios", "todos",
        "whispers", "docs", "announcements",
    }
    names: list[str] = []
    globs: list[str] = []
    for tok in re.findall(r"[a-z][a-z0-9_]*\*?", cell):
        if tok.endswith("*"):
            globs.append(tok[:-1])
        elif "_" in tok or tok in singles:
            names.append(tok)
    return tuple(dict.fromkeys(names)), tuple(dict.fromkeys(globs))


def build_table_index(rows: list[RegisterRow]) -> dict[str, int]:
    """Map table name -> register row index, exact names beating globs."""
    index: dict[str, int] = {}
    for i, row in enumerate(rows):
        for name in row.tables:
            index.setdefault(name, i)
    return index


def resolve_row(
    table: str, rows: list[RegisterRow], index: dict[str, int]
) -> RegisterRow | None:
    if table in index:
        return rows[index[table]]
    best: tuple[int, int] | None = None  # (prefix length, row index)
    for i, row in enumerate(rows):
        for g in row.globs:
            if table.startswith(g) and (best is None or len(g) > best[0]):
                best = (len(g), i)
    return rows[best[1]] if best else None


# ---------------------------------------------------------------------------
# cleaning register prose for a member's eyes
# ---------------------------------------------------------------------------

_CODE_REF = re.compile(r"\(`[^`]+`[^)]*\)")
_REVIEW_TAG = re.compile(
    r"\((?:[A-Z]\d[^)]*|[a-z-]+ [A-Z]\d[^)]*|migration \d+|verified[^)]*)\)"
)
_COMMIT = re.compile(r"\b[0-9a-f]{7,40}\b")
_DURATION = re.compile(
    r"\b\d+\s*-?\s*(d|days?|h|hours?|weeks?|months?|years?|mo|yr)\b"
)
_EVENT_RETENTION = re.compile(
    r"\buntil\b|\bon (approve|reject|round close|leave)\b|overwritten|"
    r"cleared by|kept as|kept for|pruned|live state|dies with"
)
_PARTIAL_PURGE = re.compile(
    r"\bSPLIT\b|\bASYMMETRIC\b|\bPartial\b|Anonymised, not deleted"
)


def clean_cell(text: str) -> str:
    """Strip the register's engineering furniture out of a cell.

    Backticks, commit hashes, ``file.py:123`` references, review tags like
    ``(health G3)`` and status ticks are all meaningful to a maintainer and
    noise to a member.
    """
    out = _CODE_REF.sub("", text)
    out = _REVIEW_TAG.sub("", out)
    out = _COMMIT.sub("", out)
    out = out.replace("`", "").replace("**", "").replace("✓", "")
    out = re.sub(r"\s*[—-]\s*$", "", out.strip())
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip(" ;,—-")


def _is_declared_mixed(cell: str) -> bool:
    """True when the register explicitly marks a cell as covering mixed periods."""
    return clean_cell(cell).lower().startswith("mixed")


def classify_retention(cell: str) -> str:
    """``permanent`` | ``period`` | ``event`` | ``undecided`` | ``mixed``.

    The register's 2026-09-05 revision replaced the word "indefinite", which had
    been doing two incompatible jobs, with a four-value vocabulary: a decision
    to keep something forever (``permanent — <ground>``) now reads differently
    from never having taken one (``undecided``). That distinction matters to a
    member — "we keep this deliberately, here is why" and "nobody ever decided"
    are not the same disclosure — so the report carries it through.

    Older text using "indefinite" is still understood, and maps to
    ``undecided``: the word's own ambiguity is exactly why it was retired, and
    guessing a decision was taken would be the flattering reading rather than
    the honest one.
    """
    low = clean_cell(cell).lower()
    if not low or low in _RETENTION_UNKNOWN or low.startswith("needs decision"):
        return "undecided"
    if low.startswith("undecided"):
        return "undecided"
    # The register marks a cell covering several tables with differing periods
    # by opening it "Mixed." Its own vocabulary note says to take no single
    # duration from such a cell, so this is read structurally rather than
    # inferred from the prose — see _durations_from.
    if low.startswith("mixed"):
        return "mixed"

    permanent = low.startswith("permanent") or "preserve" in low
    bounded = bool(_DURATION.search(low))
    # "indefinite" is legacy vocabulary and deliberately NOT read as a decision.
    legacy_unbounded = "indefinite" in low

    # Order matters, and this is the direction that matters most. A cell like
    # "indefinite, except xp_events: kept 90 days once an admin enables it"
    # holds both, and testing for the duration first reported it as a clean
    # 90-day deletion — telling a member their data expires when in fact most
    # of it is kept forever. A false promise of erasure is the worst thing this
    # report could say, so an unbounded reading always survives into the answer.
    if (permanent or legacy_unbounded) and bounded:
        return "mixed"
    if bounded:
        return "period"
    if permanent:
        return "permanent"
    if legacy_unbounded:
        return "undecided"
    if _EVENT_RETENTION.search(low):
        return "event"
    return "undecided"


def purge_verdict(cell: str) -> str:
    """``yes`` | ``partial`` | ``no`` | ``unknown`` from the Purge? cell.

    The register writes its *verdict* in capitals (``YES``, ``NO``, ``SPLIT``,
    ``ASYMMETRIC``) and its reasoning in ordinary prose, so the match is
    case-sensitive on purpose. Lower-casing first reads "No Art 17(3) ground
    worth claiming" — the justification for a **purge** — as a refusal to
    delete, and would tell a member their data is retained when it is erased.

    Order matters too: a cell naming both outcomes ("role_events YES, others
    NO", or a YES that preserves one table) is partial, and must be caught
    before the plain tests or it would be reported as a clean deletion.
    """
    text = clean_cell(cell)
    low = text.lower()
    if not text or low in {"n/a", "—", "-"} or low.startswith("needs decision"):
        return "unknown"

    has_yes = bool(re.search(r"\bYES\b", text))
    has_no = bool(
        re.match(r"^\W*NO\b", text)
        or re.search(r"\bSTILL NO\b", text)
        or re.search(r"\bothers\s+NO\b", text)
    )
    preserves = "preserve" in low or "preserved" in low

    if _PARTIAL_PURGE.search(text) or (has_yes and (has_no or preserves)):
        return "partial"
    if has_no or (preserves and not has_yes):
        return "no"
    if has_yes or low.startswith("purge") or low.startswith("self-service"):
        return "yes"
    if "decided" in low and re.search(r"ttl|sweep|self-service|forget-me", low):
        return "yes"
    return "unknown"


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


@dataclass
class CategoryReport:
    key: str
    title: str
    blurb: str
    purpose: str
    rows: int = 0
    tables: int = 0
    third_party_tables: int = 0
    earliest: str | None = None
    latest: str | None = None
    retentions: list[str] = field(default_factory=list)
    durations: list[tuple[int, str]] = field(default_factory=list)
    retention_kinds: set[str] = field(default_factory=set)
    purge_yes: int = 0
    purge_no: int = 0
    purge_partial: int = 0
    purge_unknown: int = 0

    @property
    def purge_partial_free(self) -> bool:
        """True when every table here is erased outright on request."""
        return self.purge_partial == 0 and self.purge_yes > 0
    preserved: str = ""
    preserved_reasons: list[str] = field(default_factory=list)
    values: list[tuple[str, str]] = field(default_factory=list)
    cross_guild_tables: list[str] = field(default_factory=list)


@dataclass
class Report:
    guild_id: int
    user_id: int
    generated_at: str
    total_rows: int
    categories: list[CategoryReport]
    unmapped_tables: list[str]
    cross_guild_tables: list[str]
    list_valued_hits: dict[str, int]
    operator_notes: list[str]


def _time_column(cols: list[str]) -> str | None:
    return next((c for c in _TIME_COLUMNS if c in cols), None)


def _to_date(value) -> str | None:
    """Normalise the register's three timestamp shapes to ``YYYY-MM-DD``."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 2e10:  # milliseconds
            v /= 1000.0
        if not 1e9 < v < 2e10:
            return None
        return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%d")
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    if re.match(r"\d{4}-\d{2}-\d{2}[T ]", text):
        return text[:10]
    return None


def _span(
    conn: sqlite3.Connection, table: str, matched: list[str], guild_id: int, user_id: int
) -> tuple[str | None, str | None]:
    try:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    except sqlite3.Error:
        return None, None
    tcol = _time_column(cols)
    usable = [c for c in matched if c in cols]
    if not tcol or not usable:
        return None, None
    where = " OR ".join(f'"{c}" = ?' for c in usable)
    params: list[int] = [user_id] * len(usable)
    sql = f'SELECT MIN("{tcol}"), MAX("{tcol}") FROM "{table}" WHERE ({where})'
    if "guild_id" in cols:
        sql += " AND guild_id = ?"
        params.append(guild_id)
    try:
        lo, hi = conn.execute(sql, tuple(params)).fetchone()
    except sqlite3.Error as exc:
        log.warning("Disclosure: span failed on %s (%s)", table, exc)
        return None, None
    return _to_date(lo), _to_date(hi)


def scan_list_valued(
    conn: sqlite3.Connection, user_id: int
) -> dict[str, int]:
    """Close the export's documented list-column blind spot.

    Twelve columns store member ids inside a JSON or CSV list, where an equality
    match cannot see them, so they are absent from every export produced so far.
    Scanning twelve *known* columns for one id is cheap — this is not
    ``privacy_coverage.py``'s full-database value sweep — so the report closes
    the gap rather than merely declaring it.
    """
    needle = str(user_id)
    hits: dict[str, int] = {}
    for table, column in LIST_VALUED_MEMBER_COLUMNS:
        try:
            rows = conn.execute(
                f'SELECT "{column}" FROM "{table}" WHERE "{column}" LIKE ?',
                (f"%{needle}%",),
            ).fetchall()
        except sqlite3.Error:
            continue  # table absent in this schema
        n = 0
        for (raw,) in rows:
            if raw is None:
                continue
            text = str(raw)
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                parsed = [p.strip() for p in text.split(",")]
            if isinstance(parsed, dict):
                parsed = list(parsed.values())
            if not isinstance(parsed, list):
                parsed = [parsed]
            if any(str(item).strip() == needle for item in _flatten(parsed)):
                n += 1
        if n:
            hits[f"{table}.{column}"] = n
    return hits


def _flatten(items):
    for item in items:
        if isinstance(item, (list, tuple)):
            yield from _flatten(item)
        elif isinstance(item, dict):
            yield from _flatten(list(item.values()))
        else:
            yield item


def build_report(
    conn: sqlite3.Connection,
    export: dict,
    register_text: str,
    guild_id: int,
    user_id: int,
) -> Report:
    """Join the export against the register into a member-readable summary."""
    reg_rows = parse_register(register_text)
    index = build_table_index(reg_rows)

    cats = {
        c.key: CategoryReport(
            c.key, c.title, c.blurb, c.purpose, preserved=c.preserved
        )
        for c in CATEGORIES
    }
    unmapped: list[str] = []
    undecided: list[str] = []
    cross_guild: list[str] = []
    notes: list[str] = []

    for table, payload in export["tables"].items():
        n = len(payload["rows"])
        if not n:
            continue
        reg = resolve_row(table, reg_rows, index)
        if reg is None or reg.feature not in FEATURE_TO_CATEGORY:
            unmapped.append(table)
            continue
        cat = cats[FEATURE_TO_CATEGORY[reg.feature]]
        cat.rows += n
        cat.tables += 1

        is_third_party = table in THIRD_PARTY_TABLES
        if is_third_party:
            cat.third_party_tables += 1

        if not payload.get("guild_scoped", True):
            cross_guild.append(table)
            cat.cross_guild_tables.append(table)

        retention = clean_cell(reg.retention)
        kind = classify_retention(reg.retention)
        if kind == "mixed":
            cat.retention_kinds.update({"permanent", "period"})
        else:
            cat.retention_kinds.add(kind)
        if kind in ("period", "mixed") and not _is_declared_mixed(reg.retention):
            # A declared-mixed cell lists several tables' periods at once. The
            # first number in it belongs to whichever table happens to be named
            # first, so quoting it as "the longest" would be arbitrary — and
            # this column has already produced two cells where the number
            # present described something *other* than this store's retention
            # (xp_daily's 90 days governed the table it replaces; whispers' 30
            # days was a visibility rule that deletes nothing). Take no
            # duration; the sentence degrades to "some is deleted
            # automatically" without a period, which is true.
            dur = _humanise_duration(retention)
            if dur and dur not in cat.durations:
                cat.durations.append(dur)
        if retention and retention not in cat.retentions:
            cat.retentions.append(retention)
        if kind == "undecided":
            undecided.append(table)

        verdict = purge_verdict(reg.purge)
        if verdict == "yes":
            cat.purge_yes += 1
        elif verdict == "partial":
            cat.purge_partial += 1
            reason = re.sub(r"^(SPLIT|ASYMMETRIC|Partial)\W*", "", clean_cell(reg.purge))
            reason = _short(reason, 200)
            if reason and reason not in cat.preserved_reasons:
                cat.preserved_reasons.append(reason)
        elif verdict == "no":
            cat.purge_no += 1
            reason = clean_cell(reg.purge)
            reason = re.sub(r"^NO\s*[—-]?\s*", "", reason).strip()
            if reason and reason not in cat.preserved_reasons:
                cat.preserved_reasons.append(reason)
        else:
            cat.purge_unknown += 1
            notes.append(
                f"{table}: register Purge? cell does not say yes or no "
                f"('{reg.purge[:40]}')"
            )

        lo, hi = _span(conn, table, payload.get("matched_columns", []), guild_id, user_id)
        if lo and (cat.earliest is None or lo < cat.earliest):
            cat.earliest = lo
        if hi and (cat.latest is None or hi > cat.latest):
            cat.latest = hi

        # Self-authored values, and never from a third-party table.
        if not is_third_party:
            for row in payload["rows"][:5]:
                for label, value in composed_values(table, row):
                    cat.values.append((label, value))
            for vt, vc, label in SELF_AUTHORED_VALUES:
                if vt != table:
                    continue
                for row in payload["rows"][:5]:
                    val = row.get(vc)
                    if val is None or val == "":
                        continue
                    if isinstance(val, (int, float)) and vc.endswith("_at"):
                        val = _to_date(val) or val
                    cat.values.append((label, _short(val)))

    if undecided:
        # One line, not thirty. This is the register's own open question — the
        # report only surfaces how much of a member's answer it touches.
        notes.insert(
            0,
            f"{len(undecided)} of this member's {len(export['tables'])} tables "
            "have no retention decision, so the report tells them so: "
            + ", ".join(sorted(undecided)),
        )

    ordered = [cats[c.key] for c in CATEGORIES if cats[c.key].rows]

    return Report(
        guild_id=guild_id,
        user_id=user_id,
        generated_at=datetime.now(timezone.utc).strftime("%d %B %Y"),
        total_rows=sum(c.rows for c in ordered),
        categories=ordered,
        unmapped_tables=sorted(unmapped),
        cross_guild_tables=sorted(set(cross_guild)),
        list_valued_hits=scan_list_valued(conn, user_id),
        operator_notes=notes,
    )


def _short(value, limit: int = 300) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

_INTRO = """\
This document lists the information **{server}** holds about you, why it is held, and how long it is kept. It was produced on request under
Article 15 of the UK GDPR.

It is a summary, written to be readable. It is not the whole of your data: the
full copy travels with it as a separate file, one line per record. Use this
document to understand what exists; use that file if you want every value.
"""

_RIGHTS = """\
## Your rights

You can ask the server to do any of the following, at any time, free of charge:

- **See your data.** This document, and the file that came with it.
- **Correct it.** If something here is wrong, say so and it will be fixed.
- **Delete it.** Use `/delete_me` in the server, or ask a moderator. Most of
  what is listed above is erased immediately. The exceptions are named in each
  section, and they exist because the record either protects someone else or is
  the evidence behind a decision that could be challenged.
- **Take it elsewhere.** The file that came with this document is machine
  readable and yours to keep.
- **Object, or complain.** If you are unhappy with how your data is handled you
  can raise it with the server owner, and you can complain to the Information
  Commissioner's Office at ico.org.uk.
"""


def _plural(n: int, word: str = "record") -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def _humanise_duration(text: str) -> tuple[int, str] | None:
    """``"90d sweep"`` -> ``(90, "90 days")``. Returns days for comparison."""
    m = _DURATION.search(text.lower())
    if not m:
        return None
    n = int(re.search(r"\d+", m.group(0)).group(0))
    unit = m.group(1)
    if unit.startswith("h"):
        return max(1, n // 24), f"{n} hours"
    if unit.startswith("w"):
        return n * 7, f"{n} week" + ("" if n == 1 else "s")
    if unit.startswith(("mo", "month")):
        return n * 30, f"{n} month" + ("" if n == 1 else "s")
    if unit.startswith(("y", "yr")):
        return n * 365, f"{n} year" + ("" if n == 1 else "s")
    return n, f"{n} day" + ("" if n == 1 else "s")


def _retention_sentence(cat: CategoryReport) -> str:
    """State retention from the register's *classification*, not its prose.

    The Retention cells are maintainer notes — they carry table names, commit
    hashes, live row counts and dashboard paths. Pasting one into a member's
    document leaks internals and reads as gibberish. So the report takes only
    what it can state plainly: which of the four kinds apply, and the longest
    period if one exists.

    ``permanent`` and ``undecided`` are worded apart on purpose. Collapsing
    them into "kept indefinitely" is what the register itself stopped doing on
    2026-09-05: a member is entitled to know the difference between data kept
    for a stated reason and data kept because nobody chose otherwise.
    """
    kinds = cat.retention_kinds
    longest = max(cat.durations, key=lambda d: d[0])[1] if cat.durations else None

    if kinds == {"permanent"}:
        return (
            "**How long it is kept.** Permanently. This is a deliberate "
            "decision rather than an oversight, and the reason is given "
            "just below."
        )
    if kinds == {"period"} and longest:
        return (
            "**How long it is kept.** It is deleted automatically. The longest "
            f"anything here is held is {longest}."
        )
    if kinds == {"undecided"}:
        return (
            "**How long it is kept.** No deletion date has been set for this. "
            "In practice that means it is kept until you ask for it to be "
            "removed — not because a period was chosen, but because none has "
            "been."
        )
    if kinds == {"event"}:
        return (
            "**How long it is kept.** Until it is no longer needed — these "
            "records are removed when the thing they describe ends, rather "
            "than on a fixed timetable."
        )

    parts = []
    if "period" in kinds and longest:
        parts.append(f"some is deleted automatically, the longest after {longest}")
    elif "period" in kinds:
        parts.append("some is deleted automatically")
    if "event" in kinds:
        parts.append("some is removed once it is no longer needed")
    if "permanent" in kinds:
        parts.append("some is kept permanently, for the reason given below")
    if "undecided" in kinds:
        parts.append("and for some of it no deletion date has been set at all")
    return "**How long it is kept.** Mixed — " + "; ".join(parts) + "."


def _erasure_sentence(cat: CategoryReport) -> str:
    """What survives an erasure request, in the category's own authored words."""
    everything_goes = cat.purge_no == 0 and cat.purge_partial == 0 and cat.purge_yes > 0
    if everything_goes:
        return (
            "**If you ask for your data to be deleted,** all of this is erased."
        )
    if cat.purge_yes == 0 and cat.purge_partial == 0 and cat.purge_no:
        return (
            "**If you ask for your data to be deleted,** this is kept. "
            + cat.preserved
        )
    return (
        "**If you ask for your data to be deleted,** most of this is erased. "
        "What is kept: " + cat.preserved
    )


def render_markdown(report: Report, server_name: str = "this") -> str:
    out: list[str] = []
    out.append("# What this server knows about you\n")
    out.append(_INTRO.format(server=server_name))
    out.append(
        f"**Prepared for:** Discord user `{report.user_id}`  \n"
        f"**Date:** {report.generated_at}  \n"
        f"**Total records held:** {report.total_rows:,}\n"
    )

    out.append("## At a glance\n")
    out.append("| What | How much | Oldest | Most recent |")
    out.append("|---|---|---|---|")
    for cat in report.categories:
        span_lo = cat.earliest or "—"
        span_hi = cat.latest or "—"
        out.append(f"| {cat.title} | {cat.rows:,} | {span_lo} | {span_hi} |")
    out.append("")

    for cat in report.categories:
        out.append(f"## {cat.title}\n")
        out.append(cat.blurb + "\n")
        out.append(f"**Why it is held.** {cat.purpose}\n")
        span = ""
        if cat.earliest and cat.latest:
            span = f", the earliest from {cat.earliest} and the most recent {cat.latest}"
        elif cat.earliest:
            span = f", going back to {cat.earliest}"
        out.append(f"**How much.** {_plural(cat.rows)}{span}.\n")
        out.append(_retention_sentence(cat) + "\n")
        out.append(_erasure_sentence(cat) + "\n")

        if cat.values:
            out.append("**What it actually says:**\n")
            for label, value in cat.values[:12]:
                out.append(f"- {label}: {value}")
            out.append("")

        if cat.third_party_tables:
            out.append(
                "> Some of these records name another member as well as you. "
                "Their details are counted here but not shown, because "
                "answering your request must not expose someone else's "
                "information.\n"
            )
        if cat.cross_guild_tables:
            out.append(
                "> Part of this is not filed per-server, so the figure above "
                "covers every server this bot is in, not only this one.\n"
            )

    out.append("## What is not in here\n")
    limits = [
        "Anything Discord itself holds. This covers only what the server's own "
        "bot has stored; your Discord account, and any data Discord keeps, is "
        "theirs and you would ask them.",
        "Messages in servers other than this one, except where a record is not "
        "filed per-server — those are flagged above.",
    ]
    if report.list_valued_hits:
        n = sum(report.list_valued_hits.values())
        limits.append(
            f"{_plural(n)} where your ID is stored inside a list alongside "
            "other players — group games and similar. These are **not** "
            "counted in the totals above, because the main search cannot see "
            "inside a list. They were found by a separate check and are named "
            "here so the picture is complete."
        )
    for item in limits:
        out.append(f"- {item}")
    out.append("")

    out.append("## Who else sees it\n")
    out.append(
        "Almost nothing leaves the machine the bot runs on. The exceptions are "
        "listed in the server's privacy notice, in the dashboard's Help "
        "section, under *Your Data & Privacy → Where your data goes*. Read that "
        "alongside this document — it is the current statement of which "
        "outside services are involved and what reaches them.\n"
    )
    out.append(_RIGHTS)
    return "\n".join(out)
