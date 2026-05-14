import { api, esc } from "../api.js";

// All user-supplied content rendered via innerHTML uses esc() for XSS safety.

const GAME_TYPES = ["wyr", "nhie", "mlt", "rushmore", "price", "clapback", "ama"];

export function mount(container) {
  let currentPage = 1;
  let currentGameType = "";
  let currentPerPage = 50;

  const gtOptions = GAME_TYPES.map((g) => `<option value="${g}">${g}</option>`).join("");

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Overview &amp; Logs</h2>
        <div class="subtitle">Game session statistics and history from the games_game_history table.</div>
      </header>

      <section>
        <div class="section-label">Stats</div>
        <div data-region="stats" style="display:flex;gap:12px;flex-wrap:wrap;">
          <div class="empty">Loading</div>
        </div>
      </section>

      <section style="margin-top:20px;">
        <div class="section-label">Game History</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-bottom:10px;">
          <div class="field" style="margin:0;">
            <label>Game type
              <select data-ctrl="filter-gt">
                <option value="">All</option>
                ${gtOptions}
              </select>
            </label>
          </div>
          <div class="field" style="margin:0;">
            <label>Per page
              <select data-ctrl="per-page">
                <option value="25">25</option>
                <option value="50" selected>50</option>
                <option value="100">100</option>
              </select>
            </label>
          </div>
          <button class="btn btn-primary" data-action="filter">Filter</button>
        </div>
        <div data-region="table-wrap"><div class="empty">Loading</div></div>
        <div data-region="pagination" style="display:flex;gap:8px;align-items:center;margin-top:8px;"></div>
      </section>
    </div>
  `;

  function ctrl(name) { return container.querySelector(`[data-ctrl="${name}"]`); }
  function region(name) { return container.querySelector(`[data-region="${name}"]`); }

  async function loadStats() {
    try {
      const data = await api("/api/games/stats");
      const el = region("stats");
      const cards = [
        { label: "Total Questions", value: data.total_questions },
        { label: "Games Played", value: data.games_played },
        { label: "Rounds Played", value: data.rounds_played },
        { label: "Unique Players", value: data.unique_players },
      ];
      el.innerHTML = cards.map((c) => `
        <div style="background:var(--bg-2,#1e1e2e);border:1px solid var(--border,#333);border-radius:8px;padding:16px 20px;min-width:140px;text-align:center;">
          <div style="font-size:28px;font-weight:700;">${c.value}</div>
          <div style="font-size:12px;color:var(--muted,#888);margin-top:4px;">${esc(c.label)}</div>
        </div>`).join("");
    } catch (_) {
      region("stats").innerHTML = `<div class="empty">Stats unavailable</div>`;
    }
  }

  async function loadTable() {
    const wrap = region("table-wrap");
    wrap.innerHTML = `<div class="empty">Loading</div>`;
    try {
      const params = { page: currentPage, per_page: currentPerPage };
      if (currentGameType) params.game_type = currentGameType;
      const data = await api("/api/games/history", params);
      renderTable(data);
      renderPagination(data.total, data.page, data.total_pages);
    } catch (err) {
      wrap.innerHTML = `<div class="empty">Error: ${esc(err.message)}</div>`;
    }
  }

  function renderTable(data) {
    const wrap = region("table-wrap");
    if (!data.rows.length) {
      wrap.innerHTML = `<div class="empty">No game history found.</div>`;
      return;
    }
    let rows = "";
    for (const r of data.rows) {
      const started = r.started_at ? String(r.started_at).slice(0, 16).replace("T", " ") : "";
      const ended = r.ended_at ? String(r.ended_at).slice(0, 16).replace("T", " ") : "—";
      rows += `<tr>
        <td>${esc(r.game_type)}</td>
        <td>${r.player_count ?? "—"}</td>
        <td>${r.round_count ?? "—"}</td>
        <td style="font-size:12px;">${esc(started)}</td>
        <td style="font-size:12px;">${esc(ended)}</td>
      </tr>`;
    }
    wrap.innerHTML = `<table style="width:100%;">
      <thead><tr>
        <th>Type</th><th>Players</th><th>Rounds</th><th>Started</th><th>Ended</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  }

  function renderPagination(total, page, totalPages) {
    const el = region("pagination");
    if (totalPages <= 1) { el.innerHTML = `<span style="font-size:12px;">${total} session(s)</span>`; return; }
    el.innerHTML = `
      <button class="btn" data-pg="prev" ${page <= 1 ? "disabled" : ""}>Prev</button>
      <span style="font-size:12px;">Page ${page} / ${totalPages} (${total} total)</span>
      <button class="btn" data-pg="next" ${page >= totalPages ? "disabled" : ""}>Next</button>`;
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
  loadTable();

  return { unmount() {} };
}