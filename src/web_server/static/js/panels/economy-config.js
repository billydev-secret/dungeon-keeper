import { api } from "../api.js";
import {
  apiPut,
  showStatus,
  loadChannels,
  loadRoles,
  loadMembers,
  toMemberOptions,
  mountPicker,
  mountChannelPicker,
  mountRolePicker,
} from "../config-helpers.js";

// Faucet rates are edited on the Income Sources page and perk-shop prices on the
// Sinks page — this page keeps the wiring and branding.

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading economy config…</div></div>`;

  (async () => {
    const [cfg, channels, roles, members] = await Promise.all([
      api("/api/economy/config"),
      loadChannels(),
      loadRoles(),
      loadMembers(),
    ]);
    render(container, cfg, channels, roles, members);
  })();
}

function render(container, cfg, channels, roles, members) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Economy Settings</h2>
        <div class="subtitle">Wiring and branding — perk-shop prices live on
          <a href="#/economy-sinks">Sinks</a>, faucet rates on
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
          <label>Register channel</label>
          <span data-picker="register_channel_id"></span>
          <div class="field-hint">A running feed of every currency movement —
            quest payouts, perk purchases, transfers and grants — each entry
            saying what it was for. Leave unset to turn the feed off. Switching
            it on starts from now; past transactions are not replayed.</div>
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
        <div class="field">
          <label>Community weekly host</label>
          <span data-picker="community_host_user_id"></span>
          <div class="field-hint">Community-weekly beat sheets (kickoff, tier
            crossed, final-24h, resolution) are DMed to this member to post in
            their own voice — the bot posts nothing publicly. Leave empty to
            DM the server owner.</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label>Daily set bonus</label>
            <input type="number" name="quest_set_bonus_daily" min="0" step="1"
              value="${cfg.quest_set_bonus_daily}" style="max-width:120px;" />
          </div>
          <div class="field">
            <label>Weekly set bonus</label>
            <input type="number" name="quest_set_bonus_weekly" min="0" step="1"
              value="${cfg.quest_set_bonus_weekly}" style="max-width:120px;" />
          </div>
        </div>
        <div class="field-hint" style="margin-top:-6px;">Extra coins for
          completing EVERY quest on your personal board of that cadence in one
          period. 0 turns the bonus off.</div>
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
  const registerChannelPicker = mountChannelPicker(
    form.querySelector('[data-picker="register_channel_id"]'),
    channels,
    String(cfg.register_channel_id),
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
  const hostPicker = mountPicker(
    form.querySelector('[data-picker="community_host_user_id"]'),
    toMemberOptions(members),
    String(cfg.community_host_user_id || "0"),
    { emptyValue: "0", emptyLabel: "(server owner)", placeholder: "Search members…" },
  );

  const numKeys = [
    "booster_multiplier",
    "quest_set_bonus_daily",
    "quest_set_bonus_weekly",
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
      register_channel_id: parseInt(registerChannelPicker.getValue() || "0", 10),
      manager_role_id: parseInt(rolePicker.getValue() || "0", 10),
      game_role_id: parseInt(gameRolePicker.getValue() || "0", 10),
      // Sent as a string: a snowflake overflows JS number precision, and
      // pydantic coerces the string to int losslessly server-side.
      community_host_user_id: hostPicker.getValue() || "0",
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
