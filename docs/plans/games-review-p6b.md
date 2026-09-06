# Games deep review — P6b, the deferred half of package P6 (2026-09-05)

The one piece of `docs/reviews/2026-09-02-games-deep-review.md` that was never
built. Everything else in that review's queue — P0 as a script, P1 through P9,
and two rounds of pre-merge fixes — is on main as of merge `ca1f4001`.

P6 shipped the Whisper and Risky Rolls halves of the rotation-rooms package and
stopped there, because two live branches owned the files the rest needed:
`review-fix-queue-round-2` owns the Guess Who modules, and `game-start-echo`
owns `cogs/risky_roll_cog.py`. **Check both have merged before starting.** If a
file below shows unexpected recent changes, keep the edits surgical and additive.

## The rule that governs the whole package

Guess Who and Whisper are the two healthiest loops in the bot — 63 rounds and
429 guesses in thirty days, with no host at the keyboard. **Do not change their
core mechanics.** The three guesses, the one reply, the consent and opt-out
flow, and the no-contact gating were all verified sound by the review. Nothing
here touches them.

Anything that could act on an existing prod round **ships dark**: the new
lifetime dial defaults to 0 (never), so the 21 stale rounds sitting in prod do
not all resolve the moment the bot restarts. Billy flips it when he wants it.

A **new per-user table is not allowed** — the data-register decision is still
open. A new column on an already-registered table is fine with a
`docs/data_register.md` note.

## Guess Who — five items

**A round-lifetime dial** (finding rotation-rooms-158). Days, on the Guess Who
panel, default 0 meaning never, enforced by the nudge/sweep loop that already
runs. At expiry the card is edited to "Nobody got it — it was {name}" with the
original spoilered, `solved_at` is marked with a NULL `solver_id`, and the
original is deleted then. A submitter-only **Reveal now** button on their own
card does the same thing early. The no-contact reveal rule still applies: a
pair-holder sees `User <id>`, and that degrade is deliberate — see
`docs/no_contact_spec.md` and the memory note on embed names. Tests at the
repo/logic layer for expiry selection and the resolve write, and the dial pinned
in the guess equivalent of `tests/web/test_game_dials_are_enforced.py`.

**A departing member's rounds** (rotation-rooms-164). An `on_member_remove`
listener runs the same path the opt-out flag does — `_do_flag_user_open_rounds_optout`
plus `_do_withdraw_consent` — and deletes the round's original immediately. The
nudge skips rounds whose submitter has left. Repo-layer test with a departed
answer.

**Self-exclusion from the picker** (rotation-rooms-170). Exclude
`interaction.user.id` from the candidate set; `candidate_members_for` already
takes an exclusion set.

**A legacy consent backfill** (rotation-rooms-171). On the first run after a
restart, write a version-0 consent row — "legacy role holder, disclosure not
shown" — for every current role holder lacking one, once, so the gap is on the
record rather than invisible. Note it in the data register's guess consent row
and in the spec, and test the idempotence at the repo layer.

**One hint correction** (rotation-rooms-161, guess half). The `(none)` hint on
the required-role dial must say it turns submitting off.

## Risky Rolls — one wiring item

P6 shipped the payoff chaser in `services/risky_roll/views.py` as a lazily
started loop (`ensure_payoff_chaser` / `stop_payoff_chaser`), because the cog
belonged to a live branch. It therefore only starts when the room next sees
traffic. Call `ensure_payoff_chaser(self.bot)` at the end of
`RiskyRollCog.cog_load` so prompts restored across a restart are chased without
waiting for the first roll, and `stop_payoff_chaser()` in `cog_unload`. One
wiring assertion each (rotation-rooms-156, cog half).

## Where things are

Specs `docs/guess_spec.md`, `docs/whisper_spec.md` and `docs/risky_roll_spec.md`
are all Reference, and `manual.html` has a section per room. Tests live in
`tests/unit/test_guess_repo.py`, `tests/components/test_guess_models.py`,
`tests/cogs/test_guess_*.py`, `tests/test_guess_*.py` and
`tests/cogs/test_risky_roll_cog.py`.
