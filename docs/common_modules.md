# Common modules — the shared seams (Reference)

A map, not a rulebook. `docs/design_guide.md` holds the order of the decisions
and the coding standards; this is the answer to the question that comes up
*while* you build — **does something already own this?**

Every module below carries a real module docstring explaining what it owns and
what it deliberately leaves to its callers. **Those docstrings are
authoritative; this page only tells you which one to open.** If a line here
disagrees with the module, the module wins and the line is a bug worth fixing.

Two things this page is not:

- **Not the consolidation backlog.** `docs/plans/common-lib-round-2.md` is the
  2026-08-22 repo-wide review: the ranked queue of duplication still worth
  collapsing, *and* a verified LEAVE list of twins that are deliberate. Read
  that before proposing a de-duplication — half the obvious candidates have
  already been examined and kept apart on purpose.
- **Not exhaustive.** It lists the seams a new feature reaches for by mistake
  when it doesn't know they exist. Small helpers stay findable by grep.

---

## Safety and identity

| Seam | What it owns |
|---|---|
| `services/no_contact_service` — `is_no_contact_conn`, `no_contact_partners_conn` | The no-contact list. Every member-to-member surface reads it through these, never by querying `no_contact_pairs`. → `no_contact_spec.md` |
| `games/utils/question_source.channel_allows_nsfw` | The single NSFW verdict, gated on Discord's own `is_nsfw()` with thread-parent inheritance and fail-safe-to-SFW. ~25 call sites; `advisor_context.can_view` delegates to the same decision |
| `services/name_resolver.build_name_fn` | Turning a member id into a name a *reading* client can render. Builders take the `name_fn`; a repo test guards that every render site passes one. → `embed_style_guide.md` § Naming members in embeds |
| `services/privacy_service` — `SUBJECT_ID_COLUMNS`, `purge_user_data` | Which column names mean "this row is about a member". A table whose subject column isn't listed is invisible to the subject-access export. → `privacy_spec.md`, `data_register.md` |
| `AppContext.member_is_mod` | The mod check, for a `Member` already in hand — it reads roles and nothing else. The uncached-member-reads-as-not-a-mod behaviour belongs to its sibling `is_mod(interaction)`, where `get_interaction_member` returning `None` is what decides it — chosen, because an unearned tick is harder to undo than a missed one |

## Embeds, DMs and panels

| Seam | What it owns |
|---|---|
| `core/branding.safe_resolve_accent` | The accent colour, from a bot, an AppContext or a db_path. Never call `resolve_accent_color` directly — it raises, and a repo-wide test fails the suite if you do |
| `services/dm_branding` — `send_branded_dm`, `brand_dm_embed` | Branded DM delivery. Four near-identical `_try_dm` helpers existed before it did |
| `services/economy_service.notify_member` | Member notification with the DM → bank-channel fallback, and `require_game_role=True` for recurring economy DMs. Returns a bare `bool`; it is `deliver_econ_dm` underneath that returns the `DmDelivery` naming the surface actually used, so reach for that one when the surface matters |
| `core/utils.jump_url` | Message permalinks. Never hand-roll the URL |
| `economy/view_helpers` — `EphemeralCard`, `review_surface` | Repainting a review card that may live in a channel *or* behind the todo board's ephemeral detail message. An ephemeral message can't be edited through the channel-message endpoint; this is the one wrapper that hides the difference instead of branching at every repaint |
| `web_server/routes/panel_posting` — `sticky_conflict`, `own_channel_id` | The guards every "post this panel into a channel" route needs. A panel already in the target channel is **warned, never blocked** — refusing doesn't undo a collision, it just locks the admin out of maintaining a panel that is sitting there |

## Games

| Seam | What it owns |
|---|---|
| `games/utils/question_source` | Every question-drawing game's draw: the shared `games_question_bank` table, heat-tag filtering, NSFW gating, least-recently-served selection and mark-served. Bank-only — an empty bank means the game has no question, and there is no AI fallback |
| `games/utils/send_retry` | Retrying a Discord call that failed transiently, and the `is_transient` classification that decides whether it was worth retrying at all. discord.py retries `{500, 502, 504, 524}` unconditionally but **not 503**, so a bare `channel.send` dies of one. Filed under `games/` because that is who calls it today; nothing in it is game-shaped, so reach for it before writing a fourth inline 5xx loop (`core/sticky._retry_delete` and `events_cog._fetch_reaction_message` are the other two, each with a genuinely different contract) |
| `games/utils/derangement.random_derangement` | Secret-partner assignment: everyone gives to exactly one other, receives from exactly one other, nobody gets themselves (Sattolo cycle, O(n)) |
| `duels/base_game.py` | The template-method base the six mini-games override. Their `on_game_start` / `render_*_state` / `handle_interaction` "clones" are required overrides, not duplication — see the LEAVE list in `plans/common-lib-round-2.md` |

## Economy

| Seam | What it owns |
|---|---|
| `services/economy_submission_store` | The ledger mechanics under every paid, mod-approved submission — `charge_and_insert`, `move_state`, `refund_once`, `expire_stale_pending`, `set_card`. Only the mechanism: receipt wording, card shape, caps and what approval *produces* stay with the product. `move_state` returns `None` on a lost race rather than raising, because the apology sentence is copy. **Bounties are deliberately not this shape** (many-payer pot with a rake, no pending state) |
| `services/economy_approvals_service` | The merge that makes three products one job for a moderator: pending rows read oldest-first across themed day, sponsored QOTD and pin. Adding a fourth product is one row in `QUEUES`. Emoji sponsorship is deliberately excluded — its approval is an upload, not a yes/no |

## Config, state and AI

| Seam | What it owns |
|---|---|
| `services/settings_registry` | The declarative inventory of config keys the AI assistant may reason about. **Hand-authored on purpose** — an auto-derived dump would be noise and would expose keys nobody vetted. `writable` is opt-in per setting; permission-boundary keys (`admin_role_ids`, `mod_role_ids`) and privacy defaults (`message_storage_level`) are never writable at any confirmation level. A registry entry that disagrees with its owning panel is the repo's most common config bug |
| `services/advisor_service` | The grounded-Claude brain behind both the dashboard Help ask box and `/ask`. Answers are grounded **only** in `manual.html`, so the advisor cannot invent a command; the corpus is prompt-cached. Model is a per-guild dial (`resolve_advisor_model`, staff and members resolve separately) — never a constant in a caller |
| `games/utils/ai_client.generate_text` | The thin Anthropic call for short generated copy. Returns `None` on any API error rather than raising — callers degrade, they don't crash |
| `feature_rotation/store` — `claim_flip` / `claim_announce`, `release_flip` / `release_announce` | Per-guild per-day idempotency for once-a-day work. The claim is taken *before* the Discord call so nothing can act twice; `release_*` hands the day back when the work didn't happen, so a failure doesn't burn the day |
