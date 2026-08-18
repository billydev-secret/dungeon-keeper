// Survivor — the feature's ENTIRE admin surface (docs/survivor_spec.md §3;
// decided 2026-08-17/18: zero admin commands in Discord). Season lifecycle,
// every §5 dial, This Week's Games with manual settle, the flavor corpus,
// and the roster's eliminate/revive all live here.
import { api } from "../api.js";
import {
  apiPost,
  apiPut,
  apiDelete,
  esc,
  showStatus,
  guardForm,
  renderMetaWarning,
  mountAsync,
  loadRoles,
  loadChannels,
  mountRolePicker,
  mountChannelPicker,
  resolveMembers,
  memberNameLookup,
} from "../config-helpers.js";

const FLAVOR_CATEGORIES = ["eulogy", "toll", "no_death", "annul"];

const ENUMS = {
  tie_rule: [["loss", "Tie counts as a loss"], ["survive", "Tie survives"]],
  late_entry: [
    ["gauntlet", "Gauntlet (replay missed weeks)"],
    ["ghost_only", "Ghost Streak only"],
    ["closed", "Closed"],
  ],
  missed_pick: [
    ["auto_assign", "Groundskeeper auto-assigns"],
    ["eliminate", "Eliminate"],
  ],
};

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading Survivor…</div></div>`;

  return refreshAll(container);
}

// The guarded full re-render: season create/end changes the panel's whole
// shape, so those two actions rebuild everything — through mountAsync, whose
// error-with-retry state catches a failed refetch instead of leaving an
// unhandled rejection and a stale panel. Everything else re-renders only its
// own card (below), so unsaved rules-form edits survive roster/flavor work.
function refreshAll(container) {
  return mountAsync(container, () => render(container), {
    errorMsg: "Couldn’t load the Survivor settings.",
  });
}

async function render(container) {
  const [overview, roles, channels] = await Promise.all([
    api("/api/survivor/overview"),
    loadRoles(),
    loadChannels(),
  ]);
  const season = overview.season;

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Survivor</h2>
        <div class="subtitle">NFL pick’em survival pool — one team a week,
          no team twice, last one standing takes the pot</div>
      </header>
      ${renderMetaWarning()}
      <div data-season-zone></div>
      <div data-week-zone></div>
      <div data-rules-zone></div>
      <div data-roster-zone></div>
      <div data-flavor-zone></div>
    </div>
  `;

  const refresh = () => refreshAll(container);
  renderSeasonCard(container.querySelector("[data-season-zone]"), overview, refresh);
  if (season) {
    await renderWeekCard(container.querySelector("[data-week-zone]"));
    renderRulesCards(
      container.querySelector("[data-rules-zone]"), season, roles, channels,
    );
    await renderRosterCard(
      container.querySelector("[data-roster-zone]"), overview.players,
    );
  }
  renderFlavorCard(container.querySelector("[data-flavor-zone]"), overview.flavor);
}

// ── this week's games (manual settle) ─────────────────────────────────

const STATUS_BADGE = {
  scheduled: "🕐", in: "🏈 live", final: "✅ final", postponed: "⛔ postponed",
};

async function renderWeekCard(zone) {
  let data;
  try {
    data = await api("/api/survivor/week");
  } catch (err) {
    zone.innerHTML = `<div class="card"><div class="section-label">This Week's
      Games</div><div class="field-hint">${esc(err.message)}</div></div>`;
    return;
  }
  const rows = (data.games || []).map((g) => {
    const kick = new Date(g.kickoff_ts * 1000).toLocaleString([], {
      weekday: "short", hour: "numeric", minute: "2-digit",
    });
    const state = g.status === "final"
      ? `✅ ${esc(g.winner || "?")}${g.winner === "TIE" ? "" : " won"}`
      : `${STATUS_BADGE[g.status] || esc(g.status)}${g.kicked && g.status === "scheduled" ? " (kicked?)" : ""}`;
    // Settle for stuck games; correction stays available on finals — the
    // buttons feed the same derived pipeline either way.
    const verb = g.status === "final" ? "correct:" : "settle:";
    // Feed-derived strings are escaped here like everywhere else — the
    // parser accepts any string ESPN serves, so the buttons must not be
    // the one unescaped column.
    const btn = (outcome, label) =>
      `<button type="button" class="btn btn-small" data-settle="${esc(g.game_id)}"
        data-outcome="${esc(outcome)}">${esc(label)}</button>`;
    return `
      <tr>
        <td>wk ${g.week}</td>
        <td><strong>${esc(g.away)}</strong> @ <strong>${esc(g.home)}</strong></td>
        <td>${esc(kick)}</td>
        <td>${state}</td>
        <td style="white-space:nowrap;">${verb}
          ${btn(g.home, g.home)} ${btn(g.away, g.away)}
          ${btn("TIE", "tie")} ${btn("VOID", "void")}
        </td>
      </tr>`;
  }).join("");

  zone.innerHTML = `
    <div class="card">
      <div class="section-label">This Week's Games</div>
      <div class="field-hint">week ${data.week ?? "—"} ·
        ${data.picked} of ${data.alive} alive have picked. The poller settles
        results itself every 10 minutes during games — these buttons are the
        escape hatch for a stuck result, and a correction on a final unwinds
        strikes and resurrects the wrongly dead.</div>
      <div style="overflow-x:auto;">
        <table class="table">
          <thead><tr><th></th><th>Game</th><th>Kickoff</th><th>Status</th><th></th></tr></thead>
          <tbody>${rows || `<tr><td class="field-hint">no games in view</td></tr>`}</tbody>
        </table>
      </div>
      <div style="margin-top:8px; display:flex; gap:8px; align-items:center;">
        <button type="button" class="btn" data-preview-btn>👁 Preview Reckoning</button>
        <span data-status></span>
      </div>
      <div data-preview></div>
    </div>
  `;
  const status = zone.querySelector("[data-status]");
  zone.querySelector("[data-preview-btn]").addEventListener("click", async () => {
    const target = zone.querySelector("[data-preview]");
    try {
      const p = await api("/api/survivor/reckoning-preview");
      const fields = (p.fields || []).map((f) =>
        `<h4 style="margin:8px 0 2px;">${esc(f.name)}</h4>
         <div style="white-space:pre-wrap;">${esc(f.value)}</div>`).join("");
      target.innerHTML = `
        <div class="card" style="margin-top:8px;">
          <div class="section-label">${esc(p.title)}${p.pending
            ? " <em>(not due yet — current state)</em>" : ""}</div>
          <div style="white-space:pre-wrap;">${esc(p.description || "")}</div>
          ${fields}
        </div>`;
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
  zone.onclick = async (e) => {
    const btn = e.target.closest("[data-settle]");
    if (!btn) return;
    if (e.target.closest("[data-preview-btn]")) return;
    const outcome = btn.dataset.outcome;
    if (!window.confirm(
      `Settle ${btn.dataset.settle} as ${outcome}? This grades picks `
      + "immediately (and a correction re-grades them).")) return;
    try {
      await apiPost("/api/survivor/settle", {
        game_id: btn.dataset.settle, outcome,
      });
      await renderWeekCard(zone);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  };
}

// ── season lifecycle ──────────────────────────────────────────────────

function renderSeasonCard(zone, overview, refresh) {
  const season = overview.season;
  const archived = overview.archived_seasons || [];
  if (!season) {
    zone.innerHTML = `
      <div class="card">
        <div class="section-label">Season</div>
        <p class="field-hint">No live season. Creating one opens enrollment —
          the announcement post and join button ship in a later stage, so
          creating it early is safe.</p>
        <form data-create-form class="form">
          <div class="field">
            <label for="sv-name">Season Name</label>
            <input type="text" id="sv-name" name="name" required maxlength="100"
              placeholder="The Long Autumn" style="max-width:280px;" />
          </div>
          <div class="field">
            <label for="sv-year">NFL Season Year</label>
            <input type="number" id="sv-year" name="season_year" required
              min="2020" max="2100" value="${new Date().getFullYear()}"
              style="max-width:140px;" />
          </div>
          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Create Season</button>
            <span data-status></span>
          </div>
        </form>
        ${archived.length ? `<div class="field-hint" style="margin-top:8px;">
          Archived: ${archived.map((s) => `${esc(s.name)} (${s.season_year})`).join(", ")}
        </div>` : ""}
      </div>
    `;
    const form = zone.querySelector("[data-create-form]");
    const status = zone.querySelector("[data-status]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        const res = await apiPost("/api/survivor/season", {
          name: fd.get("name"),
          season_year: parseInt(fd.get("season_year"), 10),
        });
        // Both reports matter: roles AND the schedule ingest — a failed
        // ingest with nothing on the panel would leave nfl_games empty and
        // nobody the wiser until the slate doesn't post.
        const report = [...(res.role_report || []), res.schedule_report]
          .filter(Boolean).join("; ");
        showStatus(status, true, report || undefined);
        setTimeout(refresh, report ? 4000 : 600);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
    return;
  }

  zone.innerHTML = `
    <div class="card">
      <div class="section-label">Season</div>
      <p><strong>${esc(season.name)}</strong> (${season.season_year}) —
        <code>${esc(season.status)}</code></p>
      <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
        <button type="button" class="btn btn-primary" data-announce-btn>
          📌 Post Announcement</button>
        <button type="button" class="btn btn-danger" data-end-btn>End Season</button>
        <span data-status></span>
      </div>
      <div class="field-hint">Post Announcement pins the season embed with its
        Join button in the configured Survivor channel (repost re-points the
        counter). Ending archives the season — history stays queryable, and a
        new season can then be created.</div>
    </div>
  `;
  const status = zone.querySelector("[data-status]");
  zone.querySelector("[data-announce-btn]").addEventListener("click", async () => {
    try {
      const res = await apiPost("/api/survivor/announcement", {});
      showStatus(status, true, res.pinned ? "posted and pinned" : "posted (pin failed — check Manage Messages)");
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
  zone.querySelector("[data-end-btn]").addEventListener("click", async () => {
    if (!window.confirm(`End "${season.name}"? This archives the season.`)) return;
    try {
      await apiPost("/api/survivor/season/end", {});
      refresh();
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}

// ── the §5 dials ──────────────────────────────────────────────────────

function numField(name, label, value, hint, { min = 0, max = 1000000 } = {}) {
  return `
    <div class="field">
      <label for="sv-${name}">${label}</label>
      <input type="number" id="sv-${name}" name="${name}" required
        min="${min}" max="${max}" step="1" value="${value}" style="max-width:140px;" />
      ${hint ? `<div class="field-hint">${hint}</div>` : ""}
    </div>`;
}

function selectField(name, label, value, hint) {
  const opts = ENUMS[name]
    .map(([v, text]) =>
      `<option value="${v}"${v === value ? " selected" : ""}>${text}</option>`)
    .join("");
  return `
    <div class="field">
      <label for="sv-${name}">${label}</label>
      <select id="sv-${name}" name="${name}" style="max-width:280px;">${opts}</select>
      ${hint ? `<div class="field-hint">${hint}</div>` : ""}
    </div>`;
}

function renderRulesCards(zone, season, roles, channels) {
  const c = season.config;
  zone.innerHTML = `
    <form class="form form-cards" data-rules-form>
      <div class="card" style="border-color: var(--gold-solid, #e6b84c);">
        <div class="section-label">⚠️ Under Construction</div>
        <p class="field-hint">The Week-1 game is fully live: picks, results,
          strikes, the groundskeeper, the gauntlet, ghosts, and the weekly
          posts. Still to come (each before its own in-season deadline):
          wipeout/annul handling, double-pick weeks (wk 14), the Accord, the
          endgame payouts, and the member notification toggles — their dials
          store now and bind when that logic ships.</p>
      </div>
      <div class="card">
        <div class="section-label">Wiring</div>
        <div class="field">
          <label>Survivor Channel</label>
          <span data-picker="channel_id"></span>
          <div class="field-hint">Where the slate, the Reckoning, and the season
            announcement post. Nothing posts until this is set.</div>
        </div>
        <div class="field"><label>🏈 Survivor Role</label>
          <span data-picker="role_survivor_id"></span></div>
        <div class="field"><label>👻 Ghost Role</label>
          <span data-picker="role_ghost_id"></span></div>
        <div class="field"><label>🏈 Sole Survivor Role</label>
          <span data-picker="role_sole_survivor_id"></span>
          <div class="field-hint">All three are created automatically at
            season creation if missing; repoint them here if you'd rather use
            your own. Death swaps Survivor → Ghost; both roles are pinged by
            the two weekly posts.</div></div>
      </div>

      <div class="card">
        <div class="section-label">Money</div>
        ${numField("buyin_coins", "Buy-In (coins)", c.buyin_coins,
    "0 = free entry (season one). Debited on join.")}
        ${numField("pot_seed", "House Pot Seed", c.pot_seed,
    "Booked at creation, minted once at payout as <code>survivor_payout</code>. " +
    "This is a faucet — 10,000 ≈ 13% of the current float.")}
        ${numField("ghost_pot_pct", "Ghost Side-Pot %", c.ghost_pot_pct,
    "Share of the seed carved off for the Ghost Streak side-pot.", { max: 100 })}
        ${numField("gauntlet_fee_per_week", "Gauntlet Fee / Week", c.gauntlet_fee_per_week,
    "× replayed weeks, charged on late entry. Alive arrivals feed the main " +
    "pot; dead-on-arrival fees feed the ghost pot.", { max: 10000 })}
      </div>

      <div class="card">
        <div class="section-label">Lives &amp; Picks</div>
        ${numField("strikes", "Strikes", c.strikes,
    "Wrong weeks a player survives. 0 = sudden death.", { max: 2 })}
        ${selectField("tie_rule", "Ties", c.tie_rule)}
        ${selectField("late_entry", "Late Entry", c.late_entry)}
        ${selectField("missed_pick", "Missed Pick", c.missed_pick)}
        ${numField("max_auto_assigns", "Auto-Assign Cap", c.max_auto_assigns,
    "Groundskeeper covers this many missed weeks per season; the next one " +
    "is an elimination.", { max: 18 })}
      </div>

      <div class="card">
        <div class="section-label">Escalation &amp; Endgame</div>
        ${numField("double_pick_start_week", "Double-Pick From Week", c.double_pick_start_week,
    "0 = never.", { max: 18 })}
        ${numField("double_pick_min_alive", "…Only If This Many Alive", c.double_pick_min_alive,
    "", { min: 2, max: 1000 })}
        ${numField("wipeout_annul_through_week", "Wipeout Annuls Through Week",
    c.wipeout_annul_through_week,
    "A week that kills everyone is annulled through this week; afterwards " +
    "it's an equal split.", { max: 18 })}
        ${numField("accord_max_alive", "Accord Available At ≤", c.accord_max_alive,
    "Living players needed before /survivor accord can be invoked.", { min: 2, max: 1000 })}
        <div class="field">
          <label style="display:flex; gap:8px; align-items:center;">
            <input type="checkbox" name="ghost_streak"
              ${c.ghost_streak ? "checked" : ""} /> Ghost Streak side-game
          </label>
          <div class="field-hint">The dead keep picking for a side-pot. Load-bearing
            for late entry — gauntlet joiners who arrive dead land here.</div>
        </div>
      </div>

      <div class="card">
        <div class="section-label">Weekly Schedule (guild-local hours)</div>
        ${numField("slate_hour", "Slate Post — Wednesday", c.slate_hour, "", { max: 23 })}
        ${numField("lastcall_hour", "Last Call — Saturday", c.lastcall_hour, "", { max: 23 })}
        ${numField("reckoning_hour", "The Reckoning — Tuesday", c.reckoning_hour, "", { max: 23 })}
      </div>

      <div style="display:flex; gap:8px; align-items:center;">
        <button type="submit" class="btn btn-primary">Save Rules</button>
        <span data-status></span>
      </div>
    </form>
  `;

  const form = zone.querySelector("[data-rules-form]");
  const status = zone.querySelector("[data-status]");
  const channelPicker = mountChannelPicker(
    form.querySelector('[data-picker="channel_id"]'),
    channels, String(c.channel_id || "0"), { label: "Survivor Channel" },
  );
  const rolePickers = {};
  for (const key of ["role_survivor_id", "role_ghost_id", "role_sole_survivor_id"]) {
    rolePickers[key] = mountRolePicker(
      form.querySelector(`[data-picker="${key}"]`),
      roles, String(c[key] || "0"),
    );
  }
  guardForm(form);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const body = {
      channel_id: channelPicker.getValue() || "0",
      ghost_streak: fd.get("ghost_streak") != null,
    };
    for (const [key, picker] of Object.entries(rolePickers)) {
      body[key] = picker.getValue() || "0";
    }
    for (const el of form.querySelectorAll("input[type=number]")) {
      const n = parseInt(el.value, 10);
      if (!Number.isFinite(n)) {
        showStatus(status, false, `${el.name} must be a number`);
        el.focus();
        return;
      }
      body[el.name] = n;
    }
    for (const key of Object.keys(ENUMS)) {
      body[key] = fd.get(key);
    }
    try {
      await apiPut("/api/survivor/config", body);
      showStatus(status, true);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}

// ── roster ────────────────────────────────────────────────────────────

async function renderRosterCard(zone, players) {
  players = players || [];
  if (!players.length) {
    zone.innerHTML = `
      <div class="card">
        <div class="section-label">Roster</div>
        <p class="field-hint">No souls enrolled yet.</p>
      </div>`;
    return;
  }
  const members = await resolveMembers(players.map((p) => p.user_id));
  const nameOf = memberNameLookup(members);
  const rows = players.map((p) => {
    const dead = p.status === "ghost";
    return `
      <tr>
        <td>${esc(nameOf(p.user_id) || p.user_id)}</td>
        <td>${dead ? "👻 ghost" : "🏈 alive"}${
      dead && p.eliminated_week ? ` (wk ${p.eliminated_week})` : ""}</td>
        <td>${p.strikes_used}</td>
        <td style="white-space:nowrap;">
          ${dead
        ? `<button type="button" class="btn btn-small"
              data-revive="${p.user_id}">Revive</button>`
        : `wk <input type="number" min="1" max="18" value="1"
              data-week="${p.user_id}" style="width:56px;" />
            <button type="button" class="btn btn-small btn-danger"
              data-eliminate="${p.user_id}">Eliminate</button>`}
        </td>
      </tr>`;
  }).join("");

  zone.innerHTML = `
    <div class="card">
      <div class="section-label">Roster</div>
      <div style="overflow-x:auto;">
        <table class="table">
          <thead><tr><th>Member</th><th>Status</th><th>Strikes</th><th></th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <div class="field-hint">Revive restores life only — a wrong <em>result</em>
        is corrected by re-settling the game, which unwinds strikes too.</div>
      <span data-status></span>
    </div>
  `;
  const status = zone.querySelector("[data-status]");
  // Scoped re-render: only this card refetches and rebuilds, so unsaved
  // edits in the rules form survive. `onclick` assignment (not
  // addEventListener) keeps the handler single across re-renders of the
  // same zone. A failed refetch lands in the still-present status line.
  zone.onclick = async (e) => {
    const elim = e.target.closest("[data-eliminate]");
    const revive = e.target.closest("[data-revive]");
    try {
      if (elim) {
        const uid = elim.dataset.eliminate;
        const week = parseInt(
          zone.querySelector(`[data-week="${uid}"]`).value, 10) || 1;
        if (!window.confirm(`Eliminate this member in week ${week}?`)) return;
        await apiPost(`/api/survivor/player/${uid}/eliminate`, { week });
      } else if (revive) {
        if (!window.confirm("Revive this member?")) return;
        await apiPost(`/api/survivor/player/${revive.dataset.revive}/revive`, {});
      } else {
        return;
      }
      const fresh = await api("/api/survivor/overview");
      await renderRosterCard(zone, fresh.players);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  };
}

// ── flavor corpus ─────────────────────────────────────────────────────

function renderFlavorCard(zone, flavor) {
  flavor = flavor || [];
  const byCategory = new Map(FLAVOR_CATEGORIES.map((cat) => [cat, []]));
  for (const f of flavor) byCategory.get(f.category)?.push(f);

  const sections = FLAVOR_CATEGORIES.map((cat) => {
    const lines = byCategory.get(cat);
    const items = lines.map((f) => `
      <tr>
        <td style="${f.active ? "" : "opacity:.45;"}">${esc(f.line)}</td>
        <td style="white-space:nowrap;">
          <button type="button" class="btn btn-small"
            data-toggle="${f.id}" data-active="${f.active ? 1 : 0}">
            ${f.active ? "Retire" : "Restore"}</button>
          <button type="button" class="btn btn-small btn-danger"
            data-delete="${f.id}">Delete</button>
        </td>
      </tr>`).join("");
    return `
      <details ${cat === "eulogy" ? "open" : ""}>
        <summary><strong>${cat}</strong> (${lines.filter((f) => f.active).length} active)</summary>
        <div style="overflow-x:auto;">
          <table class="table"><tbody>${items ||
      `<tr><td class="field-hint">no lines yet — seeded at season creation</td></tr>`}
          </tbody></table>
        </div>
      </details>`;
  }).join("");

  zone.innerHTML = `
    <div class="card">
      <div class="section-label">Flavor Corpus</div>
      <div class="field-hint">The Reckoning’s voice. Slots:
        <code>{name}</code> the member, <code>{team}</code> the fatal pick,
        <code>{week}</code> the week number. Retired lines stay for later;
        the starter corpus seeds itself at season creation.</div>
      ${sections}
      <form data-flavor-form class="form" style="margin-top:10px;">
        <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
          <select name="category">
            ${FLAVOR_CATEGORIES.map((cat) => `<option value="${cat}">${cat}</option>`).join("")}
          </select>
          <input type="text" name="line" required maxlength="500"
            placeholder="a new line for the meadow…" style="flex:1; min-width:240px;" />
          <button type="submit" class="btn btn-primary">Add Line</button>
          <span data-status></span>
        </div>
      </form>
    </div>
  `;

  const status = zone.querySelector("[data-status]");
  // Scoped like the roster card: this card refetches and rebuilds alone
  // (unsaved rules edits survive), onclick assignment keeps the zone's
  // handler single across re-renders, and a failed refetch lands in the
  // still-present status line.
  const refreshFlavor = async () => {
    const fresh = await api("/api/survivor/overview");
    renderFlavorCard(zone, fresh.flavor);
  };
  zone.querySelector("[data-flavor-form]").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      await apiPost("/api/survivor/flavor", {
        category: fd.get("category"),
        line: fd.get("line"),
      });
      await refreshFlavor();
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
  zone.onclick = async (e) => {
    const toggle = e.target.closest("[data-toggle]");
    const del = e.target.closest("[data-delete]");
    try {
      if (toggle) {
        await apiPut(`/api/survivor/flavor/${toggle.dataset.toggle}`, {
          active: toggle.dataset.active !== "1",
        });
      } else if (del) {
        if (!window.confirm("Delete this line for good? Retiring keeps it around.")) return;
        await apiDelete(`/api/survivor/flavor/${del.dataset.delete}`);
      } else {
        return;
      }
      await refreshFlavor();
    } catch (err) {
      showStatus(status, false, err.message);
    }
  };
}
