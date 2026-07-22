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
          <label>Notifications role</label>
          <span data-picker="game_role_id"></span>
          <div class="field-hint">The opt-in role members toggle with the 🔔 button
            on the how-it-works panel. It only controls DMs — holders get quest
            completions and streak milestones in their DMs instead of an
            in-channel reply, and are the only ones sent recurring economy
            notices. It gates no channel and no payout, so don't use it for
            channel permissions. Leave unset to reply in-channel for everyone and
            send no recurring DMs.</div>
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

        <div class="section-label">Coin Drops</div>
        <div class="field">
          <label>Drop channel</label>
          <span data-picker="drops_channel_id"></span>
          <div class="field-hint">The bot drops a pouch of coins here at random
            moments; the first member to press the drop's <em>Claim</em>
            button collects it. Leave unset to turn drops off. Drops wait
            for conversation — nothing lands while the channel is silent or
            mid-game.</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label>Min coins</label>
            <input type="number" name="drops_min_coins" min="0" step="1"
              value="${cfg.drops_min_coins}" style="max-width:120px;" />
          </div>
          <div class="field">
            <label>Max coins</label>
            <input type="number" name="drops_max_coins" min="0" step="1"
              value="${cfg.drops_max_coins}" style="max-width:120px;" />
          </div>
        </div>
        <div class="field-row">
          <div class="field">
            <label>Drops per day (average)</label>
            <input type="number" name="drops_per_day" min="0" max="48" step="1"
              value="${cfg.drops_per_day}" style="max-width:120px;" />
          </div>
          <div class="field">
            <label>Expire after (minutes)</label>
            <input type="number" name="drops_expire_minutes" min="1" step="1"
              value="${cfg.drops_expire_minutes}" style="max-width:120px;" />
          </div>
        </div>
        <div class="field-hint" style="margin-top:-6px;">Each pouch rolls a
          random amount between min and max. The daily count is an average —
          timing is jittered so members can't clock it. An unclaimed pouch
          vanishes after the expiry window and pays nobody.</div>

        <div class="section-label">Pin of the Day</div>
        <div class="field">
          <label>Pin channel</label>
          <span data-picker="pin_channel_id"></span>
          <div class="field-hint">A member pays (set the price on the Sinks page)
            to pin a short message here; a mod approves it first, then the bot
            pins a card for 24 hours before auto-unpinning. Needs both a channel
            AND a price &gt; 0 to switch on — it's a public sink, so announce it
            before flipping it on. The bot needs Manage Messages here to pin.
            Leave unset to keep it off.</div>
        </div>

        <div class="section-label">Community Bounty</div>
        <div class="field">
          <label>Bounty board channel</label>
          <span data-picker="bounty_channel_id"></span>
          <div class="field-hint">Where <code>/bounty</code> posts a card per
            bounty. Anyone chips coins into a bounty's pot; a mod awards it to
            whoever completed it (minus the bounty rake, set on the Sinks page),
            or cancels it to refund everyone. Unclaimed bounties expire and
            refund automatically. Leave unset to turn bounties off.</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label>Min stake</label>
            <input type="number" name="bounty_min_stake" min="1" step="1"
              value="${cfg.bounty_min_stake}" style="max-width:120px;" />
          </div>
          <div class="field">
            <label>Max open / member</label>
            <input type="number" name="bounty_max_open" min="0" step="1"
              value="${cfg.bounty_max_open}" style="max-width:120px;" />
          </div>
          <div class="field">
            <label>Expire after (days)</label>
            <input type="number" name="bounty_expire_days" min="0" step="1"
              value="${cfg.bounty_expire_days}" style="max-width:120px;" />
          </div>
        </div>
        <div class="field-hint" style="margin-top:-6px;">Min stake is the floor
          for the opener and each chip-in. Max open caps how many live bounties
          one member can have posted at once (0 = no cap). A bounty nobody awards
          within the expiry window refunds every contributor (0 = never expires).</div>

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
  const dropsChannelPicker = mountChannelPicker(
    form.querySelector('[data-picker="drops_channel_id"]'),
    channels,
    String(cfg.drops_channel_id),
  );
  const pinChannelPicker = mountChannelPicker(
    form.querySelector('[data-picker="pin_channel_id"]'),
    channels,
    String(cfg.pin_channel_id),
  );
  const bountyChannelPicker = mountChannelPicker(
    form.querySelector('[data-picker="bounty_channel_id"]'),
    channels,
    String(cfg.bounty_channel_id),
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
    "drops_min_coins",
    "drops_max_coins",
    "drops_per_day",
    "drops_expire_minutes",
    "bounty_min_stake",
    "bounty_max_open",
    "bounty_expire_days",
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
      // All snowflakes go as strings: parseInt on a 19-digit id silently
      // rounds it (parseInt("1526051848518373608") === 1526051848518373600),
      // which repoints the setting at a role/channel that doesn't exist.
      // Pydantic coerces the string to int losslessly server-side.
      bank_channel_id: channelPicker.getValue() || "0",
      register_channel_id: registerChannelPicker.getValue() || "0",
      drops_channel_id: dropsChannelPicker.getValue() || "0",
      pin_channel_id: pinChannelPicker.getValue() || "0",
      bounty_channel_id: bountyChannelPicker.getValue() || "0",
      manager_role_id: rolePicker.getValue() || "0",
      game_role_id: gameRolePicker.getValue() || "0",
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
