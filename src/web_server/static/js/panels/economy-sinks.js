import { api, apiPut, apiPost, apiDelete, request, esc } from "../api.js";
import { showStatus, guardForm } from "../config-helpers.js";
import { confirmDialog, promptDialog } from "../ui.js";

// The perk-shop prices (the currency sinks). Moved here off the Settings page so
// everything a member can spend on lives in one place. Faucet rates stay on the
// Income Sources page. Each entry: [key, label, {hint, max}] — `max` bounds both
// the input and the client-side check that names the offending field on save.
const PRICE_FIELDS = [
  ["price_role_color", "Role Color, Per Week", {
    hint: "Weekly rent for picking a custom color on a member's own role. 0 makes it free.",
  }],
  ["price_role_name", "Role Name, Per Week", {
    hint: "Weekly rent for naming their own role. 0 makes it free.",
  }],
  ["price_role_icon", "Custom Role Icon, Per Week", {
    hint: "Weekly rent when a member uploads an icon of their own. The curated catalog icons further down are priced one by one instead.",
  }],
  ["price_role_gradient", "Role Gradient, Per Week", {
    hint: "Weekly rent for a two-color gradient on their role. 0 makes it free.",
  }],
  ["price_role_holographic", "Role Holographic Shimmer, Per Week", {
    hint: "Discord's fixed holographic shimmer — a separate, pricier tier than the two-color gradient. There is nothing to pick; renting it is the whole perk. Your server needs Discord's enhanced role colors feature for it to show up at all.",
  }],
  ["price_voice_style", "Voice Room Lease, Per Week", {
    hint: "Weekly rent for the Voice Master rename and user-limit controls. 0 (the default) leaves those controls free for everyone. Setting a price is what launches this as a paid perk, so tell members before you do.",
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

// Sponsored emojis: weekly rentals opened by mod approval (queue below).
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
const DEFAULT_MAX = 100000000;

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

function iconRow(icon) {
  const bust = Date.now();
  const usedBadge = icon.in_use
    ? `<span class="badge" title="Members are renting this icon right now">In use</span>`
    : "";
  const enabledAttr = icon.enabled ? " checked" : "";
  return `
    <div class="card" data-icon-id="${icon.id}"
         style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px;">
      <img src="/api/economy/icon-catalog/${icon.id}/image?t=${bust}" alt=""
           width="48" height="48"
           style="width:48px;height:48px;border-radius:8px;object-fit:contain;
                  background:repeating-conic-gradient(#808080 0% 25%, #a0a0a0 0% 50%) 50% / 12px 12px" />
      <div class="field" style="margin:0;">
        <label>Name</label>
        <input type="text" data-name maxlength="64" value="${esc(icon.name)}" style="max-width:200px;" />
      </div>
      <div class="field" style="margin:0;">
        <label>Price Per Week</label>
        <input type="number" data-price min="0" max="${DEFAULT_MAX}" step="1" value="${icon.price}" style="max-width:120px;" />
      </div>
      <label style="display:flex;gap:6px;align-items:center;">
        <input type="checkbox" data-enabled${enabledAttr} /> Offer in the shop
      </label>
      ${usedBadge}
      <div style="display:flex;gap:8px;margin-left:auto;">
        <button type="button" class="btn btn-primary" data-save>Save</button>
        <button type="button" class="btn btn-danger" data-delete>Delete</button>
      </div>
      <span data-row-status></span>
    </div>`;
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading prices…</div></div>`;

  (async () => {
    const [cfg, metrics, icons] = await Promise.all([
      api("/api/economy/config"),
      api("/api/economy/metrics").catch(() => null),
      api("/api/economy/icon-catalog").catch(() => []),
    ]);
    const pricing = metrics && metrics.hints && Object.keys(metrics.hints).length
      ? { hints: metrics.hints, median: metrics.median_income }
      : null;
    render(container, cfg, pricing, icons);
  })();
}

// The economy master switch lives on Economy Settings. With it off, nothing on
// this page has any effect — say so instead of letting an admin price a shop
// nobody can open (W-C6).
export function economyOffBanner(cfg) {
  if (cfg && cfg.enabled) return "";
  return `<div class="empty" role="status" style="margin-bottom:12px;">
    The economy is currently off, so nothing below takes effect until it is switched
    on under <a href="#/economy-config">Economy Settings</a>.</div>`;
}

function render(container, cfg, pricing, icons) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Sinks</h2>
        <div class="subtitle">Everything members can spend currency on — perk-shop prices
          and the rentable icon catalog. What they earn is set on
          <a href="#/economy-income-sources">Income Sources</a>.</div>
      </header>
      ${economyOffBanner(cfg)}

      <form class="form form-cards" data-price-form>
        <div class="card">
          <div class="section-label">Perk Prices</div>
          <div class="field-row" style="flex-wrap:wrap;">
            ${PRICE_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing)).join("")}
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

      <section class="form card" style="margin-top:1.5rem;">
        <div class="section-label">Emoji Approval Queue</div>
        <div class="field-hint" style="margin-bottom:1rem;">
          Emojis members have sponsored and paid for, waiting on your decision.
          Approving uploads the emoji to the server and starts its weekly rent — the
          first week is already paid. Turning one down refunds the member in full. If a
          rental later lapses, the emoji comes back down on its own.
        </div>
        <div data-emoji-queue></div>
        <div data-emoji-empty class="field-hint" style="display:none;">Nothing is waiting for review.</div>
      </section>

      <section class="form card" style="margin-top:1.5rem;">
        <div class="section-label">Rentable Icon Catalog</div>
        <div class="field-hint" style="margin-bottom:1rem;">
          Role icons you curate, which members rent from <code>/bank shop</code> at whatever
          price you give each one. A price change takes effect at each renter's next weekly
          renewal, never mid-week. An icon somebody is currently renting cannot be deleted —
          stop offering it instead, and the people already renting it keep it. Uploaded
          images are shrunk to a small PNG, because Discord will not accept a role icon over
          256&nbsp;kilobytes, and your server needs Discord's Role Icons feature for any of
          this to appear.
        </div>

        <div data-catalog></div>
        <div data-catalog-empty class="field-hint" style="display:none;">
          No icons in the catalog yet. Add one below and members will see it in the shop.
        </div>

        <div style="margin-top:1.25rem;padding-top:1rem;border-top:1px solid var(--border,#333);">
          <div class="section-label">Add an Icon</div>
          <div class="field-row" style="flex-wrap:wrap;align-items:flex-end;">
            <div class="field">
              <label for="sink-add-name">Name</label>
              <input type="text" id="sink-add-name" data-add-name maxlength="64" placeholder="e.g. Golden Crown" style="max-width:200px;" />
              <div class="field-hint">What members see in the shop.</div>
            </div>
            <div class="field">
              <label for="sink-add-price">Price Per Week</label>
              <input type="number" id="sink-add-price" data-add-price min="0" max="${DEFAULT_MAX}" step="1" value="75" style="max-width:120px;" />
            </div>
            <div class="field">
              <label for="sink-add-file">Image</label>
              <input type="file" id="sink-add-file" data-add-file accept="image/png,image/webp,image/jpeg,image/gif" />
              <div class="field-hint">A PNG, WEBP, JPEG, or GIF. Square images look best.</div>
            </div>
            <button type="button" class="btn btn-primary" data-add>Add Icon</button>
            <span data-add-status></span>
          </div>
        </div>
      </section>
    </div>`;

  wirePrices(container, cfg);
  wireCatalog(container, icons);
  wireEmojiQueue(container);
}

function emojiRow(sub) {
  const kind = sub.animated ? "animated" : "static";
  return `
    <div class="card" data-sub-id="${sub.id}"
         style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px;">
      <img src="/api/economy/emoji-submissions/${sub.id}/image" alt=""
           width="48" height="48"
           style="width:48px;height:48px;object-fit:contain;
                  background:repeating-conic-gradient(#808080 0% 25%, #a0a0a0 0% 50%) 50% / 12px 12px" />
      <div>
        <div><code>:${esc(sub.name)}:</code> <span class="field-hint">(${kind}, ${sub.price}/wk)</span></div>
        <div class="field-hint">from <span data-member-id="${esc(sub.user_id)}">${esc(sub.user_id)}</span></div>
      </div>
      <div style="display:flex;gap:8px;margin-left:auto;">
        <button type="button" class="btn btn-primary" data-approve>Approve and Upload</button>
        <button type="button" class="btn btn-danger" data-deny>Turn Down</button>
      </div>
      <span data-row-status></span>
    </div>`;
}

function wireEmojiQueue(container) {
  const listEl = container.querySelector("[data-emoji-queue]");
  const emptyEl = container.querySelector("[data-emoji-empty]");

  async function refresh() {
    let subs = [];
    try {
      subs = (await api("/api/economy/emoji-submissions?state=pending")).submissions;
    } catch (err) {
      listEl.innerHTML = `<div class="error">${esc(err.message)}</div>`;
      return;
    }
    listEl.innerHTML = subs.map(emojiRow).join("");
    emptyEl.style.display = subs.length ? "none" : "block";
  }
  refresh();

  listEl.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const row = btn.closest("[data-sub-id]");
    const id = row.getAttribute("data-sub-id");
    const rowStatus = row.querySelector("[data-row-status]");
    btn.disabled = true;
    try {
      if (btn.hasAttribute("data-approve")) {
        const out = await apiPost(`/api/economy/emoji-submissions/${id}/approve`, {});
        showStatus(rowStatus, out.ok, out.ok ? "Live" : out.error);
      } else if (btn.hasAttribute("data-deny")) {
        // Shared dialog rather than the browser's native prompt(), so it is
        // themed, focus-trapped, and keyboard-accessible like every other
        // confirmation on the dashboard.
        const reason = await promptDialog(
          "This member is refunded in full and sent your reason. What should they be told?",
          { title: "Turn down this emoji?", confirmLabel: "Turn Down", danger: true },
        );
        if (reason === null) { btn.disabled = false; return; }
        await apiPost(`/api/economy/emoji-submissions/${id}/deny`, {
          reason: reason.trim() || "not a fit for the server",
        });
        showStatus(rowStatus, true, "Turned down and refunded");
      }
      await refresh();
    } catch (err) {
      showStatus(rowStatus, false, err.message);
      btn.disabled = false;
    }
  });
}

function wirePrices(container, cfg) {
  const form = container.querySelector("[data-price-form]");
  const status = form.querySelector("[data-price-status]");
  for (const [key] of ALL_NUM_FIELDS) {
    form.querySelector(`[name=${key}]`).value = cfg[key];
  }
  form.querySelector("[name=raffle_enabled]").checked = !!cfg.raffle_enabled;

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
    try {
      await apiPut("/api/economy/config", payload);
      showStatus(status, true);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}

function wireCatalog(container, icons) {
  const listEl = container.querySelector("[data-catalog]");
  const emptyEl = container.querySelector("[data-catalog-empty]");

  function renderList(rows) {
    listEl.innerHTML = rows.map(iconRow).join("");
    emptyEl.style.display = rows.length ? "none" : "block";
  }
  renderList(icons);

  // Row actions (save / delete) via delegation so re-rendered rows stay wired.
  listEl.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const row = btn.closest("[data-icon-id]");
    const id = row.getAttribute("data-icon-id");
    const rowStatus = row.querySelector("[data-row-status]");

    if (btn.hasAttribute("data-save")) {
      const nameInput = row.querySelector("[data-name]");
      const priceInput = row.querySelector("[data-price]");
      if (!nameInput.value.trim()) {
        showStatus(rowStatus, false, "Name cannot be empty");
        nameInput.focus();
        return;
      }
      const price = parseInt(priceInput.value, 10);
      if (!Number.isFinite(price) || price < 0 || price > DEFAULT_MAX) {
        showStatus(rowStatus, false, `Price Per Week must be a whole number from 0 to ${DEFAULT_MAX}`);
        priceInput.focus();
        return;
      }
      btn.disabled = true;
      try {
        await request("PATCH", `/api/economy/icon-catalog/${id}`, {
          body: {
            name: nameInput.value.trim(),
            price,
            enabled: row.querySelector("[data-enabled]").checked,
          },
        });
        showStatus(rowStatus, true);
      } catch (err) {
        showStatus(rowStatus, false, err.message);
      } finally {
        btn.disabled = false;
      }
    } else if (btn.hasAttribute("data-delete")) {
      // Deleting throws away the curated icon AND the image that was uploaded
      // for it, with no undo — every sibling flow confirms first (W-C9).
      const iconName = row.querySelector("[data-name]").value.trim() || "this icon";
      const ok = await confirmDialog(
        `"${iconName}" and the image uploaded for it are deleted for good, and it disappears from the shop. `
        + "This cannot be undone. To retire an icon while keeping it for current renters, "
        + "clear \"Offer in the shop\" and save instead.",
        { title: "Delete this icon?", danger: true, confirmLabel: "Delete" },
      );
      if (!ok) return;
      btn.disabled = true;
      try {
        await apiDelete(`/api/economy/icon-catalog/${id}`);
        const fresh = await api("/api/economy/icon-catalog");
        renderList(fresh);
      } catch (err) {
        // 409 = in use: surface the reason, keep the row.
        showStatus(rowStatus, false, err.message);
        btn.disabled = false;
      }
    }
  });

  // Add form.
  const addBtn = container.querySelector("[data-add]");
  const addStatus = container.querySelector("[data-add-status]");
  addBtn.addEventListener("click", async () => {
    const name = container.querySelector("[data-add-name]");
    const price = container.querySelector("[data-add-price]");
    const file = container.querySelector("[data-add-file]");
    if (!name.value.trim()) {
      showStatus(addStatus, false, "Give the icon a Name first");
      name.focus();
      return;
    }
    if (!file.files.length) {
      showStatus(addStatus, false, "Choose an Image first");
      file.focus();
      return;
    }
    const priceValue = parseInt(price.value, 10);
    if (!Number.isFinite(priceValue) || priceValue < 0 || priceValue > DEFAULT_MAX) {
      showStatus(addStatus, false, `Price Per Week must be a whole number from 0 to ${DEFAULT_MAX}`);
      price.focus();
      return;
    }
    const fd = new FormData();
    fd.append("name", name.value.trim());
    fd.append("price", priceValue);
    fd.append("image", file.files[0]);
    addBtn.disabled = true;
    showStatus(addStatus, true, "Uploading…");
    try {
      await apiPost("/api/economy/icon-catalog", fd);
      name.value = "";
      file.value = "";
      const fresh = await api("/api/economy/icon-catalog");
      renderList(fresh);
      showStatus(addStatus, true, "Added");
    } catch (err) {
      showStatus(addStatus, false, err.message);
    } finally {
      addBtn.disabled = false;
    }
  });
}
