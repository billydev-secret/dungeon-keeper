"""Planning edits to Discord's built-in onboarding.

Editing onboarding replaces the entire prompt list, so the plan an admin
confirms has to be exactly what gets written. That makes this the important
file: everything here is pure, and none of it needs a Discord object.

Background: ``docs/plans/role-autocreate.md``.
"""

from __future__ import annotations

import pytest

from bot_modules.services.onboarding_service import (
    MAX_OPTIONS_PER_PROMPT,
    OptionView,
    PromptView,
    offered_role_ids,
    plan_add_options,
)


def _prompt(pid=1, title="Pick your pings", options=()):
    return PromptView(id=pid, title=title, options=tuple(options))


def _opt(title, *role_ids, description="", emoji=""):
    return OptionView(
        title=title, description=description, emoji=emoji, role_ids=tuple(role_ids)
    )


# ── reading what's already offered ───────────────────────────────────


def test_offered_role_ids_spans_every_prompt():
    prompts = [
        _prompt(1, options=[_opt("A", 10), _opt("B", 11, 12)]),
        _prompt(2, options=[_opt("C", 13)]),
    ]
    assert offered_role_ids(prompts) == {10, 11, 12, 13}


def test_offered_role_ids_of_nothing_is_empty():
    assert offered_role_ids([]) == set()


# ── the destination ──────────────────────────────────────────────────


def test_adds_to_an_existing_prompt_leaving_its_other_options_alone():
    prompts = [_prompt(1, options=[_opt("Existing", 10)])]
    plan = plan_add_options(prompts, [_opt("QOTD", 20)], target_prompt_id=1)

    assert plan.ok
    assert plan.added == ("QOTD",)
    assert [o.title for o in plan.prompts[0].options] == ["Existing", "QOTD"]
    assert plan.prompts[0].options[0].role_ids == (10,)


def test_untargeted_prompts_are_passed_through_untouched():
    """The whole list is rewritten on save, so anything we don't mean to change
    has to survive byte-for-byte."""
    other = _prompt(2, title="Pick your colours", options=[_opt("Red", 99)])
    prompts = [_prompt(1, options=[_opt("Existing", 10)]), other]
    plan = plan_add_options(prompts, [_opt("QOTD", 20)], target_prompt_id=1)

    assert plan.prompts[1] == other


def test_new_prompt_is_appended_and_is_optional_multi_select():
    """Opt-in pings: several may apply, and none may block joining."""
    plan = plan_add_options([], [_opt("QOTD", 20)], new_prompt_title="Get pinged for…")

    assert plan.ok
    assert len(plan.prompts) == 1
    made = plan.prompts[0]
    assert made.title == "Get pinged for…"
    assert made.type == "multiple_choice"
    assert made.single_select is False
    assert made.required is False
    assert made.in_onboarding is True


@pytest.mark.parametrize(
    "target, title",
    [
        pytest.param(None, "", id="neither"),
        pytest.param(1, "A new one", id="both"),
    ],
)
def test_destination_must_be_exactly_one_of_the_two(target, title):
    plan = plan_add_options(
        [_prompt(1)], [_opt("QOTD", 20)],
        target_prompt_id=target, new_prompt_title=title,
    )
    assert not plan.ok


def test_a_target_that_vanished_is_an_error_not_a_silent_new_prompt():
    """Somebody edited onboarding in Server Settings between load and save.
    Guessing a destination would write into the wrong question."""
    plan = plan_add_options([_prompt(1)], [_opt("QOTD", 20)], target_prompt_id=777)

    assert not plan.ok
    assert "no longer exists" in plan.errors[0]
    assert plan.prompts == (_prompt(1),), "the prompt list must be untouched"


# ── not duplicating a role ───────────────────────────────────────────


def test_a_role_already_offered_is_skipped_not_duplicated():
    prompts = [_prompt(1, options=[_opt("Already there", 20)])]
    plan = plan_add_options(prompts, [_opt("QOTD", 20)], target_prompt_id=1)

    assert plan.ok
    assert plan.added == ()
    assert plan.skipped == (("QOTD", "already offered in onboarding"),)
    assert not plan.changes_anything
    assert len(plan.prompts[0].options) == 1


def test_a_role_offered_in_a_different_prompt_still_counts_as_offered():
    prompts = [_prompt(1), _prompt(2, title="Elsewhere", options=[_opt("X", 20)])]
    plan = plan_add_options(prompts, [_opt("QOTD", 20)], target_prompt_id=1)

    assert plan.added == ()
    assert not plan.changes_anything


def test_two_additions_carrying_the_same_role_only_add_once():
    plan = plan_add_options(
        [], [_opt("QOTD", 20), _opt("QOTD again", 20)],
        new_prompt_title="Pings",
    )
    assert plan.added == ("QOTD",)


def test_nothing_to_add_is_not_an_error():
    """"Everything is already offered" is a fine outcome; the caller just
    shouldn't write."""
    prompts = [_prompt(1, options=[_opt("There", 20)])]
    plan = plan_add_options(prompts, [_opt("QOTD", 20)], target_prompt_id=1)

    assert plan.ok
    assert not plan.changes_anything


# ── Discord's limits, checked before Discord has to ──────────────────


def test_overflowing_a_prompt_is_refused_with_the_numbers_in_it():
    full = _prompt(1, options=[_opt(f"opt{i}", 100 + i) for i in range(MAX_OPTIONS_PER_PROMPT)])
    plan = plan_add_options([full], [_opt("QOTD", 20)], target_prompt_id=1)

    assert not plan.ok
    assert str(MAX_OPTIONS_PER_PROMPT) in plan.errors[0]
    assert plan.prompts == (full,)


@pytest.mark.parametrize(
    "option, fragment",
    [
        pytest.param(_opt(""), "needs a title", id="no-title"),
        pytest.param(_opt("x" * 51, 20), "option title", id="title-too-long"),
        pytest.param(
            _opt("QOTD", 20, description="y" * 101), "description", id="desc-too-long"
        ),
        pytest.param(_opt("QOTD"), "would grant nothing", id="grants-nothing"),
    ],
)
def test_unusable_options_are_refused_in_words(option, fragment):
    plan = plan_add_options([_prompt(1)], [option], target_prompt_id=1)
    assert not plan.ok
    assert any(fragment in e for e in plan.errors)


def test_an_empty_selection_is_refused():
    assert not plan_add_options([_prompt(1)], [], target_prompt_id=1).ok


def test_a_refused_plan_never_half_applies():
    """One bad option must not leave the good ones staged for writing."""
    prompts = [_prompt(1, options=[_opt("Existing", 10)])]
    plan = plan_add_options(
        prompts, [_opt("Good", 20), _opt("")], target_prompt_id=1
    )
    assert not plan.ok
    assert plan.prompts == tuple(prompts)
