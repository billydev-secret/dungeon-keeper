import { api, esc, fmtTs } from "../api.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";

// All user-supplied content rendered via innerHTML uses esc() for XSS safety.

const GAME_TYPES = ["wyr", "nhie", "mlt", "rushmore", "price", "clapback", "ama", "photo"];
const GAME_ICONS = { wyr: "🤔", nhie: "⛔", mlt: "👑", rushmore: "🗿", price: "💰", clapback: "⚔️", ama: "🎙️", photo: "📸" };
const GAME_NAMES = {
  wyr: "Would You Rather", nhie: "Never Have I Ever", mlt: "Most Likely To",
  rushmore: "Mt. Rushmore Draft", price: "Name Your Price", clapback: "Clapback", ama: "Anonymous AMA",
  photo: "Photo Challenge",
};

// History timestamps are ISO strings recorded in UTC, and some are naive (no
// offset) — Date() would read those as local time, so pin them to UTC before
// handing them to the shared local-time formatter (W-D12).
function fmtGameTs(iso) {
  if (!iso) return "—";
  const s = String(iso).trim();
  const hasZone = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s);
  return fmtTs(hasZone ? s : s.replace(" ", "T") + "Z");
}

function gameLabel(gt) {
  return `${GAME_ICONS[gt] || ""} ${esc(GAME_NAMES[gt] || gt)}`;
}

export function mount(container) {
  let currentPage = 1;
  let currentGameType = "";
  let currentPerPage = 50;

  const gtOptions = GAME_TYPES.map((g) => `<option value="${g}">${GAME_ICONS[g] || ""} ${GAME_NAMES[g] || g}</option>`).join("");

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Overview &amp; Logs</h2>
        <div class="subtitle">Game session statistics and history.</div>
      </header>

      <section>
        <div class="section-label">Stats</div>
        <div class="card-grid" data-region="stats">
          ${renderLoading("Loading stats…")}
        </div>
      </section>

      <section style="margin-top:20px;">
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px;align-items:start;">
          <div>
            <div class="section-label">Games by Type</div>
            <div data-region="by-type">${renderLoading("Loading…")}</div>
          </div>
          <div>
            <div class="section-label">Recent Sessions</div>
            <div data-region="recent">${renderLoading("Loading…")}</div>
          </div>
        </div>
      </section>

      <section style="margin-top:20px;">
        <div class="section-label">Game History</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-bottom:10px;">
          <div class="field" style="margin:0;">
            <label>Game Type
              <select data-ctrl="filter-gt">
                <option value="">All</option>
                ${gtOptions}
              </select>
            </label>
          </div>
          <div class="field" style="margin:0;">
            <label>Per Page
              <select data-ctrl="per-page">
                <option value="25">25</option>
                <option value="50" selected>50</option>
                <option value="100">100</option>
              </select>
            </label>
          </div>
          <button class="btn btn-primary" data-action="filter">Filter</button>
        </div>
        <div data-region="table-wrap">${renderLoading("Loading game history…")}</div>
        <div data-region="pagination" style="display:flex;gap:8px;align-items:center;margin-top:8px;"></div>
      </section>
    </div>
  `;

  function ctrl(name) { return container.querySelector(`[data-ctrl="${name}"]`); }
  function region(name) { return container.querySelector(`[data-region="${name}"]`); }

  async function loadStats() {
    try {
      const data = await api("/api/games/stats");

      const statsEl = region("stats");
      const cards = [
        { label: "Total Questions", value: data.total_questions, cls: "stat-warning" },
        { label: "Games Played",    value: data.games_played,    cls: "stat-info" },
        { label: "Rounds Played",   value: data.rounds_played,   cls: "" },
        { label: "Unique Players",  value: data.unique_players,  cls: "" },
      ];
      statsEl.innerHTML = cards.map((c) => `
        <div class="stat ${c.cls}">
          <div class="stat-label">${c.label}</div>
          <div class="stat-value">${c.value ?? 0}</div>
        </div>`).join("");

      const gbt = data.games_by_type || {};
      const byTypeEl = region("by-type");
      const totals = GAME_TYPES.map((gt) => ({ gt, cnt: gbt[gt] || 0 }));
      const maxVal = Math.max(...totals.map((x) => x.cnt), 1);
      const gtRows = totals.map(({ gt, cnt }) => {
        const pct = Math.round((cnt / maxVal) * 100);
        return `<tr>
          <td>${gameLabel(gt)}</td>
          <td class="num" style="color:var(--gold-solid);font-weight:700;">${cnt}</td>
          <td style="width:40%;padding-left:8px;">
            <div style="height:4px;background:var(--rule-soft);border-radius:2px;">
              <div style="height:100%;width:${pct}%;background:var(--gold-solid);border-radius:2px;"></div>
            </div>
          </td>
        </tr>`;
      }).join("");
      byTypeEl.innerHTML = `<table class="data-table">
        <thead><tr><th>Game</th><th class="num">Played</th><th></th></tr></thead>
        <tbody>${gtRows}</tbody>
      </table>`;
    } catch (err) {
      region("stats").innerHTML = renderError(`Couldn't load game stats — ${err.message}. Reload the page to try again.`);
      region("by-type").innerHTML = renderError(`Couldn't load the per-game breakdown — ${err.message}.`);
    }
  }

  async function loadRecent() {
    try {
      const data = await api("/api/games/history", { page: 1, per_page: 10 });
      const el = region("recent");
      if (!data.rows.length) {
        el.innerHTML = renderEmpty("No games played yet. Sessions appear here once someone starts a game in Discord.");
        return;
      }
      el.innerHTML = data.rows.map((r) => {
        const icon = esc(GAME_ICONS[r.game_type] || "");
        const name = esc(GAME_NAMES[r.game_type] || r.game_type);
        const ended = fmtGameTs(r.ended_at);
        return `<div style="display:flex;justify-content:space-between;align-items:baseline;padding:7px 0;border-bottom:1px solid var(--rule-soft);">
          <div>
            <span>${icon}</span>
            <strong style="margin-left:4px;">${name}</strong>
            <span style="margin-left:8px;font-size:12px;color:var(--ink-dim);">${r.player_count ?? 0}p · ${r.round_count ?? 0}r</span>
          </div>
          <span style="font-size:11px;color:var(--ink-mute);white-space:nowrap;margin-left:8px;">${esc(ended)}</span>
        </div>`;
      }).join("");
    } catch (err) {
      region("recent").innerHTML = renderError(`Couldn't load recent sessions — ${err.message}. Reload the page to try again.`);
    }
  }

  async function loadTable() {
    const wrap = region("table-wrap");
    wrap.innerHTML = renderLoading("Loading game history…");
    try {
      const params = { page: currentPage, per_page: currentPerPage };
      if (currentGameType) params.game_type = currentGameType;
      const data = await api("/api/games/history", params);
      renderTable(data);
      renderPagination(data.total, data.page, data.total_pages);
    } catch (err) {
      wrap.innerHTML = renderError(`Couldn't load game history — ${err.message}. Press Filter to try again.`);
    }
  }

  function renderTable(data) {
    const wrap = region("table-wrap");
    if (!data.rows.length) {
      wrap.innerHTML = renderEmpty("No sessions match this filter. Pick All under Game Type, or come back after a game has been played.");
      return;
    }
    const rows = data.rows.map((r) => {
      const icon = esc(GAME_ICONS[r.game_type] || "");
      const name = esc(GAME_NAMES[r.game_type] || r.game_type);
      const started = r.started_at ? fmtGameTs(r.started_at) : "";
      const ended = fmtGameTs(r.ended_at);
      return `<tr>
        <td><span style="margin-right:5px;">${icon}</span>${name}</td>
        <td class="num">${r.player_count ?? "—"}</td>
        <td class="num">${r.round_count ?? "—"}</td>
        <td style="font-size:12px;color:var(--ink-dim);">${esc(started)}</td>
        <td style="font-size:12px;color:var(--ink-dim);">${esc(ended)}</td>
      </tr>`;
    }).join("");
    wrap.innerHTML = `<table class="data-table">
      <thead><tr>
        <th>Game</th><th class="num">Players</th><th class="num">Rounds</th><th>Started</th><th>Ended</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  }

  function renderPagination(total, page, totalPages) {
    const el = region("pagination");
    if (totalPages <= 1) {
      el.innerHTML = `<span style="font-size:12px;color:var(--ink-dim);">${total} session${total === 1 ? "" : "s"}</span>`;
      return;
    }
    el.innerHTML = `
      <button class="btn btn-sm" data-pg="prev" ${page <= 1 ? "disabled" : ""}>Prev</button>
      <span style="font-size:12px;color:var(--ink-dim);">Page ${page} / ${totalPages} (${total} total)</span>
      <button class="btn btn-sm" data-pg="next" ${page >= totalPages ? "disabled" : ""}>Next</button>`;
    el.querySelector('[data-pg="prev"]')?.addEventListener("click", () => { currentPage--; loadTable(); });
    el.querySelector('[data-pg="next"]')?.addEventListener("click", () => { currentPage++; loadTable(); });
  }

  container.querySelector('[data-action="filter"]').addEventListener("click", () => {
    currentGameType = ctrl("filter-gt").value;
    currentPerPage = parseInt(ctrl("per-page").value) || 50;
    currentPage = 1;
    loadTable();
  });

  loadStats();
  loadRecent();
  loadTable();

  return { unmount() {} };
}