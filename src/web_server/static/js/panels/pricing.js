/**
 * The priced dials — every number a member's coins are spent against.
 *
 * Lifted out of the 1,339-line "Shop & Perks" page, where these six cards sat
 * above an emoji approval queue, three catalogs and an order queue. Three
 * reasons that was wrong, all of them visible in the code rather than matters
 * of taste:
 *
 *  - ONE SAVE, SIX SUBJECTS. All six cards were inside a single <form> with a
 *    single submit, so adjusting a raffle ticket price also wrote the hoard tax
 *    rate. They are one form here too — but now the form is the whole page, so
 *    "Save" means what it says.
 *
 *  - A SHARED DIRTY BIT. `guardForm`'s unsaved-edits flag is a module global in
 *    config-helpers.js, and `showStatus(el, true, …)` clears it. The old page
 *    had 41 showStatus call sites: approving an emoji, or merely *starting* an
 *    image upload, cleared the warning that protected a half-typed tax rate, so
 *    navigating away lost it silently. Nothing else on this page can clear it.
 *
 *  - CADENCE. These are set at launch and revisited a few times a year — the
 *    services call a nonzero rate "the launch switch" and a price change
 *    notifies existing renters. That does not belong beside a queue an admin
 *    works through weekly.
 *
 * The PUT is a partial update, so sending only these keys is exactly what the
 * old page sent. Five other panels write the same endpoint the same way.
 */
import { api, apiPut, esc } from "../api.js";
import { showStatus, guardForm, mountAsync } from "../config-helpers.js";
import { DEFAULT_MAX, economyOffBanner } from "./economy-shop-shared.js";

// Each entry: [key, label, {hint, max}] — `max` bounds both the input and the
// client-side check that names the offending field on save.
const PRICE_FIELDS = [
  ["price_role_color", "Role Color, Per Week", {
    hint: "Weekly rent for picking a custom color on a member's own role. 0 makes it free.",
  }],
  ["price_role_name", "Role Name, Per Week", {
    hint: "Weekly rent for naming their own role. 0 makes it free.",
  }],
  ["price_role_icon", "Custom Role Icon, Per Week", {
    hint: "Weekly rent when a member uploads an icon of their own. The curated catalog icons on Shop &amp; Perks are priced one by one instead.",
  }],
  ["price_role_preset", "Palette Color, Per Week", {
    hint: "Weekly rent for a color from your curated palette (set up on Shop &amp; Perks). Members pick a named gradient instead of choosing their own two colors, so price it below the Role Gradient. A palette color given its own price overrides this one. 0 makes it free.",
  }],
  ["price_role_gradient", "Role Gradient, Per Week", {
    hint: "Weekly rent for a two-color gradient a member picks themselves. Worth pricing above the curated Palette Color, since this is the same effect with a free choice of colors. 0 makes it free.",
  }],
  ["price_role_holographic", "Role Holographic Shimmer, Per Week", {
    hint: "Discord's fixed holographic shimmer — a separate, pricier tier than the two-color gradient. There is nothing to pick; renting it is the whole perk. Your server needs Discord's enhanced role colors feature for it to show up at all.",
  }],
  ["price_voice_style", "Voice Room Lease, Per Week", {
    hint: "Weekly rent for the Voice Control rename and user-limit controls. 0 (the default) leaves those controls free for everyone. Setting a price is what launches this as a paid perk, so tell members before you do.",
  }],
];

// One-shot buys rather than weekly rentals — cheap enough to be an impulse,
// which is the tier the rental ladder doesn't reach. Saved by the same form.
const CONSUMABLE_FIELDS = [
  ["price_quest_reroll", "Quest Reroll", {
    hint: "Charged each time a member swaps out a quest, after their free daily swap. 0 turns paid rerolls off — the free one stays either way.",
  }],
  ["quest_reroll_daily_cap", "Paid Rerolls Per Day", {
    hint: "How many paid rerolls one member may buy in a day, on top of the free one. 0 turns paid rerolls off.",
    max: 100,
  }],
  ["price_streak_shield", "Streak Shield", {
    hint: "A one-time buy that is spent automatically to rescue a login streak the free grace day cannot cover. A member can hold only one at a time. 0 removes it from the shop.",
  }],
  ["price_pin_of_day", "Pin of the Day", {
    hint: "What /bank pin costs. A member pays to pin a short message, a moderator approves it, and Dungeon Keeper pins a card for 24 hours before unpinning it again. 0 turns it off. It also needs a pin channel set on the Economy Settings page. Coins are taken when the request is sent and refunded if it is declined or expires unreviewed. This one is very public — tell members before switching it on.",
  }],
  ["pin_expire_days", "Pin Review Window (days)", {
    hint: "A pin request nobody approves or declines within this many days expires on its own and refunds the member. 0 leaves requests queued forever.",
    max: 365,
  }],
];

// Weekly raffle: tickets in, a free-perk-week voucher out. The enable flag
// is a checkbox (the one non-numeric field on this page) because the winner
// is announced BY NAME — turning it on is a communications decision, not a
// price tweak.
const RAFFLE_FIELDS = [
  ["price_raffle_ticket", "Ticket Price", {
    hint: "What one raffle ticket costs.",
  }],
  ["raffle_max_tickets", "Tickets Per Member, Per Week", {
    hint: "The most tickets one member may buy in a week — this is what stops the richest wallet simply buying the win.",
    max: 10000,
  }],
];

// Sponsored QOTD: a member pays to queue their own question; refunded if a mod
// denies it or it expires unreviewed. Charged once at submit (not a rental).
const QOTD_FIELDS = [
  ["price_qotd_sponsor", "Sponsored Question", {
    hint: "Charged when a member submits a paid question of the day. Refunded if it is turned down or expires unreviewed. 0 lets members sponsor questions for free.",
  }],
  ["qotd_sponsor_expire_days", "Review Window (days)", {
    hint: "A sponsored question nobody has reviewed within this many days expires and refunds itself.",
    max: 365,
  }],
];

// Evaporation dials: the weekly hoard tax (demurrage — the only sink that
// works on members who buy nothing) and the house rake on PvP wager pots.
// Both default 0 (off) — like the raffle, turning either on is a
// communications decision, so announce before setting a rate.
const DEMURRAGE_FIELDS = [
  ["demurrage_rate_pct", "Hoard Tax Rate (percent)", {
    hint: "The share of everything above the protected floor that is collected at each weekly roll. 0 (the default) turns the tax off; 100 makes the floor a hard wealth cap. Around 2 percent is a gentle setting.",
    max: 100,
  }],
  ["demurrage_threshold", "Protected Floor", {
    hint: "Balances at or below this figure are never touched — only what sits above it is taxed, so no member can ever be taxed below the floor.",
  }],
  ["wager_rake_pct", "Wager Rake (percent)", {
    hint: "The house's cut of each settled member-versus-member wager pot. 0 (the default) keeps wagers a straight winner-takes-all transfer. Refunded wagers are never raked, and the winner's payout message names the cut.",
    max: 50,
  }],
  ["bounty_rake_pct", "Bounty Rake (percent)", {
    hint: "The house's cut when a community bounty is awarded. 0 (the default) means the winner takes the whole pot. Cancelled or expired bounties are never raked — every contributor gets everything back. Set the board channel on the Economy Settings page to switch bounties on at all.",
    max: 100,
  }],
];

// Sponsored emojis: weekly rentals opened by mod approval. The queue that
// approves them is its own page now (Approvals) — see the split note at the
// top of this file. Only the slot count is read while approving.
const EMOJI_FIELDS = [
  ["price_emoji", "Sponsored Emoji, Per Week", {
    hint: "Weekly rent for a member-sponsored custom emoji. The first week is held in escrow the moment they submit it. 0 stops new sponsorships; emojis already running keep being billed.",
  }],
  ["price_emoji_animated", "Sponsored Animated Emoji, Per Week", {
    hint: "Weekly rent for an animated sponsored emoji. Animated slots are scarcer, so this normally costs more.",
  }],
  ["emoji_sponsor_slots", "Sponsored Emoji Slots", {
    hint: "The most sponsorships that can be in flight at once, counting both those awaiting review and those already live. Sponsored emojis also never take the server's last free emoji slot.",
    max: 200,
  }],
  ["emoji_sponsor_expire_days", "Review Window (days)", {
    hint: "A submission nobody has reviewed within this many days expires and refunds itself.",
    max: 365,
  }],
];

const ALL_NUM_FIELDS = [
  ...PRICE_FIELDS, ...CONSUMABLE_FIELDS, ...EMOJI_FIELDS, ...RAFFLE_FIELDS,
  ...QOTD_FIELDS, ...DEMURRAGE_FIELDS,
];

// Every numeric field is capped somewhere so a typo can't create a price no
// member could ever pay; DEFAULT_MAX applies where the field has no natural
// ceiling of its own.

function fieldMax(opts) {
  return opts && opts.max != null ? opts.max : DEFAULT_MAX;
}

function numField(key, label, opts = {}, pricing) {
  const { hint } = opts;
  const hintHtml = hint ? `<div class="field-hint">${esc(hint)}</div>` : "";
  const suggested = pricing && pricing.hints ? pricing.hints[key] : null;
  const median = pricing ? Math.round(pricing.median || 0) : 0;
  const suggest = suggested != null
    ? `<div class="field-hint">Suggested: about ${suggested}, based on a median weekly income of ${median}.</div>`
    : "";
  return `
    <div class="field">
      <label for="sink-${key}">${esc(label)}</label>
      <input type="number" name="${key}" id="sink-${key}" required
        min="0" max="${fieldMax(opts)}" step="1" style="max-width:140px;" />
      ${hintHtml}
      ${suggest}
    </div>`;
}


export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading prices…</div></div>`;

  return mountAsync(container, async () => {
    // Two requests, not the old page's nine: the catalogs and the queues moved
    // out, so a slow icon-catalog fetch can no longer hold up the prices.
    const [cfg, metrics] = await Promise.all([
      api("/api/economy/config"),
      api("/api/economy/metrics").catch(() => null),
    ]);
    const pricing = metrics && metrics.hints && Object.keys(metrics.hints).length
      ? { hints: metrics.hints, median: metrics.median_income }
      : null;
    render(container, cfg, pricing);
    wirePrices(container, cfg);
  }, { errorMsg: "Couldn’t load the perk prices." });
}

function render(container, cfg, pricing) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Pricing</h2>
        <div class="subtitle">What every perk costs, and the rates that take currency
          back out. The things members buy are curated on
          <a href="#/economy-sinks">Shop &amp; Perks</a>; what they earn is set on
          <a href="#/economy-income-sources">Income Sources</a>.</div>
      </header>
      ${economyOffBanner(cfg)}

      <form class="form form-cards" data-price-form>
        <div class="card">
          <div class="section-label">Perk Prices</div>
          <div class="field-row" style="flex-wrap:wrap;">
            ${PRICE_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing)).join("")}
          </div>
          <div class="field" style="margin-top:8px;">
            <label style="display:flex;gap:6px;align-items:center;">
              <input type="checkbox" name="mod_perk_comp" /> Moderators get perks free
            </label>
            <div class="field-hint">When checked, anyone with a moderator or admin role
              here — or Discord's Manage Server permission — is entitled to every perk
              above without renting one. Nothing is charged and nothing is recorded as a
              purchase, so this does not show up as spending in your economy figures. The
              perks appear the moment someone gains the role and come off when they lose
              it. A moderator already paying for a rental keeps paying until they cancel
              it themselves from the shop.</div>
          </div>
        </div>

        <div class="card">
          <div class="section-label">Consumables</div>
          <div class="field-row" style="flex-wrap:wrap;">
            ${CONSUMABLE_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing)).join("")}
          </div>
        </div>

        <div class="card">
          <div class="section-label">Weekly Raffle</div>
          <div class="field" style="margin-bottom:8px;">
            <label style="display:flex;gap:6px;align-items:center;">
              <input type="checkbox" name="raffle_enabled" /> Run a weekly raffle
            </label>
            <div class="field-hint">When checked, members can buy tickets and a winner
              is drawn at the weekly roll. Unchecked, no tickets are sold and no draw
              happens.</div>
          </div>
          <div class="field-row" style="flex-wrap:wrap;align-items:flex-end;">
            ${RAFFLE_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing)).join("")}
          </div>
          <div class="field-hint">
            The draw happens when the week rolls over. The prize is one week of perks
            paid for the winner — a voucher, never coins — and the winner is named
            publicly on the leaderboard panel. Tell members the raffle exists before
            you switch it on.
          </div>
        </div>

        <div class="card">
          <div class="section-label">Hoard Tax and Rakes</div>
          <div class="field-row" style="flex-wrap:wrap;">
            ${DEMURRAGE_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing)).join("")}
          </div>
          <div class="field-hint">
            These are the dials that take currency back out of circulation, and all of
            them start at 0, meaning off. The hoard tax is collected at the weekly roll
            from wallets sitting above the protected floor; each rake comes out of a pot
            as it settles. Every collection appears in the register feed like any other
            transaction, so members will see it. Announce a rate before you set one.
          </div>
        </div>

        <div class="card">
          <div class="section-label">Sponsored Emojis</div>
          <div class="field-row" style="flex-wrap:wrap;">
            ${EMOJI_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing)).join("")}
          </div>
        </div>

        <div class="card">
          <div class="section-label">Sponsored QOTD</div>
          <div class="field-row" style="flex-wrap:wrap;">
            ${QOTD_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing)).join("")}
          </div>
        </div>

        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary">Save</button>
          <span data-price-status></span>
        </div>
      </form>
    </div>
  `;
}

function wirePrices(container, cfg) {
  const form = container.querySelector("[data-price-form]");
  const status = form.querySelector("[data-price-status]");
  for (const [key] of ALL_NUM_FIELDS) {
    form.querySelector(`[name=${key}]`).value = cfg[key];
  }
  form.querySelector("[name=raffle_enabled]").checked = !!cfg.raffle_enabled;
  form.querySelector("[name=mod_perk_comp]").checked = !!cfg.mod_perk_comp;

  guardForm(form);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {};
    // A blank box used to become NaN, then null, and came back as a raw
    // "Input should be a valid integer" naming no field. Check here and say
    // exactly which field is wrong (W-C5).
    for (const [key, label, opts] of ALL_NUM_FIELDS) {
      const input = form.querySelector(`[name=${key}]`);
      const max = fieldMax(opts);
      const n = parseInt(input.value, 10);
      if (!Number.isFinite(n) || n < 0 || n > max) {
        showStatus(status, false, `${label} must be a whole number from 0 to ${max}`);
        input.focus();
        return;
      }
      payload[key] = n;
    }
    payload.raffle_enabled = form.querySelector("[name=raffle_enabled]").checked;
    payload.mod_perk_comp = form.querySelector("[name=mod_perk_comp]").checked;
    try {
      await apiPut("/api/economy/config", payload);
      showStatus(status, true);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}
