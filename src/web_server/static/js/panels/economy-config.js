import { api } from "../api.js";
import {
  apiPut,
  showStatus,
  loadChannels,
  loadRoles,
  mountChannelPicker,
  mountRolePicker,
} from "../config-helpers.js";

// Numeric fields grouped by section. Each entry: [key, label, {min, step, hint}].
// Faucet rates are edited on the Income Sources page (manager-visible,
// admin-editable) — this page keeps the wiring, branding, and prices.
const PRICE_FIELDS = [
  ["price_role_color", "Role colour", { min: 0 }],
  ["price_role_name", "Role name", { min: 0 }],
  ["price_role_icon", "Role icon", { min: 0 }],
  ["price_role_gradient", "Role gradient", { min: 0 }],
  ["price_text_room", "Text room", { min: 0, hint: "Used by a later stage." }],
  ["price_voice_room", "Voice room", { min: 0, hint: "Used by a later stage." }],
  ["price_gift_color", "Gift colour", { min: 0, hint: "Used by a later stage." }],
];

function numField(key, label, cfg, { min = 0, step = 1, hint } = {}, pricing = null) {
  const hintHtml = hint ? `<div class="field-hint">${hint}</div>` : "";
  // Advisory pricing suggestion, appended as its own muted node so the
  // existing "later stage" hint is preserved. Only price_* fields carry hints.
  const suggested = pricing && pricing.hints ? pricing.hints[key] : null;
  const median = pricing ? Math.round(pricing.median || 0) : 0;
  const suggest = suggested != null
    ? `<div class="field-hint" data-suggest="${key}">suggested ≈ ${suggested} (from median weekly income ${median})</div>`
    : "";
  return `
    <div class="field">
      <label>${label}</label>
      <input type="number" name="${key}" value="${cfg[key]}" min="${min}" step="${step}" style="max-width:140px;" />
      ${hintHtml}
      ${suggest}
    </div>`;
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading economy config…</div></div>`;

  (async () => {
    const [cfg, channels, roles, metrics] = await Promise.all([
      api("/api/economy/config"),
      loadChannels(),
      loadRoles(),
      api("/api/economy/metrics").catch(() => null),
    ]);
    // Pricing hints are advisory: only render when the first rollup exists
    // (hints is {} otherwise). median feeds the "from median weekly income" note.
    const pricing = metrics && metrics.hints && Object.keys(metrics.hints).length
      ? { hints: metrics.hints, median: metrics.median_income }
      : null;
    render(container, cfg, channels, roles, pricing);
  })();
}

function render(container, cfg, channels, roles, pricing) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Economy Settings</h2>
        <div class="subtitle">Wiring, branding, and perk prices — faucet rates live on
          <a href="#/economy-income-sources">Income Sources</a></div>
      </header>

      <form class="form card" data-form>
        <div class="section-label">Core</div>
        <label style="display:flex; gap:6px; align-items:center; margin:8px 0;">
          <input type="checkbox" name="enabled"${cfg.enabled ? " checked" : ""} />
          Economy enabled
        </label>
        <div class="field">
          <label>Bank channel</label>
          <span data-picker="bank_channel_id"></span>
        </div>
        <div class="field">
          <label>Manager role</label>
          <span data-picker="manager_role_id"></span>
        </div>
        <div class="field">
          <label>Game role</label>
          <span data-picker="game_role_id"></span>
          <div class="field-hint">When set, auto-claimed quest completions DM the
            player their card instead of replying in the channel; members without
            the role are paid silently. Leave unset to reply in-channel for everyone.</div>
        </div>
        <label style="display:flex; gap:6px; align-items:center; margin:8px 0;">
          <input type="checkbox" name="transfers_enabled"${cfg.transfers_enabled ? " checked" : ""} />
          Member-to-member transfers enabled
        </label>
        <div class="field">
          <label>Booster multiplier</label>
          <input type="number" name="booster_multiplier" value="${cfg.booster_multiplier}" min="1" step="0.1" style="max-width:140px;" />
          <div class="field-hint">Applied to faucet credits for server boosters (≥ 1).</div>
        </div>

        <div class="section-label">Branding</div>
        <div class="field-row">
          <div class="field">
            <label>Currency name</label>
            <input type="text" name="currency_name" value="${cfg.currency_name}" maxlength="32" />
          </div>
          <div class="field">
            <label>Currency plural</label>
            <input type="text" name="currency_plural" value="${cfg.currency_plural}" maxlength="32" />
          </div>
        </div>
        <div class="field-row">
          <div class="field">
            <label>Currency emoji</label>
            <input type="text" name="currency_emoji" value="${cfg.currency_emoji}" maxlength="64" />
          </div>
          <div class="field">
            <label>Wallet name</label>
            <input type="text" name="wallet_name" value="${cfg.wallet_name}" maxlength="32" />
          </div>
        </div>
        <div class="field">
          <label>Currency icon URL</label>
          <input type="text" name="currency_icon_url" value="${cfg.currency_icon_url}" maxlength="512" />
        </div>

        <div class="section-label">Perk prices</div>
        <div class="field-row" style="flex-wrap:wrap;">
          ${PRICE_FIELDS.map(([k, l, o]) => numField(k, l, cfg, o, pricing)).join("")}
        </div>

        <div style="display:flex; gap:8px; align-items:center; margin-top:16px;">
          <button type="submit" class="btn btn-primary">Save</button>
          <span data-status></span>
        </div>
      </form>
    </div>`;

  const form = container.querySelector("[data-form]");
  const status = form.querySelector("[data-status]");

  const channelPicker = mountChannelPicker(
    form.querySelector('[data-picker="bank_channel_id"]'),
    channels,
    String(cfg.bank_channel_id),
  );
  const rolePicker = mountRolePicker(
    form.querySelector('[data-picker="manager_role_id"]'),
    roles,
    String(cfg.manager_role_id),
  );
  const gameRolePicker = mountRolePicker(
    form.querySelector('[data-picker="game_role_id"]'),
    roles,
    String(cfg.game_role_id),
  );

  const numKeys = [
    "booster_multiplier",
    ...PRICE_FIELDS.map(([k]) => k),
  ];
  const floatKeys = new Set(["booster_multiplier"]);
  const strKeys = [
    "currency_name",
    "currency_plural",
    "currency_emoji",
    "currency_icon_url",
    "wallet_name",
  ];

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
      enabled: form.querySelector("[name=enabled]").checked,
      transfers_enabled: form.querySelector("[name=transfers_enabled]").checked,
      bank_channel_id: parseInt(channelPicker.getValue() || "0", 10),
      manager_role_id: parseInt(rolePicker.getValue() || "0", 10),
      game_role_id: parseInt(gameRolePicker.getValue() || "0", 10),
    };
    for (const key of numKeys) {
      const raw = form.querySelector(`[name=${key}]`).value;
      payload[key] = floatKeys.has(key) ? parseFloat(raw) : parseInt(raw, 10);
    }
    for (const key of strKeys) {
      payload[key] = form.querySelector(`[name=${key}]`).value;
    }
    try {
      await apiPut("/api/economy/config", payload);
      showStatus(status, true);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}
