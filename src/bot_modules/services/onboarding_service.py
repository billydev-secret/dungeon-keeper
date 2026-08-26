"""Planning for edits to Discord's built-in onboarding ("Channels & Roles").

Onboarding is where members actually pick roles up, which makes it the natural
home for the opt-in roles in :mod:`bot_modules.services.feature_roles` — a ping
role nobody can take is furniture. DK already *reads* this config hourly
(``economy_loop._sync_setup_marks`` marks the ``role_pick`` quest from it); this
module is the other half.

**Editing onboarding is replace-the-world, and that shapes everything here.**
``Guild.edit_onboarding(prompts=...)`` overrides the entire prompt list — there
is no "append one option" call — so every edit is a read-modify-write of a
member-facing config a human also edits by hand. Two consequences:

* The plan is computed as **pure data** (:func:`plan_add_options`), so what will
  be written can be shown to an admin and confirmed before anything happens. No
  background sync writes this, ever: DK racing a hand edit in Server Settings
  would clobber it wholesale, which is the same two-writers failure that cost 78
  members their DM-mode picks when MEE6 wrote the same roles.
* Everything read must survive the round-trip. discord.py's ``to_dict`` keeps
  title, description, emoji, roles and channels, but **discards prompt ids**
  (it renumbers by list index) and never sends option ids — so Discord treats
  any write as "replace these prompts". The visible configuration is preserved;
  the ids beneath it are not. Nothing here may depend on an id surviving.

Unset fields are genuinely omitted from the request, so a prompts-only write
leaves ``enabled``, ``mode`` and the default channels alone. We rely on that
rather than echoing values back.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence

#: Discord's cap on options in one prompt.
MAX_OPTIONS_PER_PROMPT = 50
#: Discord's field caps. Exceeding one is a 400 with an opaque message, so we
#: check first and say which field in words an admin can act on.
MAX_OPTION_TITLE = 50
MAX_OPTION_DESCRIPTION = 100
MAX_PROMPT_TITLE = 100


@dataclass(frozen=True)
class OptionView:
    """One choice inside a prompt, in the shape both directions understand."""

    title: str
    description: str = ""
    emoji: str = ""
    role_ids: tuple[int, ...] = ()
    channel_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class PromptView:
    """One onboarding question.

    ``id`` is carried for display and for naming a target, never for writing —
    see the module docstring on why it cannot survive a write.
    """

    id: int
    title: str
    type: str = "multiple_choice"
    single_select: bool = False
    required: bool = False
    in_onboarding: bool = True
    options: tuple[OptionView, ...] = ()


@dataclass(frozen=True)
class Plan:
    """What a write would do, in a form an admin can be shown before it happens."""

    prompts: tuple[PromptView, ...]
    added: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()   # (title, why)
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def changes_anything(self) -> bool:
        return bool(self.added)


def offered_role_ids(prompts: Iterable[PromptView]) -> set[int]:
    """Every role id onboarding currently hands out, across all prompts.

    The same set ``economy_loop._sync_setup_marks`` derives to decide who has
    done the role-pick quest.
    """
    found: set[int] = set()
    for prompt in prompts:
        for option in prompt.options:
            found.update(option.role_ids)
    return found


def _option_errors(option: OptionView) -> list[str]:
    problems: list[str] = []
    if not option.title.strip():
        problems.append("an option needs a title")
    if len(option.title) > MAX_OPTION_TITLE:
        problems.append(
            f"“{option.title[:20]}…” is longer than Discord allows for an "
            f"option title ({MAX_OPTION_TITLE} characters)"
        )
    if len(option.description) > MAX_OPTION_DESCRIPTION:
        problems.append(
            f"the description for “{option.title}” is longer than Discord "
            f"allows ({MAX_OPTION_DESCRIPTION} characters)"
        )
    if not option.role_ids and not option.channel_ids:
        problems.append(f"“{option.title}” would grant nothing")
    return problems


def plan_add_options(
    prompts: Sequence[PromptView],
    additions: Sequence[OptionView],
    *,
    target_prompt_id: int | None = None,
    new_prompt_title: str = "",
) -> Plan:
    """Work out the prompt list that adding ``additions`` would produce.

    Exactly one destination: an existing prompt by ``target_prompt_id``, or a
    new prompt named ``new_prompt_title`` appended at the end.

    An addition whose role is **already offered anywhere in onboarding** is
    skipped rather than duplicated — two places to pick up one role reads as a
    bug to a member, and the hourly quest sweep counts a role once however many
    options carry it.

    Never raises and never partially applies: an unusable request comes back as
    ``errors`` with an unchanged prompt list, so the caller can show the reason
    without having touched Discord.
    """
    prompts = tuple(prompts)
    errors: list[str] = []

    if bool(target_prompt_id) == bool(new_prompt_title.strip()):
        errors.append(
            "pick one destination: an existing question, or a name for a new one"
        )
    if new_prompt_title and len(new_prompt_title) > MAX_PROMPT_TITLE:
        errors.append(
            f"a question title can be at most {MAX_PROMPT_TITLE} characters"
        )
    if not additions:
        errors.append("nothing selected to add")

    target_index: int | None = None
    if target_prompt_id:
        for i, prompt in enumerate(prompts):
            if prompt.id == target_prompt_id:
                target_index = i
                break
        if target_index is None:
            errors.append(
                "that question no longer exists — someone changed onboarding "
                "in Server Settings. Reload and try again."
            )

    already = offered_role_ids(prompts)
    keep: list[OptionView] = []
    added: list[str] = []
    skipped: list[tuple[str, str]] = []
    for option in additions:
        errors.extend(_option_errors(option))
        if option.role_ids and already.issuperset(option.role_ids):
            skipped.append((option.title, "already offered in onboarding"))
            continue
        keep.append(option)
        added.append(option.title)
        already.update(option.role_ids)

    if errors:
        return Plan(prompts=prompts, errors=tuple(errors), skipped=tuple(skipped))

    if not keep:
        # Everything was already there. Not an error — just nothing to write.
        return Plan(prompts=prompts, skipped=tuple(skipped))

    if target_index is not None:
        target = prompts[target_index]
        merged = target.options + tuple(keep)
        if len(merged) > MAX_OPTIONS_PER_PROMPT:
            return Plan(
                prompts=prompts,
                skipped=tuple(skipped),
                errors=(
                    f"“{target.title}” would have {len(merged)} choices; "
                    f"Discord allows {MAX_OPTIONS_PER_PROMPT}.",
                ),
            )
        new_prompts = (
            prompts[:target_index]
            + (replace(target, options=merged),)
            + prompts[target_index + 1:]
        )
    else:
        new_prompts = prompts + (
            PromptView(
                id=0,
                title=new_prompt_title.strip(),
                # Opt-in pings: many may apply, and none is required to join.
                type="multiple_choice",
                single_select=False,
                required=False,
                in_onboarding=True,
                options=tuple(keep),
            ),
        )

    return Plan(
        prompts=new_prompts,
        added=tuple(added),
        skipped=tuple(skipped),
    )


# ── translating to and from discord.py ───────────────────────────────


def read_prompts(onboarding: Any) -> tuple[PromptView, ...]:
    """Snapshot a live ``discord.Onboarding`` into plain data."""
    out: list[PromptView] = []
    for prompt in onboarding.prompts:
        options = tuple(
            OptionView(
                title=option.title,
                description=option.description or "",
                emoji=str(option.emoji) if option.emoji else "",
                role_ids=tuple(option.role_ids),
                channel_ids=tuple(option.channel_ids),
            )
            for option in prompt.options
        )
        out.append(
            PromptView(
                id=prompt.id,
                title=prompt.title,
                type=getattr(prompt.type, "name", str(prompt.type)),
                single_select=prompt.single_select,
                required=prompt.required,
                in_onboarding=prompt.in_onboarding,
                options=options,
            )
        )
    return tuple(out)


def to_discord_prompts(prompts: Sequence[PromptView], discord_mod: Any) -> list[Any]:
    """Build the ``OnboardingPrompt`` list for ``Guild.edit_onboarding``.

    ``discord_mod`` is injected so the planning layer and its tests never need
    the library. Prompt ids are deliberately not passed: discord.py renumbers
    them by index regardless, and pretending otherwise would invite code that
    depends on them surviving.
    """
    built = []
    for prompt in prompts:
        options = [
            discord_mod.OnboardingPromptOption(
                title=option.title,
                description=option.description or None,
                emoji=option.emoji or discord_mod.utils.MISSING,
                roles=list(option.role_ids),
                channels=list(option.channel_ids),
            )
            for option in prompt.options
        ]
        built.append(
            discord_mod.OnboardingPrompt(
                type=(
                    discord_mod.OnboardingPromptType.dropdown
                    if prompt.type == "dropdown"
                    else discord_mod.OnboardingPromptType.multiple_choice
                ),
                title=prompt.title,
                options=options,
                single_select=prompt.single_select,
                required=prompt.required,
                in_onboarding=prompt.in_onboarding,
            )
        )
    return built
