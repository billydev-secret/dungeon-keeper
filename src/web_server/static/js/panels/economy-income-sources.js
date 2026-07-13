// Income Sources — per-guild enable switches for the custom-coded quest
// trigger hooks, a read-only view of the built-in faucets, and the roadmap
// of suggested sources. Gated by the economy manager role (or admin).
import { api, apiPut, esc } from "../api.js";
import { showStatus } from "../config-helpers.js";
import { KIND_LABELS } from "./economy-sources-shared.js";

const FAUCET_LABELS = {
  login_text_base: "Daily login (first message)",
  login_voice_base: "Daily voice login",
  reward_qotd: "QOTD reply (flat award)",
  reward_game_participation: "Game participation",
  reward_game_win: "Game win bonus",
  xp_per_coin: "XP → coin conversion (XP per coin)",
};

// Not built yet — shown so managers can see what's on the table. Keep in
// sync with the parking lot in docs/economy_spec.md.
const SUGGESTIONS = [
  ["🔢 Counted quests", "“Play 5 party games this week” — per-member progress counters instead of once-per-period. The next structural piece; multiplies every source below."],
  ["📆 Monthly cadence", "A monthly quest type alongside daily/weekly (period = calendar month)."],
  ["🔔 Server bump", "Reward whoever runs /bump. Needs attribution from the detector-bot message (message.interaction_metadata) before it can ship."],
  ["📊 Survey completion", "The survey spec is aspirational — there is no survey feature in the code yet to hook."],
  ["🤝 Invite retention", "Pay the invite source only after the invitee survives the prune window (the current invite source pays on join)."],
  ["🤫 Confessions", "Considered and rejected — a payout in the ledger would deanonymize the confessor."],
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
  render(container, data);
}

function questBadges(quests) {
  if (!quests.length) return `<span class="field-hint">no quests use this yet</span>`;
  return quests.map((q) =>
    `<span class="badge${q.active ? "" : " badge-dim"}" title="${esc(q.qtype)}${q.active ? "" : " (inactive)"}">${esc(q.title)}</span>`
  ).join(" ");
}

function render(container, data) {
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

  const faucets = Object.entries(FAUCET_LABELS).map(([key, label]) => `
    <tr><td>${esc(label)}</td><td style="text-align:right;">${data.faucets[key] ?? "—"}</td></tr>`).join("");

  const suggestions = SUGGESTIONS.map(([label, note]) => `
    <div style="margin:8px 0;">
      <strong>${esc(label)}</strong>
      <div class="field-hint">${esc(note)}</div>
    </div>`).join("");

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Income Sources</h2>
        <div class="subtitle">The custom-coded hooks that can complete quests automatically</div>
      </header>

      <section class="card">
        <div class="section-label">Quest trigger sources</div>
        <div class="field-hint" style="margin-bottom:8px;">
          Turning a source off stops it firing immediately (quests that use it
          stay in the library, they just wait). Attach a source to a quest from
          the <strong>Bank Manager</strong> — completion mode “Playing a game”.
          Daily/weekly quests complete once per period; event quests pay every
          occurrence.
        </div>
        <div style="overflow-x:auto;">
          <table class="data-table">
            <thead><tr><th>Source</th><th>Fires when</th><th>State</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </section>

      <section class="card">
        <div class="section-label">Built-in faucets (for context)</div>
        <div class="field-hint" style="margin-bottom:8px;">
          These pay on their own, outside the quest system. The knobs live on
          the <strong>Economy</strong> config page; a value of 0 disables one.
        </div>
        <div style="overflow-x:auto;">
          <table class="data-table" style="max-width:520px;">
            <thead><tr><th>Faucet</th><th>Value</th></tr></thead>
            <tbody>${faucets}</tbody>
          </table>
        </div>
      </section>

      <section class="card">
        <div class="section-label">Suggested sources (not built yet)</div>
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
}
