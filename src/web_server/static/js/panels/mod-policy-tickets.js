import { api, esc, fmtTs, fmtAge } from "../api.js";
import { showTranscript } from "../transcript-modal.js";
import { makeFilterStrip } from "../tab-strip.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";

const STATUS_BADGE = {
  open:   '<span class="badge badge-info">Open</span>',
  voting: '<span class="badge badge-warning">Voting</span>',
  closed: '<span class="badge badge-dim">Closed</span>',
};

// Lowercased blob of visible text (title, description, creator) plus metadata
// that never renders (raw IDs, status) so a query can match on either.
function policyHaystack(t) {
  return [t.id, `#${t.id}`, t.status, t.creator_name, t.creator_id, t.title, t.description]
    .filter((v) => v != null && v !== "")
    .join(" ")
    .toLowerCase();
}

function matchesSearch(t, query) {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return true;
  const hay = policyHaystack(t);
  return terms.every((term) => hay.includes(term));
}

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
        <div class="ctrl-group" role="group" aria-label="Filter policy tickets" data-filter-group>
          <button class="active" data-filter="">All</button>
          <button data-filter="open">Open</button>
          <button data-filter="voting">Voting</button>
          <button data-filter="closed">Closed</button>
        </div>
      </div>

      <div class="ticket-search" style="border:0;padding:0 0 8px">
        <input type="search" data-search autocomplete="off"
          placeholder="Search title, description, creator, ID…"
          aria-label="Search policy tickets" />
      </div>

      <div class="table-scroll" data-table-wrap>
        ${renderLoading("Loading...")}
      </div>
    </div>
  `;

  const filterGroup = container.querySelector("[data-filter-group]");
  const statsEl = container.querySelector("[data-stats]");
  const tableWrap = container.querySelector("[data-table-wrap]");
  const searchEl = container.querySelector("[data-search]");

  let currentFilter = "";
  let currentSearch = "";
  let loadedTickets = [];

  // A search spans every status regardless of the active tab, so it needs the
  // full unfiltered set. Fetched once and cached; guarded against overlapping
  // fetches from rapid typing.
  let allTickets = null;
  let allPromise = null;
  function ensureAll() {
    if (allTickets) return Promise.resolve();
    if (!allPromise) {
      allPromise = api("/api/moderation/policy-tickets")
        .then((data) => { allTickets = data.policy_tickets || []; })
        .catch((err) => { console.error("Failed to load all policy tickets for search:", err); allTickets = []; })
        .finally(() => { allPromise = null; });
    }
    return allPromise;
  }

  function renderTable() {
    const searching = currentSearch.trim() !== "";
    const source = searching ? (allTickets || []) : loadedTickets;
    const tickets = source.filter((t) => matchesSearch(t, currentSearch));
    if (!tickets.length) {
      tableWrap.innerHTML = renderEmpty(
        currentSearch ? "No policy tickets match your search." : "No policy tickets found.",
      );
      return;
    }

    const timeHeader = currentFilter === "voting" ? "Vote Started" : currentFilter === "closed" ? "Vote Ended" : "Age";
    const rows = tickets.map((t) => {
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
          <td class="reason-cell" title="${esc(t.title)}">${esc(t.title || "—")}</td>
          <td class="reason-cell" title="${esc(t.description)}">${esc(t.description || "—")}</td>
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
  }

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

      loadedTickets = data.policy_tickets || [];
      renderTable();
    } catch (err) {
      tableWrap.innerHTML = renderError(err);
    }
  }

  makeFilterStrip(filterGroup, (value) => {
    currentFilter = value;
    refresh();
  });

  searchEl.addEventListener("input", async () => {
    currentSearch = searchEl.value;
    if (currentSearch.trim() && !allTickets) {
      await ensureAll();
    }
    renderTable();
  });

  refresh();

  return { unmount() {} };
}
