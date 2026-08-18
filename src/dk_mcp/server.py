# pyright: reportMissingImports=false
"""server.py — Dungeon Keeper's specs and source as an MCP server.

Exposes the bot's documentation and code to Claude (claude.ai custom connector)
over streamable HTTP, so feature specs drafted in chat are grounded in how DK
actually works instead of being plausible-sounding inventions. Sits on the
Fedora NUC behind a Cloudflare Tunnel, same pattern as /opt/tod.

READ-ONLY BY DESIGN. There is no write path and no database tool. The chat
session produces spec text that Billy pastes into a /dk-feature session, so
every change still lands through the normal gate and commit path.

Environment:
    DK_REPO_ROOT       path to the Dungeon Keeper checkout
                       (default: /home/ben/discord-bots/dungeon-keeper)
    DK_MCP_HOST        bind address              (default: 127.0.0.1)
    DK_MCP_PORT        port                      (default: 8322)
    DK_MCP_PATH        HTTP path for the endpoint (default: /mcp)
                       -> set this to something long and random; it acts
                          as a shared secret since the connector is
                          unauthenticated
    DK_MCP_ALLOWED_HOSTS  comma-separated Host headers to accept
                       (set to the tunnel hostname, dkmcp.billy-bots.com)

Security: the checkout this reads is PRODUCTION -- .env holds the Discord
token, dungeonkeeper.db is the live database, .git carries every secret ever
committed. Every read goes through the allowlist in paths_service.py, which
resolves symlinks and '..' before checking containment. See its docstring and
tests/test_dk_mcp_paths_service.py.

Run:  python -m dk_mcp.server
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from dk_mcp.code_service import CodeService
from dk_mcp.conventions_service import ConventionsService
from dk_mcp.corpus_service import UNSERVED_INDEX_SECTIONS, Catalogue, Kind
from dk_mcp.paths_service import PathDenied, PathGuard
from dk_mcp.search_service import SearchService
from dk_mcp.sections_service import (
    extract_section,
    html_to_text,
    outline,
    strip_markdown_sections,
)

DEFAULT_REPO = "/home/ben/discord-bots/dungeon-keeper"

# A document under this many lines comes back whole; longer ones return an
# outline so the caller can ask for the section it actually wants.
WHOLE_DOC_LINES = 400
PREVIEW_LINES = 60

_guard = PathGuard.for_repo(os.environ.get("DK_REPO_ROOT", DEFAULT_REPO))
_catalogue = Catalogue(_guard)
_search = SearchService(_guard, _catalogue)
_code = CodeService(_guard)
_conventions = ConventionsService(_guard)

server = MCPServer(
    name="dungeon-keeper",
    instructions=(
        "Dungeon Keeper's own specs, plans, conventions and source code, "
        "read-only, for developing feature specs that integrate with how the "
        "bot ACTUALLY works. DK is a Discord bot (thin cogs in "
        "src/bot_modules/, logic in per-feature modules) plus a FastAPI "
        "dashboard (src/web_server/, vanilla-JS panels), SQLite-backed. "
        "THE ONE RULE THAT MATTERS MOST: docs/INDEX.md classifies every spec "
        "as Reference (matches current behavior), Design (written to "
        "implement; may lag the code) or Aspirational (describes things NEVER "
        "BUILT), and CLAUDE.md is explicit that WHEN A SPEC AND THE CODE "
        "DISAGREE, THE CODE WINS. Every document this server returns carries "
        "its classification in a banner -- read it. An aspirational spec "
        "(e.g. duel_minigame_flows_v2.md, which documents Liar's Dice and "
        "Minesweeper flows that do not exist) will otherwise lead you to "
        "design against features nobody ever wrote. When a spec's specifics "
        "matter, check src/ with search_code before relying on them. "
        "SUGGESTED LOOP: (1) feature_brief('<feature>') to ground yourself -- "
        "it returns the relevant specs with their classifications, related "
        "plans with their build status, the user-facing manual section and "
        "the source modules. (2) search_docs / get_doc(path, section=...) to "
        "read the parts that matter; the corpus is 2.7 MB, so fetch sections, "
        "not whole files. (3) search_code / get_code to confirm what is "
        "really built. (4) get_conventions() before writing -- DK has strong "
        "house rules that make a spec wrong if missed: admin configuration "
        "lives on the WEB DASHBOARD and never in Discord slash commands, "
        "dashboard route ids are FROZEN, a new per-user table needs a "
        "docs/data_register.md row in the same commit, a UI change must "
        "update manual.html in the same commit, and every feature ships with "
        "logic-layer tests. "
        "NOT SERVED, deliberately: docs/reviews/ (43 audit documents whose "
        "findings are largely superseded) and docs/testing/ (completed QA "
        "checklists). docs/INDEX.md links to both; those sections are omitted "
        "from what you get, and asking for one of those paths returns an "
        "explicit 'outside the corpus' message rather than a missing file. "
        "This server is READ-ONLY and has no database access: it serves "
        "docs/, docs/plans/, CLAUDE.md, the user-facing manual.html, and "
        "src/. Output is spec text for a human to paste into a dev session."
    ),
)


def _denied(exc: PathDenied) -> dict:
    return {"error": str(exc), "reason": exc.reason.value}


@server.tool()
def list_docs(kind: str | None = None, contains: str | None = None) -> dict:
    """Catalogue every document served, with its INDEX.md classification.

    The map of the corpus: ~133 documents, each with the flavour docs/INDEX.md
    assigns it and the one-line summary from the index table. Cheap to call
    first when you do not yet know what exists.

    Remember that classification is the point: a 'reference' spec matches
    current behavior, a 'design' spec may lag the code, an 'aspirational' spec
    describes things that were never built, and a 'plan' is a build plan whose
    status may be anything from shipped to "proposal, nothing built". When a
    spec and the code disagree, the code wins.

    Args:
        kind: filter to one of reference, design, aspirational, plan,
            agreement, manual, index, unclassified
        contains: substring filter over path, title and summary
    """
    try:
        docs = _catalogue.by_kind(kind) if kind else _catalogue.docs()
    except ValueError:
        return {
            "error": f"Unknown kind {kind!r}",
            "kinds": [k.value for k in Kind],
        }
    if contains:
        needle = contains.lower()
        docs = [
            d
            for d in docs
            if needle in d.path.lower()
            or needle in d.title.lower()
            or needle in d.summary.lower()
        ]
    return {
        "count": len(docs),
        "note": (
            "Classification comes from docs/INDEX.md. The code wins over any "
            "spec. docs/reviews/ and docs/testing/ are not served."
        ),
        "documents": [
            {
                "path": d.path,
                "kind": d.kind.value,
                "title": d.title,
                "summary": d.summary,
                "status_or_notes": d.note,
                "listed_in_index": d.indexed,
            }
            for d in docs
        ],
    }


@server.tool()
def search_docs(query: str, kind: str | None = None, limit: int = 20) -> dict:
    """Search the specs, plans, working agreement and user manual.

    Ranked per document SECTION, so each hit names the heading breadcrumb and
    line to fetch next rather than just a filename. Supports "quoted phrases".

    Results are ordered with reference material above design specs above plans,
    and aspirational specs are demoted -- but never hidden, since they are still
    the record of what was intended. Every hit carries its classification and
    banner. Check the code before relying on any spec's specifics.

    Args:
        query: words to look for; wrap a phrase in double quotes to require it
        kind: restrict to one classification (reference, design, aspirational,
            plan, agreement, manual, index)
        limit: maximum hits (default 20)
    """
    try:
        hits = _search.search(query, kind=kind, limit=max(1, min(limit, 60)))
    except ValueError:
        return {"error": f"Unknown kind {kind!r}", "kinds": [k.value for k in Kind]}
    return {
        "query": query,
        "count": len(hits),
        "hint": (
            "Fetch a hit with get_doc(path, section=...) using its breadcrumb. "
            "Then confirm specifics with search_code -- the code wins."
        ),
        "hits": [
            {
                "path": h.path,
                "kind": h.kind.value,
                "section": h.breadcrumb,
                "line": h.line,
                "snippet": h.snippet,
                "classification": h.banner,
            }
            for h in hits
        ],
    }


@server.tool()
def get_doc(path: str, section: str | None = None) -> dict:
    """Read a served document, or one section of it, with its classification.

    ALWAYS read the returned `classification` banner. It tells you whether the
    document matches current behavior, may lag the code, or describes something
    that was never built.

    Long documents return an outline instead of their full text; ask again with
    `section` (a heading, breadcrumb or anchor -- partial matches are fine).
    manual.html is rendered to readable prose and its sections keep their real
    anchor ids, so you can cite a dashboard deep link.

    Args:
        path: repo-relative, e.g. docs/casino_spec.md, docs/plans/casino.md,
            CLAUDE.md, src/web_server/static/manual.html
        section: heading to extract, e.g. "Payouts" or "Economy > Casino"
    """
    try:
        doc = _catalogue.get(path)
        real = _guard.resolve(doc.path)
    except PathDenied as exc:
        return _denied(exc)

    is_html = doc.path.endswith(".html")
    text = real.read_text(encoding="utf-8", errors="replace")
    if doc.kind is Kind.INDEX:
        text = strip_markdown_sections(text, UNSERVED_INDEX_SECTIONS)

    base = {
        "path": doc.path,
        "kind": doc.kind.value,
        "title": doc.title,
        "summary": doc.summary,
        "classification": doc.banner,
    }

    if section:
        try:
            found = extract_section(text, section, is_html=is_html)
        except LookupError as exc:
            return {**base, "error": str(exc)}
        body = "\n".join(text.splitlines()[found.start - 1 : found.end])
        return {
            **base,
            "section": found.breadcrumb,
            "anchor": found.anchor,
            "lines": f"{found.start}-{found.end}",
            "content": html_to_text(body) if is_html else body,
        }

    lines = text.splitlines()
    if len(lines) <= WHOLE_DOC_LINES:
        return {
            **base,
            "lines": f"1-{len(lines)}",
            "content": html_to_text(text) if is_html else text,
        }

    sections = outline(text, is_html=is_html)
    preview = "\n".join(lines[:PREVIEW_LINES])
    return {
        **base,
        "total_lines": len(lines),
        "note": (
            f"{len(lines)} lines — returning the outline and the first "
            f"{PREVIEW_LINES}. Call get_doc(path, section=...) for the part "
            f"you need."
        ),
        "outline": [
            {
                "section": s.breadcrumb,
                "anchor": s.anchor,
                "lines": f"{s.start}-{s.end}",
            }
            for s in sections
        ],
        "preview": html_to_text(preview) if is_html else preview,
    }


@server.tool()
def get_conventions(topic: str | None = None) -> dict:
    """The house rules a Dungeon Keeper spec has to satisfy.

    Call this BEFORE drafting. DK's conventions are scattered across CLAUDE.md,
    the embed style guide, the dashboard IA doc, the data register and the
    testing docs, and a spec that misses one of them is wrong rather than
    merely incomplete -- putting admin config in a slash command, renaming a
    frozen dashboard route id, or adding a per-user table with no data-register
    row.

    With no topic you get every rule, assembled. With a topic you also get the
    full source document behind it.

    Args:
        topic: one of working-agreement, docs-contract, testing, dashboard,
            privacy, embeds, commits
    """
    if topic is None:
        return {
            "topics": _conventions.names(),
            "content": _conventions.digest(),
        }
    try:
        return {"topic": topic, "content": _conventions.get(topic)}
    except LookupError as exc:
        return {"error": str(exc), "topics": _conventions.names()}


@server.tool()
def search_code(
    pattern: str,
    path_glob: str | None = None,
    regex: bool = False,
    limit: int = 40,
) -> dict:
    """Search the bot's source under src/.

    This is how you settle a disagreement between a spec and reality: the code
    wins, so check it. Useful for confirming a command exists, finding which
    service owns a behavior, or seeing whether an aspirational spec's feature
    was ever written.

    Args:
        pattern: literal text by default
        path_glob: filter paths, e.g. "*casino*", "*.js",
            "src/bot_modules/games/*"
        regex: treat pattern as a regular expression
        limit: maximum hits (default 40)
    """
    try:
        hits = _code.search(
            pattern,
            path_glob=path_glob,
            regex=regex,
            limit=max(1, min(limit, 200)),
        )
    except (ValueError, PathDenied) as exc:
        return {"error": str(exc)}
    return {
        "pattern": pattern,
        "count": len(hits),
        "hits": [{"path": h.path, "line": h.line, "text": h.text} for h in hits],
        "hint": "Read around a hit with get_code(path, start=..., end=...).",
    }


@server.tool()
def get_code(
    path: str,
    start: int | None = None,
    end: int | None = None,
    symbol: str | None = None,
) -> dict:
    """Read a source file under src/, a line range, or one named definition.

    Args:
        path: repo-relative, e.g. src/bot_modules/services/casino_logic.py
        start: first line (1-based)
        end: last line
        symbol: a def/class name to extract instead of a line range
    """
    try:
        if symbol:
            body, first, last = _code.symbol(path, symbol)
            return {
                "path": path,
                "symbol": symbol,
                "lines": f"{first}-{last}",
                "content": body,
            }
        body, first, last, total = _code.read(path, start=start, end=end)
        return {
            "path": path,
            "lines": f"{first}-{last}",
            "total_lines": total,
            "content": body,
        }
    except PathDenied as exc:
        return _denied(exc)
    except LookupError as exc:
        return {"path": path, "error": str(exc)}


@server.tool()
def feature_brief(feature: str) -> dict:
    """Ground yourself on one feature before designing anything for it.

    One call that assembles what you would otherwise need five for: the specs
    that mention it WITH their classifications, the implementation plans and
    their build status, the user-facing manual section, and the source modules
    that implement it.

    Read the classifications. A feature can have a confident-looking spec and
    no code at all -- survey_spec.md is filed as a design spec and its own
    index note says "Zero code ... not started". The source modules listed here
    are the ground truth.

    Args:
        feature: a feature name or keyword, e.g. "casino", "pen pals", "quests"
    """
    hits = _search.search(feature, limit=40)
    by_kind: dict[str, list[dict]] = {}
    for hit in hits:
        entries = by_kind.setdefault(hit.kind.value, [])
        if any(e["path"] == hit.path for e in entries):
            continue
        entries.append(
            {
                "path": hit.path,
                "section": hit.breadcrumb,
                "line": hit.line,
                "classification": hit.banner,
            }
        )

    needle = feature.lower().replace(" ", "")
    catalogue = [
        {
            "path": d.path,
            "kind": d.kind.value,
            "title": d.title,
            "status_or_notes": d.note,
            "listed_in_index": d.indexed,
        }
        for d in _catalogue.docs()
        if needle in d.path.lower().replace("-", "").replace("_", "")
    ]

    modules = sorted(
        {
            "/".join(p.split("/")[:4])
            for p in _code.files(f"*{feature.split()[0].lower()}*", limit=120)
        }
    )

    return {
        "feature": feature,
        "read_this_first": (
            "Classifications matter more than content: reference matches the "
            "running bot, design may lag it, aspirational was never built, and "
            "a plan may be a proposal with no code. Confirm anything load-"
            "bearing with search_code — the code wins."
        ),
        "documents_named_for_it": catalogue,
        "search_hits_by_kind": by_kind,
        "source_modules": modules,
        "next": (
            "get_doc(path, section=...) to read; search_code to verify; "
            "get_conventions() before you write."
        ),
    }


@server.prompt()
def draft_spec(feature: str = "") -> str:
    """Draft a Dungeon Keeper feature spec, grounded in the real codebase."""
    return (
        f"Help me develop a feature spec for Dungeon Keeper: {feature}\n\n"
        "Work in this order, and do not skip the grounding:\n\n"
        "1. GROUND FIRST. Call feature_brief() on the feature and on anything "
        "adjacent to it. Read the classification banner on every document you "
        "open. A spec classified 'aspirational' describes something that was "
        "never built; a plan may be a proposal with no code. Where a spec's "
        "specifics matter, verify them with search_code — when a spec and the "
        "code disagree, the code wins.\n\n"
        "2. FIND THE PRECEDENT. DK is a mature bot; almost every new feature "
        "resembles an existing one. Find the closest built feature, read how "
        "it is actually structured (cog + logic/service module + dashboard "
        "panel + tests), and model the new one on it rather than inventing a "
        "shape.\n\n"
        "3. READ THE HOUSE RULES. Call get_conventions(). The ones that most "
        "often make a draft wrong: admin configuration belongs on the WEB "
        "DASHBOARD and never in Discord slash commands; Discord is for member "
        "self-service and mod actions; dashboard route ids are frozen; one "
        "dial beats several overlapping toggles; NSFW gates on "
        "channel.is_nsfw(); never ship a preference that isn't enforced.\n\n"
        "4. OFFER 2–3 DIRECTIONS with real tradeoffs, recommend one, and "
        "commit once I pick.\n\n"
        "5. WRITE THE SPEC to match the existing ones in docs/ in shape and "
        "voice — read one first. State explicitly: the member-facing surface, "
        "the dashboard panel and its settings, the data model (and whether any "
        "table holds per-user data), the guards and safety gates, and the "
        "logic-layer tests that will cover each branch.\n\n"
        "6. LIST THE SAME-COMMIT OBLIGATIONS the change triggers: the docs/ "
        "spec and its INDEX.md classification, manual.html if the UI changes, "
        "a docs/data_register.md row plus a purge-or-preserve decision if it "
        "stores per-user data, the tests, and the commit's 'Testing:' "
        "checkbox lines.\n\n"
        "Output spec text I can paste into a dev session. This server is "
        "read-only; you are not editing the repo."
    )


@server.prompt()
def check_spec_against_code(path: str = "") -> str:
    """Audit an existing spec against what the code actually does."""
    return (
        f"Check whether this Dungeon Keeper spec still matches the code: "
        f"{path}\n\n"
        "docs/INDEX.md classifies specs as Reference (matches current "
        "behavior), Design (may lag the code) or Aspirational (never fully "
        "built), and the code wins in any disagreement. Read the "
        "classification banner get_doc returns, then verify the document's "
        "load-bearing claims against src/ with search_code and get_code: does "
        "each named command, setting, table and flow actually exist, and does "
        "it behave as described?\n\n"
        "Report: claims confirmed, claims that have drifted (with the "
        "file:line that disproves them), and anything described that does not "
        "exist at all. Say whether the document's INDEX classification still "
        "looks right, since a behavior change is supposed to update the spec "
        "and its classification in the same commit."
    )


def main() -> None:
    host = os.environ.get("DK_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("DK_MCP_PORT", "8322"))
    path = os.environ.get("DK_MCP_PATH", "/mcp")

    allowed = os.environ.get("DK_MCP_ALLOWED_HOSTS", "")
    if allowed:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[h.strip() for h in allowed.split(",") if h.strip()],
        )
    else:
        # Behind Cloudflare Tunnel the public hostname arrives in Host; without
        # DK_MCP_ALLOWED_HOSTS we disable header validation. Set it in prod.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    server.run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path=path,
        stateless_http=True,
        transport_security=security,
    )


if __name__ == "__main__":
    main()
