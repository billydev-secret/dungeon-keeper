"""The channel-panel registry, and that every entry still resolves.

``POST /api/panels/{key}/post`` reaches its cog method by *name*, looked up at
request time. Nothing at import time catches a rename or a deleted method — the
first sign would be a 503 in production when an admin presses Post. The
resolution test below is the compile-time check the dynamic lookup doesn't get.

Added 2026-07-28 with the route that replaced six panel-posting slash commands.
The ``host_page`` checks below arrived the same day, when the shared Channel
Panels page was split and each control moved onto its feature's config page:
with no page listing every panel, nothing else notices a spec whose control was
never drawn anywhere.
"""
from __future__ import annotations

import importlib
import pathlib
import re

import pytest

from bot_modules.services.panel_registry import (
    PANEL_SPECS,
    get_panel_spec,
    list_panel_specs,
)

# Where each registered cog class lives. Kept here rather than in the registry
# because only tests need to import cogs — the registry stays Discord-free so
# it can be read without a bot.
_COG_MODULES = {
    "EconomyCog": "bot_modules.cogs.economy_cog",
    "VoiceMasterCog": "bot_modules.cogs.voice_master_cog",
    "GuessCog": "bot_modules.cogs.guess_cog",
    "JailCog": "bot_modules.cogs.jail_cog",
    "RoleGrantCog": "bot_modules.cogs.role_grant_cog",
}


@pytest.mark.parametrize("spec", PANEL_SPECS, ids=lambda s: s.key)
def test_every_panel_resolves_to_a_real_cog_method(spec):
    """A renamed or deleted method would otherwise only surface as a 503 when
    an admin presses Post."""
    module = importlib.import_module(_COG_MODULES[spec.cog])
    cog_class = getattr(module, spec.cog)
    method = getattr(cog_class, spec.method, None)
    assert method is not None, f"{spec.cog}.{spec.method} is gone"
    assert callable(method)


@pytest.mark.parametrize("spec", PANEL_SPECS, ids=lambda s: s.key)
def test_every_panel_method_is_a_coroutine(spec):
    """The route awaits the result; a sync method would raise at request time."""
    import inspect

    module = importlib.import_module(_COG_MODULES[spec.cog])
    method = getattr(getattr(module, spec.cog), spec.method)
    assert inspect.iscoroutinefunction(method)


def test_panel_keys_are_unique():
    keys = [spec.key for spec in PANEL_SPECS]
    assert len(keys) == len(set(keys))


def test_lookup_returns_the_matching_spec():
    spec = get_panel_spec("economy-panel")
    assert spec is not None
    assert spec.cog == "EconomyCog"
    assert spec.method == "post_economy_panel"


def test_lookup_returns_none_for_an_unknown_key():
    """None rather than KeyError, so the route can answer 404 in its own words."""
    assert get_panel_spec("no-such-panel") is None


def test_every_spec_carries_dashboard_copy():
    """The panel renders label and description directly; a blank one would ship
    an unlabelled button next to a destructive-looking action."""
    for spec in list_panel_specs():
        assert spec.label.strip()
        assert spec.description.strip()


def test_registry_covers_the_commands_it_replaced():
    """Guards against a panel quietly losing its dashboard route — the command
    that used to post it is gone, so the route is the only way left."""
    assert {spec.key for spec in PANEL_SPECS} == {
        # One key for the two merged panels (/bank post-guide and
        # /bank post-leaderboard, both long gone) since 2026-08-18.
        "economy-panel",
        "economy-shop",       # /bank post-shop
        "economy-bounty",     # /bounty
        "voice-control",      # /voice-admin post-panel
        "guess-prompt",       # /guess prompt
        "ticket-panel",       # /ticket panel
        "grant-audit",        # /grant_audit
    }


# ── where each panel's control is drawn ──────────────────────────────

_PANELS_DIR = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src" / "web_server" / "static" / "js" / "panels"
)
_CALL_RE = re.compile(r"mountPanelPoster\(")
_KEY_LITERAL_RE = re.compile(r"""\s*["'](?P<key>[a-z0-9-]+)["']\s*""")


def _second_argument(text: str, start: int) -> str | None:
    """The second argument of a call, given the index just past its ``(``.

    Deliberately a small scanner rather than a regex. Every call site's first
    argument is itself a call — ``slot("economy-panel")``,
    ``container.querySelector('[data-poster="grant-audit"]')`` — and a regex
    cheap enough to write inline stops at the first quote it meets, which is
    *inside* that first argument. It then reads the slot selector while
    appearing to read the key, so a call site passing the wrong key still
    matches on the right one and the check silently proves nothing.

    Returns None when the second argument isn't a plain string literal, which
    fails the caller loudly — a computed key can't be verified from here.
    """
    depth = 0
    arg_start = start
    args: list[str] = []
    i = start
    while i < len(text):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                args.append(text[arg_start:i])
                break
            depth -= 1
        elif c == "," and depth == 0:
            args.append(text[arg_start:i])
            arg_start = i + 1
        elif c in "\"'`":
            quote, i = c, i + 1
            while i < len(text) and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
        i += 1
    if len(args) < 2:
        return None
    m = _KEY_LITERAL_RE.fullmatch(args[1])
    return m.group("key") if m else None


_APP_JS = _PANELS_DIR.parent / "app.js"
_NAV_MODULE_RE = re.compile(
    r"""id:\s*["'](?P<id>[\w-]+)["'][^}]*?module:\s*["']\./panels/(?P<mod>[\w.-]+)\.js["']"""
)
_LOCAL_IMPORT_RE = re.compile(r"""from\s+["']\./(?P<mod>[\w.-]+)\.js["']""")


def _read(source: pathlib.Path) -> str:
    # Explicit encoding: the panel sources carry emoji and em dashes, and the
    # Windows test runner's default is cp1252, which can't decode them.
    return source.read_text(encoding="utf-8")


def _module_for_page(page_id: str) -> str:
    """The panel module a nav page id mounts, per app.js.

    Page id and filename used to be the same string, so this could be assumed.
    Merged pages broke that: "mod-policy-tickets" mounts panels/policy-tickets.js,
    which composes the queue and settings modules (the other merged pages were
    split back apart 2026-08-29, but the mechanism stays).
    """
    for m in _NAV_MODULE_RE.finditer(_read(_APP_JS)):
        if m.group("id") == page_id:
            return m.group("mod")
    return page_id


def _mounted_keys(page_id: str) -> set[str]:
    """Registry keys the given dashboard page mounts a poster for.

    Follows the page's local imports one level, because a merged page is a thin
    shell that delegates to a report half and a settings half — the poster call
    lives in whichever half owns that feature's settings, not in the shell.
    """
    entry = _module_for_page(page_id)
    source = _PANELS_DIR / f"{entry}.js"
    if not source.exists():
        return set()
    text = _read(source)
    sources = [text]
    for imp in _LOCAL_IMPORT_RE.finditer(text):
        part = _PANELS_DIR / f"{imp.group('mod')}.js"
        if part.exists():
            sources.append(_read(part))
    keys: set[str] = set()
    for chunk in sources:
        found = (_second_argument(chunk, m.end()) for m in _CALL_RE.finditer(chunk))
        keys |= {key for key in found if key}
    return keys


def test_the_call_site_scanner_reads_the_key_not_the_slot():
    """The scanner is the whole basis of the two checks below, and its first
    version read the first argument while looking like it read the second — so
    a mismatched key passed. This is that bug, frozen."""
    mismatched = 'mountPanelPoster(slot("economy-panel"), "economy-shop");'
    assert _second_argument(mismatched, mismatched.index("(") + 1) == "economy-shop"

    selector_form = (
        "mountPanelPoster(container.querySelector('[data-poster=\"a-b\"]'), \"c-d\", {})"
    )
    assert _second_argument(selector_form, selector_form.index("(") + 1) == "c-d"

    # A computed key is unverifiable, so it must not be reported as present.
    computed = "mountPanelPoster(slot(key), key);"
    assert _second_argument(computed, computed.index("(") + 1) is None


@pytest.mark.parametrize("spec", PANEL_SPECS, ids=lambda s: s.key)
def test_every_panel_is_mounted_on_the_page_it_names(spec):
    """``host_page`` is a claim about where an admin finds this control.

    Nothing else checks it now that no page lists every panel: a spec added to
    the registry without a page mounting it is postable only by hand-crafting
    the request, and the button an admin goes looking for is simply absent.
    """
    module = _module_for_page(spec.host_page)
    assert (_PANELS_DIR / f"{module}.js").exists(), (
        f"{spec.key} names host page {spec.host_page!r}, which has no panel module"
    )
    assert spec.key in _mounted_keys(spec.host_page), (
        f"{spec.host_page}.js does not call mountPanelPoster for {spec.key!r}"
    )


def test_no_page_mounts_a_panel_the_registry_never_declared():
    """The other direction: a key renamed in the registry leaves a page calling
    for one that no longer exists, which fails only in the browser."""
    known = {spec.key for spec in PANEL_SPECS}
    for source in _PANELS_DIR.glob("*.js"):
        stray = _mounted_keys(source.stem) - known
        assert not stray, f"{source.name} mounts unregistered panel(s): {sorted(stray)}"


def test_the_retired_channel_panels_page_is_gone():
    """Its seven controls moved onto their features' config pages on
    2026-07-28. A page left behind would post the same panels from a second
    place, which is the sprawl the split was undoing."""
    assert not (_PANELS_DIR / "channel-panels.js").exists()


# ── domain gates that live on the cog, not the route ─────────────────


@pytest.mark.asyncio
async def test_economy_panels_refuse_while_the_economy_is_disabled(monkeypatch):
    """The three economy panels check `enabled` themselves, and *raise*.

    That check used to sit in the /bank post-* command bodies and was briefly
    lost when they became cog methods — posting a currency guide for a currency
    that doesn't exist. It belongs on the cog rather than the route because it
    is a domain rule, not an access rule: it holds however the call arrives.

    It raises rather than returning None because the route reports a bare None
    as "Discord rejected the post", which would send an admin to check bot
    permissions when the actual fix is a toggle on Economy → Settings.
    """
    from unittest.mock import AsyncMock, MagicMock

    from bot_modules.cogs.economy_cog import EconomyCog

    cog = EconomyCog.__new__(EconomyCog)
    cog._require_economy_enabled = AsyncMock(  # type: ignore[method-assign]
        side_effect=ValueError("The economy is disabled for this server — …")
    )
    for panel in ("economy_panel", "shop_panel"):
        setattr(cog, panel, MagicMock(place_or_refresh=AsyncMock()))

    guild = MagicMock(id=1)
    for method in ("post_economy_panel", "post_shop_panel"):
        with pytest.raises(ValueError, match="disabled"):
            await getattr(cog, method)(guild, MagicMock())

    for panel in ("economy_panel", "shop_panel"):
        getattr(cog, panel).place_or_refresh.assert_not_awaited()
