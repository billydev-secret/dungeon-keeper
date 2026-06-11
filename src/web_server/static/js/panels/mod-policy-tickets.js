import { api, esc, fmtTs, fmtAge } from "../api.js";
import { showTranscript } from "../transcript-modal.js";

const STATUS_BADGE = {
  open:   '<span class="badge badge-info">Open</span>',
  voting: '<span class="badge badge-warning">Voting</span>',
  closed: '<span class="badge badge-dim">Closed</span>',
};

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Policy Tickets</h2>
        <div class="subtitle">Community policy proposals and votes</div>
      </header>

      <div class="mod-stats" data-stats>
        <div class="mod-stat open"><div class="lbl">Open</div><div class="v">\u2014</div><div class="sub">loading\u2026</div></div>
        <div class="mod-stat claimed"><div class="lbl">Voting</div><div class="v">\u2014</div><div class="sub"></div></div>
        <div class="mod-stat resolved"><div class="lbl">Closed</div><div class="v">\u2014</div><div class="sub"></div></div>
        <div class="mod-stat avg"><div class="lbl">Total</div><div class="v">\u2014</div><div class="sub"></div></div>
      </div>

      <div class="ticket-list-head" style="margin-bottom:8px">
        <h3>Queue</h3>
        <div class="ctrl-group" role="tablist" data-filter-group>
          <button class="active" data-filter="">All</button>
          <button data-filter="open">Open</button>
          <button data-filter="voting">Voting</button>
          <button data-filter="closed">Closed</button>
        </div>
      </div>

      <div class="table-scroll" data-table-wrap>
        <div class="empty">Loading...</div>
      </div>
    </div>
  `;

  const filterGroup = container.querySelector("[data-filter-group]");
  const statsEl = container.querySelector("[data-stats]");
  const tableWrap = container.querySelector("[data-table-wrap]");

  let currentFilter = "";

  async function refresh() {
    const params = {};
    if (currentFilter) params.status = currentFilter;

    try {
      const data = await api("/api/moderation/policy-tickets", params);

      statsEl.innerHTML = `
        <div class="mod-stat open">
          <div class="lbl">Open</div>
          <div class="v">${data.open_count}</div>
          <div class="sub">${data.open_count === 1 ? "proposal" : "proposals"}</div>
        </div>
        <div class="mod-stat claimed">
          <div class="lbl">Voting</div>
          <div class="v">${data.voting_count}</div>
          <div class="sub">${data.voting_count ? "in progress" : "none"}</div>
        </div>
        <div class="mod-stat resolved">
          <div class="lbl">Closed</div>
          <div class="v">${data.closed_count}</div>
          <div class="sub"></div>
        </div>
        <div class="mod-stat avg">
          <div class="lbl">Total</div>
          <div class="v">${data.total_count}</div>
          <div class="sub"></div>
        </div>
      `;

      if (!data.policy_tickets.length) {
        tableWrap.innerHTML = '<div class="empty">No policy tickets found.</div>';
        return;
      }

      const timeHeader = currentFilter === "voting" ? "Vote Started" : currentFilter === "closed" ? "Vote Ended" : "Age";
      const rows = data.policy_tickets.map((t) => {
        const badge = STATUS_BADGE[t.status] || t.status;
        const timeCol = t.status === "voting"
          ? fmtTs(t.vote_started_at)
          : t.status === "closed"
            ? fmtTs(t.vote_ended_at)
            : fmtAge(Date.now() / 1000 - t.created_at) + " ago";

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
            <th>Description</th><th>Created</th><th>${timeHeader}</th>
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

  filterGroup.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-filter]");
    if (!btn) return;
    filterGroup.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
    currentFilter = btn.dataset.filter;
    refresh();
  });

  refresh();

  return { unmount() {} };
}
