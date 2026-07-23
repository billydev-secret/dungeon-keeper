// Income Sources — everything that pays coins, on one page: per-guild enable
// switches for the custom-coded quest trigger hooks, the built-in faucet
// rates (editable in place for admins, read-only for manager-role holders —
// the save endpoint is admin-gated), and the roadmap of suggested sources.
// Gated by the economy manager role (or admin).
import { api, apiPut, esc } from "../api.js";
import { showStatus, guardForm } from "../config-helpers.js";
import { KIND_LABELS } from "./economy-sources-shared.js";

// Order matters — rendered top to bottom. xp_per_coin is the one float knob.
// Each entry: [key, label, max].
const FAUCET_FIELDS = [
  ["login_text_base", "First message of the day", 1000000],
  ["login_voice_base", "First voice call of the day", 1000000],
  ["streak_bonus_cap", "Most a daily streak can add", 1000000],
  ["milestone_day7", "Reaching a 7-day streak", 1000000],
  ["milestone_day30", "Reaching a 30-day streak", 1000000],
  ["milestone_day100", "Reaching a 100-day streak", 1000000],
  ["milestone_per_100", "Every further 100 days", 1000000],
  ["reward_qotd", "Answering the question of the day", 1000000],
  ["reward_game_participation", "Playing a game", 1000000],
  ["reward_game_win", "Winning a game (on top of playing)", 1000000],
  ["reward_photo_post", "Entering the photo challenge", 1000000],
  ["xp_per_coin", "XP needed to earn one coin", 100000],
];
const FLOAT_FAUCETS = new Set(["xp_per_coin"]);

// Not built yet — shown so managers can see what's on the table. Keep in
// sync with the parking lot in docs/economy_spec.md.
const SUGGESTIONS = [
  ["🔔 Server bump", "Reward whoever runs /bump. Needs attribution from the detector-bot message (message.interaction_metadata) before it can ship."],
  ["📊 Survey completion", "The survey spec is aspirational — there is no survey feature in the code yet to hook."],
  ["🤝 Invite retention", "Pay the invite source only after the invitee survives the prune window (the current invite source pays on join)."],
  ["🔥 Streak milestones", "A trigger kind firing on login-streak milestone days, so streaks can be quest-ified beyond the flat milestone payouts."],
];

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading income sources…</div></div>`;
  refresh(container);
  return null;
}

async function refresh(container) {
  // Admin probe: the config GET is admin-gated, so a success means this user
  // may edit faucet rates in place (saves go to the same admin-gated PUT).
  // Manager-role holders get a 403 and the read-only view. The same response
  // carries the economy master switch used for the "economy is off" banner.
  const [dataResult, cfgResult] = await Promise.allSettled([
    api("/api/economy/income-sources"),
    api("/api/economy/config"),
  ]);
  if (dataResult.status === "rejected") {
    container.innerHTML = `<div class="panel"><div class="error">Income sources failed to load: ${esc(dataResult.reason.message)}</div></div>`;
    return;
  }
  const isAdmin = cfgResult.status === "fulfilled";
  // Managers cannot read the master switch; only warn when we actually know
  // the economy is off.
  const economyOff = isAdmin && !cfgResult.value.enabled;
  render(container, dataResult.value, isAdmin, economyOff);
}

function questBadges(quests) {
  if (!quests.length) return `<span class="field-hint">No quest uses this yet.</span>`;
  return quests.map((q) =>
    `<span class="badge${q.active ? "" : " badge-dim"}" title="${esc(q.qtype)}${q.active ? "" : " (inactive)"}">${esc(q.title)}</span>`
  ).join(" ");
}

function faucetSection(data, isAdmin) {
  if (isAdmin) {
    const rows = FAUCET_FIELDS.map(([key, label, max]) => `
      <tr>
        <td><label for="inc-${key}">${esc(label)}</label></td>
        <td style="text-align:right;">
          <input type="number" name="${key}" id="inc-${key}" required
                 value="${data.faucets[key] ?? 0}"
                 min="0" max="${max}" step="${FLOAT_FAUCETS.has(key) ? "0.5" : "1"}"
                 style="max-width:110px; text-align:right;" />
        </td>
      </tr>`).join("");
    return `
      <div class="field-hint" style="margin-bottom:8px;">
        These pay out by themselves, with no quest involved. Set any of them to 0 and
        that payment stops. "XP needed to earn one coin" works the other way around —
        the higher it is, the harder coins are to earn, and 0 turns XP-to-coin
        conversion off entirely. Everything else about the economy lives on
        <a href="#/economy-config">Economy Settings</a>.
      </div>
      <form data-form-faucets>
        <div style="overflow-x:auto;">
          <table class="data-table" style="max-width:560px;">
            <thead><tr><th>Members Are Paid For</th><th style="text-align:right;">Coins</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
        <div style="display:flex; gap:8px; align-items:center; margin-top:10px;">
          <button type="submit" class="btn btn-primary">Save</button>
          <span data-status-faucets></span>
        </div>
      </form>`;
  }
  const rows = FAUCET_FIELDS.map(([key, label]) => `
    <tr><td>${esc(label)}</td><td style="text-align:right;">${data.faucets[key] ?? "—"}</td></tr>`).join("");
  return `
    <div class="field-hint" style="margin-bottom:8px;">
      These pay out by themselves, with no quest involved. A rate of 0 means that
      payment is switched off. Only an admin can change these figures.
    </div>
    <div style="overflow-x:auto;">
      <table class="data-table" style="max-width:560px;">
        <thead><tr><th>Members Are Paid For</th><th style="text-align:right;">Coins</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function render(container, data, isAdmin, economyOff) {
  const rows = data.sources.map((s) => `
    <tr data-source-row="${esc(s.source)}">
      <td style="white-space:nowrap;">${esc(KIND_LABELS[s.source] || s.label)}</td>
      <td style="max-width:420px;">${esc(s.info)}<div style="margin-top:3px;">${questBadges(s.quests)}</div></td>
      <td>
        <label style="display:inline-flex; gap:5px; align-items:center; cursor:pointer;">
          <input type="checkbox" data-source-toggle="${esc(s.source)}"${s.enabled ? " checked" : ""} /> Paying out
        </label>
        <span class="save-status" data-source-status="${esc(s.source)}"></span>
      </td>
    </tr>`).join("");

  const suggestions = SUGGESTIONS.map(([label, note]) => `
    <div style="margin:8px 0;">
      <strong>${esc(label)}</strong>
      <div class="field-hint">${esc(note)}</div>
    </div>`).join("");

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Income Sources</h2>
        <div class="subtitle">Every way members earn currency — the events quests can
          hook into, and the payments that happen on their own</div>
      </header>
      ${economyOff ? `<div class="empty" role="status" style="margin-bottom:12px;">
        The economy is currently off, so nothing below takes effect until it is switched
        on under <a href="#/economy-config">Economy Settings</a>.</div>` : ""}

      <section class="card">
        <div class="section-label">Events Quests Can Use</div>
        <div class="field-hint" style="margin-bottom:8px;">
          Clearing a checkbox stops that event counting straight away. Quests built on
          it stay in your library and simply wait. To hang a quest off one of these, go
          to the <a href="#/economy-quests">Quests</a> page and choose the "Playing a
          game" completion mode. Daily and weekly quests count once per period, while
          event quests pay out every single time.
        </div>
        <div style="overflow-x:auto;">
          <table class="data-table">
            <thead><tr><th>Event</th><th>Happens When</th><th>Counting</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </section>

      <section class="card">
        <div class="section-label">Automatic Payments</div>
        ${faucetSection(data, isAdmin)}
      </section>

      <section class="card">
        <div class="section-label">Coin Drops</div>
        <div class="field-hint">Dungeon Keeper drops a pouch of coins into a chosen
          channel at unpredictable moments, and the first member to press its Claim
          button keeps it. A pouch nobody claims expires and pays nothing. The channel,
          the amounts, how often drops happen, and how long they last are all set on
          <a href="#/economy-config">Economy Settings</a>, which needs admin access.</div>
      </section>

      <section class="card">
        <div class="section-label">Ideas Not Built Yet</div>
        <div class="field-hint" style="margin-bottom:8px;">Nothing here works yet — it is
          listed so you can see what is being considered.</div>
        ${suggestions}
      </section>
    </div>`;

  container.querySelectorAll("[data-source-toggle]").forEach((cb) => {
    cb.addEventListener("change", async () => {
      const source = cb.dataset.sourceToggle;
      const status = container.querySelector(`[data-source-status="${source}"]`);
      try {
        await apiPut(`/api/economy/income-sources/${encodeURIComponent(source)}`, {
          enabled: cb.checked,
        });
        showStatus(status, true, cb.checked ? "Counting" : "Stopped");
      } catch (err) {
        cb.checked = !cb.checked; // revert
        showStatus(status, false, err.message);
      }
    });
  });

  const faucetForm = container.querySelector("[data-form-faucets]");
  if (faucetForm) {
    const status = faucetForm.querySelector("[data-status-faucets]");
    guardForm(faucetForm);
    faucetForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const payload = {};
      // A blank box used to post NaN and come back as a raw 422 naming no
      // field — check here and name the row that is wrong (W-C5).
      for (const [key, label, max] of FAUCET_FIELDS) {
        const input = faucetForm.querySelector(`[name=${key}]`);
        const n = FLOAT_FAUCETS.has(key) ? parseFloat(input.value) : parseInt(input.value, 10);
        if (!Number.isFinite(n) || n < 0 || n > max) {
          showStatus(status, false, `"${label}" must be a number from 0 to ${max}`);
          input.focus();
          return;
        }
        payload[key] = n;
      }
      try {
        await apiPut("/api/economy/config", payload);
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }
}
