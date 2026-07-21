// Income Sources — everything that pays coins, on one page: per-guild enable
// switches for the custom-coded quest trigger hooks, the built-in faucet
// rates (editable in place for admins, read-only for manager-role holders —
// the save endpoint is admin-gated), and the roadmap of suggested sources.
// Gated by the economy manager role (or admin).
import { api, apiPut, esc } from "../api.js";
import { showStatus } from "../config-helpers.js";
import { KIND_LABELS } from "./economy-sources-shared.js";

// Order matters — rendered top to bottom. xp_per_coin is the one float knob.
const FAUCET_FIELDS = [
  ["login_text_base", "Daily login (first message)"],
  ["login_voice_base", "Daily voice login"],
  ["streak_bonus_cap", "Streak bonus cap"],
  ["milestone_day7", "Day 7 milestone"],
  ["milestone_day30", "Day 30 milestone"],
  ["milestone_day100", "Day 100 milestone"],
  ["milestone_per_100", "Per-100-days bonus"],
  ["reward_qotd", "QOTD reply (flat award)"],
  ["reward_game_participation", "Game participation"],
  ["reward_game_win", "Game win bonus"],
  ["reward_photo_post", "Photo Challenge post (flat award)"],
  ["xp_per_coin", "XP → coin conversion (XP per coin; 0 = off)"],
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
  container.innerHTML = `<div class="panel"><div class="empty">Loading Income Sources…</div></div>`;
  refresh(container);
  return null;
}

async function refresh(container) {
  let data;
  try {
    data = await api("/api/economy/income-sources");
  } catch (err) {
    container.innerHTML = `<div class="panel"><div class="error">${esc(err.message)}</div></div>`;
    return;
  }
  // Admin probe: the config GET is admin-gated, so a success means this user
  // may edit faucet rates in place (saves go to the same admin-gated PUT).
  // Manager-role holders get a 403 and the read-only view.
  const isAdmin = await api("/api/economy/config").then(() => true, () => false);
  render(container, data, isAdmin);
}

function questBadges(quests) {
  if (!quests.length) return `<span class="field-hint">no quests use this yet</span>`;
  return quests.map((q) =>
    `<span class="badge${q.active ? "" : " badge-dim"}" title="${esc(q.qtype)}${q.active ? "" : " (inactive)"}">${esc(q.title)}</span>`
  ).join(" ");
}

function faucetSection(data, isAdmin) {
  if (isAdmin) {
    const rows = FAUCET_FIELDS.map(([key, label]) => `
      <tr>
        <td>${esc(label)}</td>
        <td style="text-align:right;">
          <input type="number" name="${key}" value="${data.faucets[key] ?? 0}"
                 min="0" step="${FLOAT_FAUCETS.has(key) ? "0.5" : "1"}"
                 style="max-width:110px; text-align:right;" />
        </td>
      </tr>`).join("");
    return `
      <div class="field-hint" style="margin-bottom:8px;">
        These pay on their own, outside the quest system. A value of 0 disables
        one. Everything else about the economy is on
        <a href="#/economy-config">Settings</a>.
      </div>
      <form data-form-faucets>
        <div style="overflow-x:auto;">
          <table class="data-table" style="max-width:520px;">
            <thead><tr><th>Faucet</th><th style="text-align:right;">Value</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
        <div style="display:flex; gap:8px; align-items:center; margin-top:10px;">
          <button type="submit" class="btn btn-primary">Save Rates</button>
          <span data-status-faucets></span>
        </div>
      </form>`;
  }
  const rows = FAUCET_FIELDS.map(([key, label]) => `
    <tr><td>${esc(label)}</td><td style="text-align:right;">${data.faucets[key] ?? "—"}</td></tr>`).join("");
  return `
    <div class="field-hint" style="margin-bottom:8px;">
      These pay on their own, outside the quest system. A value of 0 disables
      one; the rates are admin-editable.
    </div>
    <div style="overflow-x:auto;">
      <table class="data-table" style="max-width:520px;">
        <thead><tr><th>Faucet</th><th style="text-align:right;">Value</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function render(container, data, isAdmin) {
  const rows = data.sources.map((s) => `
    <tr data-source-row="${esc(s.source)}">
      <td style="white-space:nowrap;">${esc(KIND_LABELS[s.source] || s.label)}</td>
      <td style="max-width:420px;">${esc(s.info)}<div style="margin-top:3px;">${questBadges(s.quests)}</div></td>
      <td>
        <label style="display:inline-flex; gap:5px; align-items:center; cursor:pointer;">
          <input type="checkbox" data-source-toggle="${esc(s.source)}"${s.enabled ? " checked" : ""} /> enabled
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
        <div class="subtitle">Everything that pays coins — trigger hooks and built-in faucet rates</div>
      </header>

      <section class="card">
        <div class="section-label">Quest Trigger Sources</div>
        <div class="field-hint" style="margin-bottom:8px;">
          Turning a source off stops it firing immediately (quests that use it
          stay in the library, they just wait). Attach a source to a quest on
          the <a href="#/economy-quests">Quests</a> page — completion mode
          “Playing a game”. Daily/weekly quests complete once per period;
          event quests pay every occurrence.
        </div>
        <div style="overflow-x:auto;">
          <table class="data-table">
            <thead><tr><th>Source</th><th>Fires when</th><th>State</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </section>

      <section class="card">
        <div class="section-label">Built-in faucet rates</div>
        ${faucetSection(data, isAdmin)}
      </section>

      <section class="card">
        <div class="section-label">Coin Drops</div>
        <div class="field-hint">The bot drops a random pouch of coins in a
          configured channel at unpredictable moments — the first member to
          press the drop's Claim button collects it; unclaimed pouches
          expire. Channel, amounts, cadence and expiry are configured on
          <a href="#/economy-config">Settings</a> (admin).</div>
      </section>

      <section class="card">
        <div class="section-label">Suggested Sources (not built yet)</div>
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
        showStatus(status, true, cb.checked ? "On" : "Off");
      } catch (err) {
        cb.checked = !cb.checked; // revert
        showStatus(status, false, err.message);
      }
    });
  });

  const faucetForm = container.querySelector("[data-form-faucets]");
  if (faucetForm) {
    const status = faucetForm.querySelector("[data-status-faucets]");
    faucetForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const payload = {};
      for (const [key] of FAUCET_FIELDS) {
        const raw = faucetForm.querySelector(`[name=${key}]`).value;
        payload[key] = FLOAT_FAUCETS.has(key) ? parseFloat(raw) : parseInt(raw, 10);
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
