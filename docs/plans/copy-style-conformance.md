# Copy style-guide conformance sweep (round 2)

**Status:** COMPLETE — bulk landed on main via `0d3c12c4` 2026-07-21; the
mop-up gaps below (Gap 1, Gap 2, Stage 8, Stage 11) shipped in the same
session.

The 2026-07-21 embed-style-conformance sweep covered color. The style guide
was then grown from a color/embed guide into a full **user-facing copy**
guide (`docs/embed_style_guide.md`, merged `1bc6350`) — Title Case rulings,
`❌` error prefixes, "server" not "guild", separators, ellipsis, progress-bar
vocabulary, slash-command/button/modal conventions, dashboard toast wording.

A parallel session ran the bulk of this sweep directly on main as
`0d3c12c4` ("Style: apply the guide rulings — Title Case, ❌ errors, canonical
colors") — six parallel slices covering ~550 error-prefix sites, ~90 Title
Case sites, dashboard buttons/headings (165 replacements/53 panels), color
aliasing, guild→server, `colour=`, Cancel glyphs, footer/pagination
separators. A worktree session (this one) independently audited and started
fixing the same ground before discovering the overlap; those two commits
were discarded (superseded) and this doc restarts clean from main's `HEAD`
with a **gap audit** — what `0d3c12c4` didn't reach.

## Confirmed done by 0d3c12c4 (no further action)
Title Case (games/economy/misc titles+modals+buttons, dashboard
buttons/headings), ❌/✅ prefixes (~550 sites incl. logic/service layers),
no-permission consolidation (`services/replies.py::NO_PERMISSION`), guild→
server in `xp_cog.py`/`guess_cog.py`, color aliasing (`SUCCESS_COLOR`/
`ERROR_COLOR` → `COLOR_GREEN`/`COLOR_RED`), `colour=` kwarg, ✕ Cancel →
Cancel, footer separator double-space fix, Voice Master middot→em-dash,
bios pagination wording, dashboard ASCII ellipsis (spot-checked — none
remain outside legitimate JS spread syntax).

## Remaining gaps (this doc's actual scope)

### Gap 1 — `games_external_cog.py` missed entirely
`0d3c12c4` never touched this file (it postdates the external-economy
stages, landed on a different branch). 5 sites still read
`"Guild only."` with no ❌ prefix and the banned word "guild":
lines 225, 256, 299, 318, 355 → `"❌ This command only works in a server."`
(matches the established wording elsewhere, e.g. `ai_mod_cog.py`).

### Gap 2 — two logic/service strings missed
- `src/bot_modules/emoji_stealer/logic.py:78` — validation error string,
  no ❌ prefix.
- `src/bot_modules/services/economy_qotd_sponsor_service.py` —
  `resolve_submission`'s 3 raises (surfaced unwrapped via
  `economy/sponsor_views.py:294`'s `_safe_ephemeral(interaction, str(exc))`)
  have no ❌ prefix. `submit_sponsor`'s raises are fine (wrapped at the
  economy_cog.py call site).

### Stage 8 — Progress bars `█░`/eighth-block → `▰▱`
Still legacy in 5 files: `games/utils/live_bar.py` (`build_bar`, feeds
`games_ama/embeds.py`), `cogs/chicken/cog.py` (`_meter_bar`),
`cogs/pressure_cooker/views.py` (`gauge_bar`), `privacy/logic.py`
(`render_progress_bar`). Converge on the `▰▱` vocabulary and the
`{bar} {current:,}/{target:,}` format; drop bracket/pipe wrappers. Update
`tests/cogs/test_pressure_views.py` and `tests/test_privacy_logic.py`,
which pin the old rendering.

### Stage 11 — Bot-side ASCII `...` → `…`
13 sites confirmed still ASCII:
- Placeholders: `dm_perms_cog.py:441`, `games_ama_cog.py:96,228`,
  `games_hottakes_cog.py:73`, `games_clapback_cog.py:81`,
  `games_ffa_cog.py:152`, `games_price_cog.py:141`, `jail_cog.py:114`,
  `games_story_cog.py:66`.
- Modal field labels: `games_mlt_cog.py:176`, `games_nhie_cog.py:54`.
- Truncation: `games_rushmore/embeds.py:65`, `services/risky_roll/views.py:493`.

## Non-goals
- Re-auditing anything `0d3c12c4` already covers — spot-checked, trust it.
- Dashboard ellipsis — confirmed clean, no gap.
