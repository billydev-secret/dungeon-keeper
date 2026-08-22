"""Tests for the shared feature-role provisioner.

``choose_role_action`` is the whole decision and has no Discord in it, so most
of this file is a table over it. ``ensure_feature_role`` is exercised against a
hand-rolled fake guild rather than Discord mocks — what matters is which of
use/adopt/create/recreate it took, whether it persisted the id, and that it
never raises at a caller who is mid-interaction.

Background: ``docs/plans/role-autocreate.md``.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.core.role_provision import (
    RoleSpec,
    choose_role_action,
    ensure_feature_role,
    recreate_notice,
    role_dial_opted_out,
)


# ── fakes ────────────────────────────────────────────────────────────


class FakeRole:
    def __init__(self, role_id: int, name: str):
        self.id = role_id
        self.name = name


class FakeGuild:
    """Just enough guild: an ordered role list and a create that can fail."""

    def __init__(self, roles=(), *, create_raises: Exception | None = None):
        self.id = 999
        self.roles = list(roles)
        self.created: list[dict] = []
        self._create_raises = create_raises
        self._next_id = 5000

    def get_role(self, role_id: int):
        return next((r for r in self.roles if r.id == role_id), None)

    async def create_role(self, **kwargs):
        if self._create_raises is not None:
            raise self._create_raises
        self.created.append(kwargs)
        self._next_id += 1
        role = FakeRole(self._next_id, kwargs["name"])
        self.roles.append(role)
        return role


def _response():
    """A minimal response object for discord.py's HTTP exception constructors."""
    resp = type("R", (), {"status": 403, "reason": "Forbidden"})()
    return resp


# ── choose_role_action ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "stored_id, stored_resolves, named, expected",
    [
        # The steady state: stored id still resolves. No writes, no API calls.
        pytest.param(10, True, [10], ("use", 10), id="stored-resolves"),
        # Stored id resolving wins even when a same-named role also exists —
        # the admin's repoint is not second-guessed.
        pytest.param(10, True, [11], ("use", 10), id="stored-beats-name"),
        # Never configured, but the guild already has the role: adopt it.
        # This is the case that stops a fresh install twinning TGM's @Jailed.
        pytest.param(0, False, [11], ("adopt", 11), id="unset-adopts-by-name"),
        # Configured long ago, id now stale, but the named role is there —
        # e.g. the role was recreated by hand. Adopt rather than make a third.
        pytest.param(10, False, [11], ("adopt", 11), id="stale-adopts-by-name"),
        # Nothing stored, nothing named: first run of a feature. Silent create.
        pytest.param(0, False, [], ("create", None), id="unset-creates"),
        # Something WAS stored and now resolves to nothing: an admin deleted
        # the role. Same create, but the mods get told.
        pytest.param(10, False, [], ("recreate", None), id="stale-recreates"),
        # An empty guild still creates rather than exploding.
        pytest.param(0, False, [], ("create", None), id="empty-guild"),
        # Duplicate names: the first in guild order (lowest position) wins, and
        # keeps winning — an arbitrary pick would adopt a different role each run.
        pytest.param(0, False, [11, 12], ("adopt", 11), id="duplicate-names"),
    ],
)
def test_choose_role_action(stored_id, stored_resolves, named, expected):
    assert choose_role_action(stored_id, stored_resolves, named) == expected


def test_choose_role_action_is_deterministic_across_calls():
    """Same inputs, same answer — the duplicate-name tie-break must not drift."""
    first = choose_role_action(0, False, [11, 12])
    for _ in range(5):
        assert choose_role_action(0, False, [11, 12]) == first


# ── ensure_feature_role ──────────────────────────────────────────────


SPEC = RoleSpec(name="Jailed", reason="test")


async def _ensure(guild, stored, **kw):
    """Run the helper over a one-slot store, returning (role, stored_id)."""
    box = {"id": stored}
    role = await ensure_feature_role(
        guild,
        kw.pop("spec", SPEC),
        load=lambda: box["id"],
        store=lambda rid: box.__setitem__("id", rid),
        **kw,
    )
    return role, box["id"]


@pytest.mark.asyncio
async def test_use_makes_no_api_call_and_no_write():
    guild = FakeGuild([FakeRole(10, "Jailed")])
    role, stored = await _ensure(guild, 10)
    assert role is not None and role.id == 10
    assert guild.created == []
    assert stored == 10


@pytest.mark.asyncio
async def test_adopt_by_exact_name_stores_the_id():
    guild = FakeGuild([FakeRole(11, "Jailed")])
    role, stored = await _ensure(guild, 0)
    assert role is not None and role.id == 11
    assert guild.created == [], "adopting must not also create a twin"
    assert stored == 11, "the adopted id has to be persisted or we re-adopt forever"


@pytest.mark.asyncio
async def test_adoption_is_case_sensitive():
    """Decision 5: exact match only. @jailed is somebody else's role."""
    guild = FakeGuild([FakeRole(11, "jailed")])
    role, stored = await _ensure(guild, 0)
    assert role is not None and role.id != 11
    assert len(guild.created) == 1
    assert stored == role.id


@pytest.mark.asyncio
async def test_created_role_has_no_permissions_by_default():
    """A feature role earns permissions from channel overwrites, not itself."""
    guild = FakeGuild()
    role, _ = await _ensure(guild, 0)
    assert role is not None
    assert guild.created[0]["permissions"] == discord.Permissions.none()


@pytest.mark.asyncio
async def test_spec_fields_reach_create_role():
    guild = FakeGuild()
    spec = RoleSpec(
        name="Ghost",
        reason="season setup",
        colour=discord.Colour(0x00FF00),
        hoist=True,
        mentionable=True,
    )
    await _ensure(guild, 0, spec=spec)
    made = guild.created[0]
    assert made["name"] == "Ghost"
    assert made["reason"] == "season setup"
    assert made["colour"] == discord.Colour(0x00FF00)
    assert made["hoist"] is True
    assert made["mentionable"] is True


@pytest.mark.asyncio
async def test_first_create_is_silent():
    guild = FakeGuild()
    said: list[str] = []

    async def announce(msg):
        said.append(msg)

    await _ensure(guild, 0, announce=announce)
    assert said == [], "a first create is not news"


@pytest.mark.asyncio
async def test_recreate_announces_that_members_were_lost():
    """Decision 6: a stale stored id means an admin deleted the role."""
    guild = FakeGuild()
    said: list[str] = []

    async def announce(msg):
        said.append(msg)

    role, stored = await _ensure(guild, 10, announce=announce, feature="Jail")
    assert role is not None and stored == role.id
    assert len(said) == 1
    assert "Jailed" in said[0] and "Jail" in said[0]
    assert "no longer" in said[0], "the mods need to know the members are gone"


@pytest.mark.asyncio
async def test_announce_failure_never_costs_the_role():
    """Telling the mods is best-effort; the feature still gets its role."""
    guild = FakeGuild()

    async def announce(_msg):
        raise RuntimeError("mod channel is on fire")

    role, stored = await _ensure(guild, 10, announce=announce)
    assert role is not None
    assert stored == role.id


@pytest.mark.asyncio
async def test_missing_manage_roles_returns_none_without_raising():
    guild = FakeGuild(create_raises=discord.Forbidden(_response(), "nope"))
    role, stored = await _ensure(guild, 0)
    assert role is None
    assert stored == 0, "a failed create must not persist anything"


@pytest.mark.asyncio
async def test_transient_http_error_returns_none_without_raising():
    """The ensure_dm_roles gap: a rate limit used to escape into a click."""
    guild = FakeGuild(create_raises=discord.HTTPException(_response(), "429"))
    role, stored = await _ensure(guild, 0)
    assert role is None
    assert stored == 0


@pytest.mark.asyncio
async def test_async_load_and_store_are_awaited():
    """Jail and Inactive read config on a thread; the helper must allow it."""
    guild = FakeGuild()
    box = {"id": 0}

    async def load():
        return box["id"]

    async def store(rid):
        box["id"] = rid

    role = await ensure_feature_role(guild, SPEC, load=load, store=store)
    assert role is not None
    assert box["id"] == role.id


def test_recreate_notice_names_the_role_and_feature():
    text = recreate_notice("Inactive", "the inactive sweep")
    assert "Inactive" in text
    assert "the inactive sweep" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored, existing, should_fire",
    [
        # A brand-new role needs its first-time setup.
        pytest.param(0, [], True, id="create-fires"),
        # So does one remade after a deletion — the new role has no overwrites.
        pytest.param(10, [], True, id="recreate-fires"),
        # But an adopted role is one the guild already configured. Re-running a
        # deny-view-everywhere sweep over it would be a destructive surprise.
        pytest.param(0, [FakeRole(11, "Jailed")], False, id="adopt-does-not-fire"),
        # And the steady state must cost nothing at all.
        pytest.param(10, [FakeRole(10, "Jailed")], False, id="use-does-not-fire"),
    ],
)
async def test_on_create_fires_only_for_a_genuinely_new_role(
    stored, existing, should_fire
):
    guild = FakeGuild(existing)
    seen: list[int] = []

    async def on_create(role):
        seen.append(role.id)

    role, _ = await _ensure(guild, stored, on_create=on_create)
    assert role is not None
    assert bool(seen) is should_fire
    if should_fire:
        assert seen == [role.id]


# ── the opted-out rule (Stage 2) ─────────────────────────────────────


@pytest.mark.parametrize(
    "stored_id, stored_resolves, named",
    [
        # Opting out beats every other signal, including a stored live role,
        # a same-named role sitting there, and a stale id that would otherwise
        # be read as a deletion worth announcing.
        pytest.param(10, True, [10], id="over-a-live-stored-role"),
        pytest.param(0, False, [11], id="over-an-adoptable-name"),
        pytest.param(10, False, [], id="over-a-would-be-recreate"),
        pytest.param(0, False, [], id="over-a-would-be-create"),
    ],
)
def test_opted_out_always_skips(stored_id, stored_resolves, named):
    assert choose_role_action(
        stored_id, stored_resolves, named, opted_out=True
    ) == ("skip", None)


@pytest.mark.asyncio
async def test_opted_out_creates_nothing_and_writes_nothing():
    """An admin who picked \"(none)\" must not get a role made for them."""
    guild = FakeGuild([FakeRole(11, "Jailed")])
    role, stored = await _ensure(guild, 0, opted_out=True)
    assert role is None
    assert guild.created == []
    assert stored == 0


def test_role_dial_opted_out_distinguishes_never_set_from_chosen_none():
    """The distinction Stage 2 rests on: no row vs a row holding \"0\"."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE config (guild_id INTEGER, key TEXT, value TEXT)"
    )
    conn.execute(
        "INSERT INTO config VALUES (?, ?, ?)", (7, "chose_none_role_id", "0")
    )
    conn.execute(
        "INSERT INTO config VALUES (?, ?, ?)", (7, "chose_role_id", "123")
    )

    # Never touched: ours to provision.
    assert role_dial_opted_out(conn, "never_set_role_id", 7) is False
    # Explicitly "(none)": hands off.
    assert role_dial_opted_out(conn, "chose_none_role_id", 7) is True
    # A real choice is not an opt-out.
    assert role_dial_opted_out(conn, "chose_role_id", 7) is False


def test_role_dial_opted_out_follows_the_legacy_fallback():
    """A guild inheriting a home-guild \"(none)\" is opted out, not unconfigured.

    ``get_config_value`` falls back to guild 0, so reading only the guild's own
    row would provision a role the admin had already declined.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE config (guild_id INTEGER, key TEXT, value TEXT)"
    )
    conn.execute("INSERT INTO config VALUES (?, ?, ?)", (0, "ping_role_id", "0"))

    assert role_dial_opted_out(conn, "ping_role_id", 7) is True
    # ...and a guild that opts out of the fallback is unconfigured again.
    assert (
        role_dial_opted_out(conn, "ping_role_id", 7, allow_legacy_fallback=False)
        is False
    )


@pytest.mark.parametrize(
    "stored_is_own, expected",
    [
        # This guild configured a role and it's gone: an admin deleted it, and
        # the members who held it are gone with it. Say so.
        pytest.param(True, ("recreate", None), id="own-row-is-a-deletion"),
        # The id came from the legacy guild_id=0 row, so it names a role in a
        # DIFFERENT guild and was never going to resolve here. Announcing a
        # deletion would be a lie told to every guild inheriting the row.
        pytest.param(False, ("create", None), id="inherited-id-is-a-first-run"),
    ],
)
def test_an_inherited_id_is_not_a_deletion(stored_is_own, expected):
    assert choose_role_action(
        1470278713504694302, False, [], stored_is_own=stored_is_own
    ) == expected


@pytest.mark.asyncio
async def test_inherited_id_creates_without_announcing():
    guild = FakeGuild()
    said: list[str] = []

    async def announce(msg):
        said.append(msg)

    role = await ensure_feature_role(
        guild, SPEC,
        load=lambda: 999_999,          # a real id, but from another guild
        store=lambda _rid: None,
        announce=announce,
        stored_is_own=False,
    )
    assert role is not None
    assert said == [], "no deletion happened, so the mods hear nothing"


@pytest.mark.asyncio
async def test_respect_opt_out_false_provisions_over_a_stored_zero():
    """For a dial where 0 records a save rather than a decision.

    ``ensure_feature_role`` is told ``opted_out=False`` by its caller in that
    case, so the stored 0 reads as "nothing here yet" and a role gets made.
    """
    guild = FakeGuild()
    role, stored = await _ensure(guild, 0, opted_out=False)

    assert role is not None
    assert stored == role.id
