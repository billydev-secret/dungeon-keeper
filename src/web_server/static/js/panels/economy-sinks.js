import { api, apiPut, apiPost, apiDelete, request, esc } from "../api.js";
import { showStatus } from "../config-helpers.js";

// The perk-shop prices (the currency sinks). Moved here off the Settings page so
// everything a member can spend on lives in one place. Faucet rates stay on the
// Income Sources page. Each entry: [key, label, {min, hint}].
const PRICE_FIELDS = [
  ["price_role_color", "Role color", {}],
  ["price_role_name", "Role name", {}],
  ["price_role_icon", "Role icon (custom upload)", {
    hint: "Flat price when a member uploads their own icon. Curated catalog icons below are priced individually.",
  }],
  ["price_role_gradient", "Role gradient", {}],
  ["price_role_holographic", "Role holographic", {
    hint: "Discord's fixed holographic shimmer preset — a distinct, pricier tier than the two-colour gradient. Members pick nothing; renting it is the whole thing. Needs the server's enhanced role colours feature to render.",
  }],
  ["price_voice_style", "Voice style", {
    hint: "Weekly lease for Voice Master rename + user limit. 0 (the default) keeps those controls free for everyone — setting a price is the launch switch, so announce before flipping it.",
  }],
  ["price_text_room", "Text room", { hint: "Used by a later stage." }],
  ["price_voice_room", "Voice room", { hint: "Used by a later stage." }],
];

// One-shot buys rather than weekly rentals — cheap enough to be an impulse,
// which is the tier the rental ladder doesn't reach. Saved by the same form.
const CONSUMABLE_FIELDS = [
  ["price_quest_reroll", "Quest reroll", {
    hint: "Charged per reroll after the free daily one. 0 disables paid rerolls (the free one stays).",
  }],
  ["quest_reroll_daily_cap", "Paid rerolls / day", {
    hint: "How many paid rerolls a member can buy per day, on top of the free one. 0 disables paid rerolls.",
  }],
  ["price_streak_shield", "Streak shield", {
    hint: "One-shot: auto-burned to save a login streak the free grace day can't. Members hold at most one. 0 removes it from the shop.",
  }],
  ["price_pin_of_day", "Pin of the day", {
    hint: "What /bank pin costs. A member pays to pin a short message; a mod approves it; the bot pins a card for 24h, then auto-unpins. 0 disables it. Also needs a pin channel set on the Economy config page — it's a public sink, so announce before switching it on. Charged at submit; declines and expired-unreviewed requests refund.",
  }],
  ["pin_expire_days", "Pin review window (days)", {
    hint: "A pin request no mod approves or declines within this many days expires and refunds automatically. 0 keeps requests queued indefinitely.",
  }],
];

// Weekly raffle: tickets in, a free-perk-week voucher out. The enable flag
// is a checkbox (the one non-numeric field on this page) because the winner
// is announced BY NAME — turning it on is a communications decision, not a
// price tweak.
const RAFFLE_FIELDS = [
  ["price_raffle_ticket", "Ticket price", {}],
  ["raffle_max_tickets", "Max tickets / member / week", {
    hint: "Caps how much certainty one wallet can buy.",
  }],
];

// Sponsored QOTD: a member pays to queue their own question; refunded if a mod
// denies it or it expires unreviewed. Charged once at submit (not a rental).
const QOTD_FIELDS = [
  ["price_qotd_sponsor", "Sponsored question", {
    hint: "Charged when a member submits a paid question; refunded on denial or if it expires unreviewed. 0 makes sponsoring free.",
  }],
  ["qotd_sponsor_expire_days", "Review timeout (days)", {
    hint: "A pending sponsored question nobody reviews refunds itself after this many days.",
  }],
];

// Evaporation dials: the weekly hoard tax (demurrage — the only sink that
// works on members who buy nothing) and the house rake on PvP wager pots.
// Both default 0 (off) — like the raffle, turning either on is a
// communications decision, so announce before setting a rate.
const DEMURRAGE_FIELDS = [
  ["demurrage_rate_pct", "Hoard tax rate (%)", {
    hint: "Percent of the excess above the threshold collected at each weekly roll. 0 (the default) = off; 100 = a hard wealth cap at the threshold. Suggested ≈ 2.",
  }],
  ["demurrage_threshold", "Protected floor", {
    hint: "Balances at or below this are never touched — only the excess above it is taxed, so nobody can be taxed below the floor.",
  }],
  ["wager_rake_pct", "Wager rake (%)", {
    hint: "House cut of each settled PvP wager pot (max 50). 0 (the default) keeps wagers a pure winner-takes-all transfer; refunds are never raked. The winner's payout names the cut.",
  }],
];

// Sponsored emojis: weekly rentals opened by mod approval (queue below).
const EMOJI_FIELDS = [
  ["price_emoji", "Emoji / week", {
    hint: "Weekly rent for a sponsored custom emoji, escrowed for week one at submit. 0 disables new sponsorships (running ones keep billing).",
  }],
  ["price_emoji_animated", "Animated emoji / week", {}],
  ["emoji_sponsor_slots", "Sponsored slots", {
    hint: "Max sponsorships in flight (pending + live). Sponsors also never take the server's last free emoji slot.",
  }],
  ["emoji_sponsor_expire_days", "Review timeout (days)", {
    hint: "A pending submission nobody reviews refunds itself after this many days.",
  }],
];

const ALL_NUM_FIELDS = [
  ...PRICE_FIELDS, ...CONSUMABLE_FIELDS, ...EMOJI_FIELDS, ...RAFFLE_FIELDS,
  ...QOTD_FIELDS, ...DEMURRAGE_FIELDS,
];

function numField(key, label, { hint } = {}, pricing) {
  const hintHtml = hint ? `<div class="field-hint">${esc(hint)}</div>` : "";
  const suggested = pricing && pricing.hints ? pricing.hints[key] : null;
  const median = pricing ? Math.round(pricing.median || 0) : 0;
  const suggest = suggested != null
    ? `<div class="field-hint">suggested ≈ ${suggested} (from median weekly income ${median})</div>`
    : "";
  return `
    <div class="field">
      <label>${esc(label)}</label>
      <input type="number" name="${key}" min="0" step="1" style="max-width:140px;" />
      ${hintHtml}
      ${suggest}
    </div>`;
}

function iconRow(icon) {
  const bust = Date.now();
  const usedBadge = icon.in_use
    ? `<span class="badge" title="Members are renting this icon">in use</span>`
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
        <label>Price / week</label>
        <input type="number" data-price min="0" step="1" value="${icon.price}" style="max-width:120px;" />
      </div>
      <label style="display:flex;gap:6px;align-items:center;">
        <input type="checkbox" data-enabled${enabledAttr} /> Enabled
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
  container.innerHTML = `<div class="panel"><div class="empty">Loading sinks…</div></div>`;

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

function render(container, cfg, pricing, icons) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Sinks</h2>
        <div class="subtitle">Everything members spend currency on — perk-shop prices and
          the rentable icon catalog. Faucet rates live on
          <a href="#/economy-income-sources">Income Sources</a>.</div>
      </header>

      <form class="form card" data-price-form>
        <div class="section-label">Perk Prices</div>
        <div class="field-row" style="flex-wrap:wrap;">
          ${PRICE_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing)).join("")}
        </div>
        <div class="section-label" style="margin-top:16px;">Consumables</div>
        <div class="field-row" style="flex-wrap:wrap;">
          ${CONSUMABLE_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing)).join("")}
        </div>
        <div class="section-label" style="margin-top:16px;">Weekly Raffle</div>
        <div class="field-row" style="flex-wrap:wrap;align-items:flex-end;">
          <label style="display:flex;gap:6px;align-items:center;margin-bottom:8px;">
            <input type="checkbox" name="raffle_enabled" /> Enabled
          </label>
          ${RAFFLE_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing)).join("")}
        </div>
        <div class="field-hint" style="margin-bottom:8px;">
          Drawn at the ISO-week roll; the prize is a free weekly perk payment
          (a voucher, never coins) and the winner is announced by name on the
          leaderboard panel — announce the raffle before enabling it.
        </div>
        <div class="section-label" style="margin-top:16px;">Evaporation — hoard tax &amp; wager rake</div>
        <div class="field-row" style="flex-wrap:wrap;">
          ${DEMURRAGE_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing)).join("")}
        </div>
        <div class="field-hint" style="margin-bottom:8px;">
          Both ship at 0 (off). The hoard tax is collected at the weekly roll
          from wallets above the floor; the rake comes out of each settled
          wager pot. Every collection shows in the register feed like any
          other transaction — announce before setting either rate.
        </div>
        <div class="section-label" style="margin-top:16px;">Sponsored Emojis</div>
        <div class="field-row" style="flex-wrap:wrap;">
          ${EMOJI_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing)).join("")}
        </div>
        <div class="section-label" style="margin-top:16px;">Sponsored QOTD</div>
        <div class="field-row" style="flex-wrap:wrap;">
          ${QOTD_FIELDS.map(([k, l, o]) => numField(k, l, o, pricing)).join("")}
        </div>
        <div style="display:flex; gap:8px; align-items:center; margin-top:16px;">
          <button type="submit" class="btn btn-primary">Save Prices</button>
          <span data-price-status></span>
        </div>
      </form>

      <section class="form card" style="margin-top:1.5rem;">
        <div class="section-label">Emoji Approval Queue</div>
        <div class="field-hint" style="margin-bottom:1rem;">
          Member-sponsored emojis waiting for review. Approving uploads the
          emoji and starts its weekly rental (the escrow already paid week
          one); denying refunds in full. A lapsed rental takes the emoji down
          automatically.
        </div>
        <div data-emoji-queue></div>
        <div data-emoji-empty class="field-hint" style="display:none;">Nothing waiting.</div>
      </section>

      <section class="form card" style="margin-top:1.5rem;">
        <div class="section-label">Rentable Icon Catalog</div>
        <div class="field-hint" style="margin-bottom:1rem;">
          Curated role icons members rent from <code>/bank shop</code>, each with its own
          weekly price. Renting bills the icon's price; a price change applies at the
          renter's next weekly renewal. An icon that members are currently renting can't be
          deleted — disable it instead, and current renters keep it. Images are downscaled to
          a small PNG (Discord caps role icons at 256&nbsp;KB); requires the server to have
          the Role Icons feature.
        </div>

        <div data-catalog></div>
        <div data-catalog-empty class="field-hint" style="display:none;">
          No catalog icons yet — add one below.
        </div>

        <div style="margin-top:1.25rem;padding-top:1rem;border-top:1px solid var(--border,#333);">
          <div class="section-label">Add an Icon</div>
          <div class="field-row" style="flex-wrap:wrap;align-items:flex-end;">
            <div class="field">
              <label>Name</label>
              <input type="text" data-add-name maxlength="64" placeholder="e.g. Golden crown" style="max-width:200px;" />
            </div>
            <div class="field">
              <label>Price / week</label>
              <input type="number" data-add-price min="0" step="1" value="75" style="max-width:120px;" />
            </div>
            <div class="field">
              <label>Image (PNG/WEBP)</label>
              <input type="file" data-add-file accept="image/png,image/webp,image/jpeg,image/gif" />
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
        <button type="button" class="btn btn-primary" data-approve>Approve &amp; upload</button>
        <button type="button" class="btn btn-danger" data-deny>Deny</button>
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
        const reason = prompt("Reason (sent to the member):");
        if (reason === null) { btn.disabled = false; return; }
        await apiPost(`/api/economy/emoji-submissions/${id}/deny`, {
          reason: reason.trim() || "not a fit for the server",
        });
        showStatus(rowStatus, true, "Denied & refunded");
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
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {};
    for (const [key] of ALL_NUM_FIELDS) {
      payload[key] = parseInt(form.querySelector(`[name=${key}]`).value, 10);
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
      btn.disabled = true;
      try {
        await request("PATCH", `/api/economy/icon-catalog/${id}`, {
          body: {
            name: row.querySelector("[data-name]").value.trim(),
            price: parseInt(row.querySelector("[data-price]").value, 10),
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
    if (!name.value.trim()) { showStatus(addStatus, false, "Name required"); return; }
    if (!file.files.length) { showStatus(addStatus, false, "Pick an image"); return; }
    const fd = new FormData();
    fd.append("name", name.value.trim());
    fd.append("price", parseInt(price.value, 10) || 0);
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
