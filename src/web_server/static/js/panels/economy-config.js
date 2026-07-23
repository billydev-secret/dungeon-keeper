import { api } from "../api.js";
import {
  apiPut,
  showStatus,
  guardForm,
  renderMetaWarning,
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
  container.innerHTML = `<div class="panel"><div class="empty">Loading configuration…</div></div>`;

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
        <div class="subtitle">Where the economy lives and what the currency is called.
          Prices are set on <a href="#/economy-sinks">Sinks</a> and earnings on
          <a href="#/economy-income-sources">Income Sources</a>.</div>
      </header>
      ${renderMetaWarning()}

      <form class="form form-cards" data-form>
        <div class="card">
        <div class="section-label">Core</div>
        <div class="field">
          <label style="display:flex; gap:6px; align-items:center;">
            <input type="checkbox" name="enabled"${cfg.enabled ? " checked" : ""} />
            Run an economy on this server
          </label>
          <div class="field-hint">The master switch. Unchecked, nobody earns or spends
            anything and every economy command goes quiet — balances are kept, not
            wiped, so switching it back on picks up where you left off.</div>
        </div>
        <div class="field">
          <label>Bank Channel</label>
          <span data-picker="bank_channel_id"></span>
          <div class="field-hint">Home of the how-it-works panel members use to check
            their balance and open the shop. Leave unset and members have to use the
            slash commands instead.</div>
        </div>
        <div class="field">
          <label>Register Channel</label>
          <span data-picker="register_channel_id"></span>
          <div class="field-hint">A running feed of every currency movement —
            quest payouts, perk purchases, transfers and grants — each entry
            saying what it was for. Leave unset to turn the feed off. Switching
            it on starts from now; past transactions are not replayed.</div>
        </div>
        <div class="field">
          <label>Manager Role</label>
          <span data-picker="manager_role_id"></span>
          <div class="field-hint">Members with this role can grant and remove currency
            and approve paid requests. That is real spending power — keep the list of
            holders short. "(none)" leaves those powers to admins.</div>
        </div>
        <div class="field">
          <label>Notifications Role</label>
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
          <label>Community Weekly Host</label>
          <span data-picker="community_host_user_id"></span>
          <div class="field-hint">Community-weekly beat sheets (kickoff, tier
            crossed, final-24h, resolution) are DMed to this member to post in
            their own voice — the bot posts nothing publicly. Leave empty to
            DM the server owner.</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="ec-set-daily">Daily Clean-Sweep Bonus</label>
            <input type="number" name="quest_set_bonus_daily" id="ec-set-daily" required
              min="0" max="1000000" step="1"
              value="${cfg.quest_set_bonus_daily}" style="max-width:120px;" />
          </div>
          <div class="field">
            <label for="ec-set-weekly">Weekly Clean-Sweep Bonus</label>
            <input type="number" name="quest_set_bonus_weekly" id="ec-set-weekly" required
              min="0" max="1000000" step="1"
              value="${cfg.quest_set_bonus_weekly}" style="max-width:120px;" />
          </div>
        </div>
        <div class="field-hint" style="margin-top:-6px;">Extra coins paid to a member
          who finishes <em>every</em> quest on their personal board for that period.
          0 turns the bonus off.</div>
        <div class="field">
          <label style="display:flex; gap:6px; align-items:center;">
            <input type="checkbox" name="transfers_enabled"${cfg.transfers_enabled ? " checked" : ""} />
            Let members send currency to each other
          </label>
          <div class="field-hint">When checked, members can hand coins to one another
            directly. Unchecked, currency only moves through payouts and purchases,
            which makes it much harder to buy or trade favors.</div>
        </div>
        <div class="field">
          <label for="ec-booster">Booster Multiplier</label>
          <input type="number" name="booster_multiplier" id="ec-booster" required
            value="${cfg.booster_multiplier}" min="1" max="10" step="0.1" style="max-width:140px;" />
          <div class="field-hint">Everything a server booster earns is multiplied by
            this. 1 means boosters earn the same as everyone else; 1.5 means they earn
            half as much again.</div>
        </div>

        </div>

        <div class="card">
        <div class="section-label">Coin Drops</div>
        <div class="field">
          <label>Drop Channel</label>
          <span data-picker="drops_channel_id"></span>
          <div class="field-hint">The bot drops a pouch of coins here at random
            moments; the first member to press the drop's <em>Claim</em>
            button collects it. Leave unset to turn drops off. Drops wait
            for conversation — nothing lands while the channel is silent or
            mid-game.</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="ec-drop-min">Smallest Drop (coins)</label>
            <input type="number" name="drops_min_coins" id="ec-drop-min" required
              min="0" max="1000000" step="1"
              value="${cfg.drops_min_coins}" style="max-width:120px;" />
          </div>
          <div class="field">
            <label for="ec-drop-max">Largest Drop (coins)</label>
            <input type="number" name="drops_max_coins" id="ec-drop-max" required
              min="0" max="1000000" step="1"
              value="${cfg.drops_max_coins}" style="max-width:120px;" />
          </div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="ec-drop-per-day">Drops Per Day (average)</label>
            <input type="number" name="drops_per_day" id="ec-drop-per-day" required
              min="0" max="48" step="1"
              value="${cfg.drops_per_day}" style="max-width:120px;" />
          </div>
          <div class="field">
            <label for="ec-drop-expire">Expires After (minutes)</label>
            <input type="number" name="drops_expire_minutes" id="ec-drop-expire" required
              min="1" max="1440" step="1"
              value="${cfg.drops_expire_minutes}" style="max-width:120px;" />
          </div>
        </div>
        <div class="field-hint" style="margin-top:-6px;">Each pouch is worth a random
          amount between the smallest and largest figures. The daily count is an
          average and the timing is deliberately uneven, so nobody can sit and wait for
          the next one. A pouch nobody claims before it expires simply vanishes and
          pays out nothing.</div>

        </div>

        <div class="card">
        <div class="section-label">Pin of the Day</div>
        <div class="field">
          <label>Pin Channel</label>
          <span data-picker="pin_channel_id"></span>
          <div class="field-hint">A member pays (set the price on the Sinks page)
            to pin a short message here; a mod approves it first, then the bot
            pins a card for 24 hours before auto-unpinning. Needs both a channel
            AND a price &gt; 0 to switch on — it's a public sink, so announce it
            before flipping it on. The bot needs Manage Messages here to pin.
            Leave unset to keep it off.</div>
        </div>

        </div>

        <div class="card">
        <div class="section-label">Community Bounty</div>
        <div class="field">
          <label>Bounty Board Channel</label>
          <span data-picker="bounty_channel_id"></span>
          <div class="field-hint">Where <code>/bounty</code> posts a card per
            bounty. Anyone chips coins into a bounty's pot; a mod awards it to
            whoever completed it (minus the bounty rake, set on the Sinks page),
            or cancels it to refund everyone. Unclaimed bounties expire and
            refund automatically. Leave unset to turn bounties off.</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="ec-bounty-min">Smallest Contribution (coins)</label>
            <input type="number" name="bounty_min_stake" id="ec-bounty-min" required
              min="1" max="1000000" step="1"
              value="${cfg.bounty_min_stake}" style="max-width:120px;" />
          </div>
          <div class="field">
            <label for="ec-bounty-open">Open Bounties Per Member</label>
            <input type="number" name="bounty_max_open" id="ec-bounty-open" required
              min="0" max="1000" step="1"
              value="${cfg.bounty_max_open}" style="max-width:120px;" />
          </div>
          <div class="field">
            <label for="ec-bounty-expire">Expires After (days)</label>
            <input type="number" name="bounty_expire_days" id="ec-bounty-expire" required
              min="0" max="365" step="1"
              value="${cfg.bounty_expire_days}" style="max-width:120px;" />
          </div>
        </div>
        <div class="field-hint" style="margin-top:-6px;">The smallest contribution
          applies both to whoever opens a bounty and to everyone who chips in
          afterwards. The per-member limit caps how many live bounties one person can
          have posted at once; 0 means no limit. A bounty nobody has awarded by the
          time it expires refunds every contributor in full; 0 means bounties never
          expire.</div>
        </div>

        <div class="card">
        <div class="section-label">Branding</div>
        <div class="field-row">
          <div class="field">
            <label for="ec-cur-name">Currency Name (one)</label>
            <input type="text" name="currency_name" id="ec-cur-name" value="${cfg.currency_name}" maxlength="32" placeholder="e.g. coin" />
          </div>
          <div class="field">
            <label for="ec-cur-plural">Currency Name (many)</label>
            <input type="text" name="currency_plural" id="ec-cur-plural" value="${cfg.currency_plural}" maxlength="32" placeholder="e.g. coins" />
          </div>
        </div>
        <div class="field-hint" style="margin-top:-6px;">These two names appear in every
          balance, price, and payout message members see.</div>
        <div class="field-row">
          <div class="field">
            <label for="ec-cur-emoji">Currency Emoji</label>
            <input type="text" name="currency_emoji" id="ec-cur-emoji" value="${cfg.currency_emoji}" maxlength="64" />
            <div class="field-hint">Shown next to every amount. A standard emoji or one
              of this server's custom emojis.</div>
          </div>
          <div class="field">
            <label for="ec-wallet">Wallet Name</label>
            <input type="text" name="wallet_name" id="ec-wallet" value="${cfg.wallet_name}" maxlength="32" placeholder="e.g. wallet" />
            <div class="field-hint">What members' balance is called — "wallet", "purse",
              "vault", whatever suits your server.</div>
          </div>
        </div>
        <div class="field">
          <label for="ec-icon-url">Currency Icon Address</label>
          <input type="text" name="currency_icon_url" id="ec-icon-url" value="${cfg.currency_icon_url}" maxlength="512" placeholder="https://example.com/coin.png" />
          <div class="field-hint">A full web address (starting with https://) of a small
            image used as the thumbnail on economy cards. Leave empty for no image. An
            address that stops working leaves those cards without a picture.</div>
        </div>
        </div>

        <div style="display:flex; gap:8px; align-items:center;">
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
    { label: "Bank Channel" },
  );
  const registerChannelPicker = mountChannelPicker(
    form.querySelector('[data-picker="register_channel_id"]'),
    channels,
    String(cfg.register_channel_id),
    { label: "Register Channel" },
  );
  const rolePicker = mountRolePicker(
    form.querySelector('[data-picker="manager_role_id"]'),
    roles,
    String(cfg.manager_role_id),
    { label: "Manager Role" },
  );
  const gameRolePicker = mountRolePicker(
    form.querySelector('[data-picker="game_role_id"]'),
    roles,
    String(cfg.game_role_id),
    { label: "Notifications Role" },
  );
  const dropsChannelPicker = mountChannelPicker(
    form.querySelector('[data-picker="drops_channel_id"]'),
    channels,
    String(cfg.drops_channel_id),
    { label: "Drop Channel" },
  );
  const pinChannelPicker = mountChannelPicker(
    form.querySelector('[data-picker="pin_channel_id"]'),
    channels,
    String(cfg.pin_channel_id),
    { label: "Pin Channel" },
  );
  const bountyChannelPicker = mountChannelPicker(
    form.querySelector('[data-picker="bounty_channel_id"]'),
    channels,
    String(cfg.bounty_channel_id),
    { label: "Bounty Board Channel" },
  );
  const hostPicker = mountPicker(
    form.querySelector('[data-picker="community_host_user_id"]'),
    toMemberOptions(members),
    String(cfg.community_host_user_id || "0"),
    {
      emptyValue: "0",
      emptyLabel: "(server owner)",
      placeholder: "Search members…",
      label: "Community Weekly Host",
    },
  );

  guardForm(form);

  // [name, visible label, min, max] — a blank box used to post NaN and come
  // back as a raw 422 naming no field (W-C5).
  const numKeys = [
    ["booster_multiplier", "Booster Multiplier", 1, 10],
    ["quest_set_bonus_daily", "Daily Clean-Sweep Bonus", 0, 1000000],
    ["quest_set_bonus_weekly", "Weekly Clean-Sweep Bonus", 0, 1000000],
    ["drops_min_coins", "Smallest Drop", 0, 1000000],
    ["drops_max_coins", "Largest Drop", 0, 1000000],
    ["drops_per_day", "Drops Per Day", 0, 48],
    ["drops_expire_minutes", "Expires After (drops)", 1, 1440],
    ["bounty_min_stake", "Smallest Contribution", 1, 1000000],
    ["bounty_max_open", "Open Bounties Per Member", 0, 1000],
    ["bounty_expire_days", "Expires After (bounties)", 0, 365],
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
    for (const [key, label, min, max] of numKeys) {
      const input = form.querySelector(`[name=${key}]`);
      const n = floatKeys.has(key) ? parseFloat(input.value) : parseInt(input.value, 10);
      if (!Number.isFinite(n) || n < min || n > max) {
        showStatus(status, false, `${label} must be a number from ${min} to ${max}`);
        input.focus();
        return;
      }
      payload[key] = n;
    }
    if (payload.drops_max_coins < payload.drops_min_coins) {
      showStatus(status, false, "Largest Drop cannot be smaller than Smallest Drop");
      form.querySelector("[name=drops_max_coins]").focus();
      return;
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
