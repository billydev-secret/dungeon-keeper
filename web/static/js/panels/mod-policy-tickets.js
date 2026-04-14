import { api, esc } from "../api.js";
import { showTranscript } from "../transcript-modal.js";

const STATUS_BADGE = {
  open:   '<span class="badge badge-info">Open</span>',
  voting: '<span class="badge badge-warning">Voting</span>',
  closed: '<span class="badge badge-dim">Closed</span>',
};

function fmtTs(ts) {
  if (!ts) return "\u2014";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
         d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function fmtAge(ts) {
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
}

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Policy Tickets</h2>
        <div class="subtitle">Community policy proposals and votes</div>
      </header>
      <div class="controls">
        <label>Status
          <select data-control="status">
            <option value="">All</option>
            <option value="open">Open</option>
            <option value="voting">Voting</option>
            <option value="closed">Closed</option>
          </select>
        </label>
      </div>
      <div class="mod-stats" data-stats></div>
      <div class="table-scroll" data-table-wrap>
        <div class="empty">Loading...</div>
      </div>
    </div>
  `;

  const statusEl = container.querySelector('[data-control="status"]');
  const statsEl = container.querySelector("[data-stats]");
  const tableWrap = container.querySelector("[data-table-wrap]");

  async function refresh() {
    const params = {};
    if (statusEl.value) params.status = statusEl.value;

    try {
      const data = await api("/api/moderation/policy-tickets", params);

      statsEl.innerHTML = `
        <div class="stat-card stat-info"><div class="stat-value">${data.open_count}</div><div class="stat-label">Open</div></div>
        <div class="stat-card stat-warning"><div class="stat-value">${data.voting_count}</div><div class="stat-label">Voting</div></div>
        <div class="stat-card"><div class="stat-value">${data.closed_count}</div><div class="stat-label">Closed</div></div>
        <div class="stat-card"><div class="stat-value">${data.total_count}</div><div class="stat-label">Total</div></div>
      `;

      if (!data.policy_tickets.length) {
        tableWrap.innerHTML = '<div class="empty">No policy tickets found.</div>';
        return;
      }

      const rows = data.policy_tickets.map((t) => {
        const badge = STATUS_BADGE[t.status] || t.status;
        const timeCol = t.status === "voting"
          ? fmtTs(t.vote_started_at)
          : t.status === "closed"
            ? fmtTs(t.vote_ended_at)
            : fmtAge(t.created_at) + " ago";

        return `
          <tr class="clickable-row" data-record-type="policy_ticket" data-record-id="${t.id}">
            <td>${badge}</td>
            <td>#${t.id}</td>
            <td class="user-cell">${esc(t.creator_name || t.creator_id)}</td>
            <td class="reason-cell" title="${esc(t.title)}">${esc(t.title || "\u2014")}</td>
            <td class="reason-cell" title="${esc(t.description)}">${esc(t.description || "\u2014")}</td>
            <td>${fmtTs(t.created_at)}</td>
            <td>${timeCol}</td>
          </tr>
        `;
      }).join("");

      tableWrap.innerHTML = `
        <table class="data-table">
          <thead><tr>
            <th>Status</th><th>ID</th><th>Creator</th><th>Title</th>
            <th>Description</th><th>Created</th><th>${statusEl.value === "voting" ? "Vote Started" : statusEl.value === "closed" ? "Vote Ended" : "Age"}</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      `;
      tableWrap.querySelector("tbody")?.addEventListener("click", (e) => {
        const row = e.target.closest("tr.clickable-row");
        if (row) showTranscript(row.dataset.recordType, row.dataset.recordId);
      });
    } catch (err) {
      tableWrap.innerHTML = `<div class="error">${esc(err.message)}</div>`;
    }
  }

  statusEl.addEventListener("change", refresh);
  refresh();

  return { unmount() {} };
}
