// Survivor — the feature's ENTIRE admin surface (docs/survivor_spec.md §3;
// decided 2026-08-17/18: zero admin commands in Discord). Season lifecycle,
// every §5 dial, This Week's Games with manual settle, and the roster's
// eliminate/revive all live here. (The flavor corpus was removed 2026-08-18
// — the Reckoning is just-the-facts now.)
import { api } from "../api.js";
import { confirmDialog, toast } from "../ui.js";
import {
  apiPost,
  apiPut,
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
// own card (below), so unsaved rules-form edits survive roster work.
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
      <div data-sim-zone></div>
      <div data-week-zone></div>
      <div data-roster-zone></div>
      <div data-rules-zone></div>
    </div>
  `;

  const refresh = () => refreshAll(container);
  // Zone order is operational-first (2026-08-18): the cards that change
  // weekly (season, sim, games, roster) sit above the set-once rules form,
  // which previously split them in half.
  renderSeasonCard(container.querySelector("[data-season-zone]"), overview, refresh);
  if (season) {
    if (season.season_year >= 2090) {
      renderSimulatorCard(container.querySelector("[data-sim-zone]"), refresh);
    }
    await renderWeekCard(container.querySelector("[data-week-zone]"));
    renderRulesCards(
      container.querySelector("[data-rules-zone]"), season, roles, channels,
    );
    await renderRosterCard(
      container.querySelector("[data-roster-zone]"), overview.players,
    );
  }
}

// ── season simulator (synthetic seasons, year ≥ 2090) ─────────────────

function renderSimulatorCard(zone, refresh) {
  zone.innerHTML = `
    <div class="card" style="border-color: var(--gold-solid, #e6b84c);">
      <div class="section-label">🧪 Season Simulator</div>
      <div class="field-hint">Synthetic season — ESPN is never touched. Lay
        down a compressed schedule (weeks run in minutes), let people join
        and pick in the channel, then settle kicked games and run the weekly
        tasks to advance. Everything flows through the real engine.</div>
      <form data-sim-form class="form">
        <div class="field-row" style="align-items:end;">
          <div class="field"><label for="sim-weeks">Weeks</label>
            <input type="number" id="sim-weeks" name="weeks" min="1" max="18"
              value="4" /></div>
          <div class="field"><label for="sim-mpw">Minutes per Week</label>
            <input type="number" id="sim-mpw" name="minutes_per_week" min="2"
              max="1440" value="15" /></div>
          <div class="field">
            <button type="submit" class="btn btn-primary">Generate Schedule</button>
          </div>
        </div>
      </form>
      <div class="field mt-8">
        <label>Settle Kicked Games</label>
        <div class="row-8" style="flex-wrap:wrap;">
          <button type="button" class="btn btn-sm" data-sim-settle="chalk">Favorites Win</button>
          <button type="button" class="btn btn-sm" data-sim-settle="random">Random</button>
          <button type="button" class="btn btn-sm" data-sim-settle="upset">Upsets</button>
        </div>
      </div>
      <div class="field mt-8">
        <label>Advance the Week</label>
        <div class="row-8" style="flex-wrap:wrap;">
          <button type="button" class="btn btn-sm" data-tasks-btn>▶ Run Weekly Tasks</button>
          <span data-status></span>
        </div>
      </div>
      <div class="field-hint">The weekly tasks run themselves on the clock —
        slate Wednesday, last call Saturday, the Reckoning Tuesday, in the
        guild's own hours. This forces them now so a simulated week doesn't
        wait for Tuesday; the once-per-week state still prevents double
        posts, and only this server's season is touched.</div>
    </div>
  `;
  const status = zone.querySelector("[data-status]");
  zone.querySelector("[data-sim-form]").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      const res = await apiPost("/api/survivor/sim/schedule", {
        weeks: parseInt(fd.get("weeks"), 10),
        minutes_per_week: parseInt(fd.get("minutes_per_week"), 10),
      });
      showStatus(status, true, `${res.games} games laid down — first kickoff in ~1 minute`);
      setTimeout(refresh, 1500);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
  zone.querySelector("[data-tasks-btn]").addEventListener("click", async () => {
    if (!await confirmDialog(
      "Run the weekly tasks now, skipping the day/hour gates? "
      + "Once-per-week still holds — nothing double-posts.",
      { confirmLabel: "Run Now" })) return;
    try {
      const res = await apiPost("/api/survivor/tasks/run", {});
      // Report what actually posted: with no schedule ingested every gate
      // returns "not due", and a flat success message hid that entirely.
      const row = (res.report || [])[0];
      if (!row) {
        showStatus(status, false, "no live season on this server");
      } else if (row.error) {
        showStatus(status, false, `task failed: ${row.error}`);
      } else if (row.fired && row.fired.length) {
        showStatus(status, true, `posted ${row.fired.join(", ")} — check the channel`);
      } else {
        showStatus(status, false, `nothing was due — ${row.reason}`);
      }
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
  zone.onclick = async (e) => {
    const btn = e.target.closest("[data-sim-settle]");
    if (!btn) return;
    try {
      const res = await apiPost("/api/survivor/sim/settle", {
        mode: btn.dataset.simSettle,
      });
      showStatus(status, true, `${res.settled.length} game(s) settled`);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  };
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
      `<button type="button" class="btn btn-sm" data-settle="${esc(g.game_id)}"
        data-outcome="${esc(outcome)}">${esc(label)}</button>`;
    return `
      <tr>
        <td>wk ${g.week}</td>
        <td><strong>${esc(g.away)}</strong> @ <strong>${esc(g.home)}</strong></td>
        <td>${esc(kick)}</td>
        <td>${state}</td>
        <td style="white-space:nowrap;">
          <span class="field-hint" style="display:inline;">${verb}</span>
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
      <div class="row-8 mt-8">
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
        `<div class="mt-8"><strong>${esc(f.name)}</strong></div>
         <div style="white-space:pre-wrap;">${esc(f.value)}</div>`).join("");
      target.innerHTML = `
        <div class="card mt-8">
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
    if (!await confirmDialog(
      `Settle ${btn.dataset.settle} as ${outcome}? This grades picks `
      + "immediately (and a correction re-grades them).",
      { confirmLabel: "Settle" })) return;
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
          <div class="row-8">
            <button type="submit" class="btn btn-primary">Create Season</button>
            <span data-status></span>
          </div>
        </form>
        ${archived.length ? `<div class="field-hint mt-8">
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
      <div class="row-8" style="flex-wrap:wrap;">
        <button type="button" class="btn btn-primary" data-announce-btn>
          📌 Post Panel</button>
        <span data-status></span>
        <span class="act-spacer"></span>
        <button type="button" class="btn btn-danger" data-end-btn>End Season</button>
      </div>
      <div class="field-hint">Post Panel posts the channel's one updating
        message — slate, standings, join and pick buttons — in the configured
        Survivor channel, where it keeps itself at the bottom; reposting
        retires the previous copy, and the bot reposts it itself every
        Wednesday with the week-open ping. Ending archives the season —
        history stays queryable, and a new season can then be created.</div>
      <div data-clock class="mt-8"></div>
    </div>
  `;
  const status = zone.querySelector("[data-status]");
  renderClock(zone.querySelector("[data-clock]"));
  zone.querySelector("[data-announce-btn]").addEventListener("click", async () => {
    try {
      const res = await apiPost("/api/survivor/announcement", {});
      showStatus(status, true, res.retired_previous ? "posted — previous copy retired" : "posted");
      // Posted, but another sticky panel already holds that channel's bottom.
      if (res.warning) toast(res.warning, "info");
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
  zone.querySelector("[data-end-btn]").addEventListener("click", async () => {
    if (!await confirmDialog(
      `End "${season.name}"? This archives the season — history stays queryable, and a new season can then be created.`,
      { danger: true, confirmLabel: "End Season" })) return;
    try {
      await apiPost("/api/survivor/season/end", {});
      refresh();
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}

// ── weekly clock ──────────────────────────────────────────────────────
// Read-only view of the three clock-gated posts on a real season (the
// force-run button stays on the Simulator card on purpose): which week each
// last fired for, and when its gate next opens, in the guild's own hours.
// "Already fired for this week" is the shape that was invisible before
// (2026-09-02 review) — the slate and the last call can be re-armed here.

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function hourLabel(hour) {
  const h12 = ((hour + 11) % 12) + 1;
  return `${h12}:00 ${hour < 12 ? "AM" : "PM"}`;
}

// Guild-local rendering: shift by the guild's offset and format as UTC, so
// an admin three zones away reads the server's clock, not their own.
function guildLocal(ts, offsetHours) {
  return new Date((ts + offsetHours * 3600) * 1000).toLocaleString([], {
    weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit", timeZone: "UTC",
  });
}

async function renderClock(zone) {
  let data;
  try {
    data = await api("/api/survivor/clock");
  } catch (err) {
    zone.innerHTML = `<div class="field-hint">Weekly clock: ${esc(err.message)}</div>`;
    return;
  }
  const rows = (data.tasks || []).map((t) => {
    let next;
    if (t.due_week != null) {
      next = `<strong>due now</strong> — fires for week ${t.due_week} on the next tick`;
    } else if (t.spent) {
      next = `already fired for week ${t.fired_week} (this week) · `
        + `gate reopens ${esc(guildLocal(t.next_ts, data.offset_hours))}`;
    } else {
      next = esc(guildLocal(t.next_ts, data.offset_hours));
    }
    const reset = t.resettable && t.spent
      ? `<button type="button" class="btn btn-sm" data-reset="${esc(t.task)}"
          data-label="${esc(t.label)}">Reset this week</button>`
      : "";
    return `
      <tr>
        <td>${esc(t.label)}</td>
        <td>${WEEKDAYS[t.weekday]} ${hourLabel(t.hour)}</td>
        <td>${t.fired_week ? `week ${t.fired_week}` : "never"}</td>
        <td>${next}</td>
        <td style="white-space:nowrap;">${reset}</td>
      </tr>`;
  }).join("");
  zone.innerHTML = `
    <div class="section-label">Weekly clock</div>
    <div class="field-hint">Pick week ${data.week ?? "—"} · server-local
      hours. Each post fires once per week, at or after its hour.</div>
    <div style="overflow-x:auto;">
      <table class="table">
        <thead><tr><th>Post</th><th>Gate</th><th>Last fired</th><th>Next</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <span data-clock-status></span>
  `;
  const status = zone.querySelector("[data-clock-status]");
  zone.onclick = async (e) => {
    const btn = e.target.closest("[data-reset]");
    if (!btn) return;
    if (!await confirmDialog(
      `Re-arm the ${btn.dataset.label} for week ${data.week}? It posts again `
      + "at its next gate — or on the next tick if the gate is already open — "
      + "and pings the roles again.",
      { danger: true, confirmLabel: "Reset" })) return;
    try {
      await apiPost(`/api/survivor/tasks/${btn.dataset.reset}/reset`, {});
      await renderClock(zone);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  };
}

// ── the §5 dials ──────────────────────────────────────────────────────
// Each rules card is one spec: compact dial rows up top, and the dials'
// explanations collapsed behind a "What do these dials do?" details below
// (decided 2026-08-18 — the always-visible per-dial prose made the page
// mostly scroll). A dial with no hint just doesn't appear in the details.

function numField(name, label, value, { min = 0, max = 1000000 } = {}) {
  return `
    <div class="field">
      <label for="sv-${name}">${label}</label>
      <input type="number" id="sv-${name}" name="${name}" required
        min="${min}" max="${max}" step="1" value="${value}" />
    </div>`;
}

function selectField(name, label, value) {
  const opts = ENUMS[name]
    .map(([v, text]) =>
      `<option value="${v}"${v === value ? " selected" : ""}>${text}</option>`)
    .join("");
  return `
    <div class="field">
      <label for="sv-${name}">${label}</label>
      <select id="sv-${name}" name="${name}">${opts}</select>
    </div>`;
}

// [kind, name, label, hint, opts] — kind picks the widget; hint feeds only
// the collapsed explanations block.
const RULES_CARDS = [
  ["Money", [
    ["num", "buyin_coins", "Buy-In (coins)",
      "0 = free entry (season one). Debited on join."],
    ["num", "pot_seed", "House Pot Seed",
      "Booked at creation, minted once at payout as <code>survivor_payout</code>. "
      + "This is a faucet — 10,000 ≈ 13% of the current float."],
    ["num", "ghost_pot_pct", "Ghost Side-Pot %",
      "Share of the seed carved off for the Ghost Streak side-pot.", { max: 100 }],
    ["num", "gauntlet_fee_per_week", "Gauntlet Fee / Week",
      "× replayed weeks, charged on late entry. Alive arrivals feed the main "
      + "pot; dead-on-arrival fees feed the ghost pot.", { max: 10000 }],
    ["num", "weekly_win_coins", "Weekly Win Prize",
      "Paid at the Reckoning to everyone whose picks all won that week "
      + "(ghosts included). A real faucet — <code>survivor_weekly_win</code> in "
      + "the ledger. 0 = off.", { max: 10000 }],
  ]],
  ["Lives & Picks", [
    ["num", "strikes", "Strikes",
      "Wrong weeks a player survives. 0 = sudden death.", { max: 2 }],
    ["select", "tie_rule", "Ties", ""],
    ["select", "late_entry", "Late Entry",
      "Gauntlet replays missed weeks as the favorite each week — the joiner "
      + "inherits that line's full fate before paying."],
    ["select", "missed_pick", "Missed Pick", ""],
    ["num", "max_auto_assigns", "Auto-Assign Cap",
      "Groundskeeper covers this many missed weeks per season; the next one "
      + "is an elimination.", { max: 18 }],
  ]],
  ["Escalation & Endgame", [
    // The double-pick-start, wipeout-annul, double-pick-minimum and Accord
    // dials are deliberately absent: nothing enforces those rules yet, so
    // offering them here would promise a season rule the bot does not play
    // by. (Double-pick's start week was the last to go, 2026-09-02: only the
    // gauntlet replay read it, grading late joiners on a rule nobody else
    // played.) They return with the code that reads them.
    ["check", "ghost_streak", "Ghost Streak side-game",
      "The dead keep picking for a side-pot. Load-bearing for late entry — "
      + "gauntlet joiners who arrive dead land here."],
  ]],
  ["Weekly Schedule (guild-local hours)", [
    ["num", "slate_hour", "Slate Post — Wednesday", "", { max: 23 }],
    ["num", "lastcall_hour", "Last Call — Saturday", "", { max: 23 }],
    ["num", "reckoning_hour", "The Reckoning — Tuesday", "", { max: 23 }],
  ]],
];

function checkField(name, label, value) {
  return `
    <div class="field">
      <label class="row-8">
        <input type="checkbox" name="${name}" ${value ? "checked" : ""} />
        ${label}
      </label>
    </div>`;
}

function renderRulesCard(title, fields, c) {
  const dials = fields.map(([kind, name, label, , opts]) => {
    if (kind === "select") return selectField(name, label, c[name]);
    if (kind === "check") return checkField(name, label, c[name]);
    return numField(name, label, c[name], opts);
  }).join("");
  const notes = fields
    .filter(([, , , hint]) => hint)
    .map(([, , label, hint]) =>
      `<div class="note"><strong>${label}</strong> — ${hint}</div>`).join("");
  return `
    <div class="card">
      <div class="section-label">${title}</div>
      <div class="field-row">${dials}</div>
      ${notes ? `
        <details class="panel-about">
          <summary>What do these dials do?</summary>
          ${notes}
        </details>` : ""}
    </div>`;
}

function renderRulesCards(zone, season, roles, channels) {
  const c = season.config;
  zone.innerHTML = `
    <form class="form form-cards" data-rules-form>
      <div class="notice-banner mb-8">
        <strong>Under construction:</strong> the Week-1 game is fully live —
        picks, results, strikes, the groundskeeper, the gauntlet, ghosts, and
        the weekly posts. Still to come (each before its own in-season
        deadline): wipeout/annul handling, double-pick weeks (wk 14), the
        Accord, the endgame payouts, and the member notification toggles —
        their dials store now and bind when that logic ships.
      </div>
      <div class="card">
        <div class="section-label">Wiring</div>
        <div class="field">
          <label>Survivor Channel</label>
          <span data-picker="channel_id"></span>
          <div class="field-hint">Where the slate, the Reckoning, and the season
            announcement post. Nothing posts until this is set.</div>
        </div>
        <div class="field-row">
          <div class="field"><label>🏈 Survivor Role</label>
            <span data-picker="role_survivor_id"></span></div>
          <div class="field"><label>👻 Ghost Role</label>
            <span data-picker="role_ghost_id"></span></div>
          <div class="field"><label>🏈 Sole Survivor Role</label>
            <span data-picker="role_sole_survivor_id"></span></div>
        </div>
        <details class="panel-about">
          <summary>How the roles behave</summary>
          <div class="note">All three are created automatically at season
            creation if missing; repoint them here if you'd rather use your
            own. Death swaps Survivor → Ghost; both roles are pinged by the
            two weekly posts.</div>
        </details>
      </div>

      ${RULES_CARDS.map(([title, fields]) => renderRulesCard(title, fields, c)).join("")}

      <div class="row-8">
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
        ? `<button type="button" class="btn btn-sm"
              data-revive="${p.user_id}">Revive</button>`
        : `wk <input type="number" min="1" max="18" value="1"
              data-week="${p.user_id}" style="width:56px;" />
            <button type="button" class="btn btn-sm btn-danger"
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
        if (!await confirmDialog(
          `Eliminate this member in week ${week}? Revive can undo it.`,
          { danger: true, confirmLabel: "Eliminate" })) return;
        await apiPost(`/api/survivor/player/${uid}/eliminate`, { week });
      } else if (revive) {
        if (!await confirmDialog("Revive this member?",
          { confirmLabel: "Revive" })) return;
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

