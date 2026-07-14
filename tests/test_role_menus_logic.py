"""Tests for the role-menus pure mode engine (logic.py)."""

from __future__ import annotations

from bot_modules.role_menus.logic import (
    ERR_AT_CAP,
    ERR_NO_CHANGE,
    ERR_PERMANENT,
    CooldownGate,
    resolve_click,
    resolve_selection,
)

MENU = [11, 22, 33, 44]


# ── clicks: toggle ──────────────────────────────────────────────────

def test_toggle_click_grants_when_missing():
    out = resolve_click("toggle", MENU, set(), 11, 0, None)
    assert out.adds == (11,) and out.removes == () and not out.error


def test_toggle_click_removes_when_held():
    out = resolve_click("toggle", MENU, {11}, 11, 0, None)
    assert out.removes == (11,) and out.adds == ()


def test_toggle_cap_blocks_new_grant():
    out = resolve_click("toggle", MENU, {11, 22}, 33, 2, None)
    assert out.error == ERR_AT_CAP


def test_toggle_cap_never_blocks_removal():
    # Holding 3 with a cap of 2 (cap was lowered) — removal still works.
    out = resolve_click("toggle", MENU, {11, 22, 33}, 33, 2, None)
    assert out.removes == (33,) and not out.error


def test_toggle_ignores_roles_outside_menu_for_cap():
    # Held roles not in the menu don't count toward the cap.
    out = resolve_click("toggle", MENU, {99, 88}, 11, 1, None)
    assert out.adds == (11,) and not out.error


# ── clicks: unique ──────────────────────────────────────────────────

def test_unique_click_swaps_out_other_roles():
    out = resolve_click("unique", MENU, {22, 33}, 11, 0, None)
    assert out.adds == (11,)
    assert out.removes == (22, 33)  # menu order


def test_unique_click_on_held_role_removes_it():
    out = resolve_click("unique", MENU, {22}, 22, 0, None)
    assert out.removes == (22,) and out.adds == ()


# ── clicks: verify / drop ───────────────────────────────────────────

def test_verify_click_grants_once():
    assert resolve_click("verify", MENU, set(), 11, 0, None).adds == (11,)
    assert resolve_click("verify", MENU, {11}, 11, 0, None).error == ERR_NO_CHANGE


def test_drop_click_removes_only():
    assert resolve_click("drop", MENU, {11}, 11, 0, None).removes == (11,)
    assert resolve_click("drop", MENU, set(), 11, 0, None).error == ERR_NO_CHANGE


# ── clicks: binding ─────────────────────────────────────────────────

def test_binding_first_pick_binds_and_grants():
    out = resolve_click("binding", MENU, set(), 11, 0, None)
    assert out.adds == (11,) and out.bind_role_id == 11


def test_binding_second_pick_is_permanent():
    out = resolve_click("binding", MENU, {11}, 22, 0, 11)
    assert out.error == ERR_PERMANENT


def test_binding_pick_of_already_held_role_binds_without_add():
    # Role was hand-granted earlier; the menu click just records the pick.
    out = resolve_click("binding", MENU, {11}, 11, 0, None)
    assert out.adds == () and out.bind_role_id == 11


# ── selections (dropdown submissions) ───────────────────────────────

def test_selection_becomes_the_set():
    out = resolve_selection("toggle", MENU, {11, 22}, [22, 33], 0, None)
    assert out.adds == (33,) and out.removes == (11,)


def test_selection_empty_clears_menu_roles():
    out = resolve_selection("toggle", MENU, {11, 22, 99}, [], 0, None)
    assert out.removes == (11, 22) and out.adds == ()  # 99 isn't menu-owned


def test_selection_ignores_stale_role_ids():
    # Role 55 was removed from the menu mid-interaction (spec §5).
    out = resolve_selection("toggle", MENU, set(), [55, 11], 0, None)
    assert out.adds == (11,)


def test_selection_no_change_reports():
    out = resolve_selection("toggle", MENU, {11}, [11], 0, None)
    assert out.error == ERR_NO_CHANGE


def test_selection_cap_blocks_growth():
    out = resolve_selection("toggle", MENU, {11}, [11, 22, 33], 2, None)
    assert out.error == ERR_AT_CAP


def test_selection_cap_allows_swap_at_over_cap():
    # Grandfathered holder of 3 with cap 2: a swap keeps the total flat.
    out = resolve_selection("toggle", MENU, {11, 22, 33}, [11, 22, 44], 2, None)
    assert out.adds == (44,) and out.removes == (33,) and not out.error


def test_selection_unique_multi_degrades_to_first_menu_order():
    out = resolve_selection("unique", MENU, set(), [33, 22], 0, None)
    assert out.adds in ((22,), (33,)) and len(out.adds) == 1
    assert out.adds == (22,)  # menu order wins


def test_selection_verify_only_adds():
    out = resolve_selection("verify", MENU, {11}, [22], 0, None)
    assert out.adds == (22,) and out.removes == ()
    assert resolve_selection("verify", MENU, {11}, [11], 0, None).error == ERR_NO_CHANGE


def test_selection_drop_only_removes():
    out = resolve_selection("drop", MENU, {11, 22}, [11], 0, None)
    assert out.removes == (11,) and out.adds == ()
    assert resolve_selection("drop", MENU, set(), [11], 0, None).error == ERR_NO_CHANGE


def test_selection_binding_single_pick():
    out = resolve_selection("binding", MENU, set(), [22], 0, None)
    assert out.adds == (22,) and out.bind_role_id == 22
    assert resolve_selection("binding", MENU, set(), [22], 0, 11).error == ERR_PERMANENT
    assert resolve_selection("binding", MENU, set(), [], 0, None).error == ERR_NO_CHANGE


# ── cooldown gate ───────────────────────────────────────────────────

def test_cooldown_gate_blocks_within_window_and_recovers():
    gate = CooldownGate()
    assert gate.check(1, 100, 10, now=1000.0) == 0.0
    wait = gate.check(1, 100, 10, now=1004.0)
    assert wait == 6.0
    assert gate.check(1, 100, 10, now=1011.0) == 0.0


def test_cooldown_gate_is_per_menu_and_member():
    gate = CooldownGate()
    assert gate.check(1, 100, 10, now=1000.0) == 0.0
    assert gate.check(2, 100, 10, now=1000.0) == 0.0  # other menu
    assert gate.check(1, 200, 10, now=1000.0) == 0.0  # other member


def test_cooldown_gate_zero_always_allows():
    gate = CooldownGate()
    assert gate.check(1, 100, 0, now=1000.0) == 0.0
    assert gate.check(1, 100, 0, now=1000.0) == 0.0
