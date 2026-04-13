import { api } from "../api.js";
import { showTranscript } from "../transcript-modal.js";

const STATUS_BADGE = {
  active:   '<span class="badge badge-danger">Active</span>',
  released: '<span class="badge badge-success">Released</span>',
};

function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
         d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function fmtDuration(start, end) {
  if (!end) return "—";
  const s = Math.round(end - start);
  if (s < 60) return "<1m";
  const parts = [];
  if (s >= 86400) { parts.push(Math.floor(s / 86400) + "d"); }
  if (s % 86400 >= 3600) { parts.push(Math.floor((s % 86400) / 3600) + "h"); }
  if (s % 3600 >= 60) { parts.push(Math.floor((s % 3600) / 60) + "m"); }
  return parts.join(" ");
}

function timeRemaining(expiresAt) {
  if (!expiresAt) return "Indefinite";
  const remaining = expiresAt - Date.now() / 1000;
  if (remaining <= 0) return "Expiring...";
  return fmtDuration(0, remaining);
}

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Jails</h2>
        <div class="subtitle">Active and historical jail records</div>
      </header>
      <div class="controls">
        <label>Status
          <select data-control="status">
            <option value="">All</option>
            <option value="active">Active</option>
            <option value="released">Released</option>
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
      const data = await api("/api/moderation/jails", params);

      statsEl.innerHTML = `
        <div class="stat-card stat-danger"><div class="stat-value">${data.active_count}</div><div class="stat-label">Active</div></div>
        <div class="stat-card"><div class="stat-value">${data.total_count}</div><div class="stat-label">Total</div></div>
      `;

      if (!data.jails.length) {
        tableWrap.innerHTML = '<div class="empty">No jail records found.</div>';
        return;
      }

      const rows = data.jails.map((j) => `
        <tr class="clickable-row" data-record-type="jail" data-record-id="${j.id}">
          <td>${STATUS_BADGE[j.status] || j.status}</td>
          <td class="user-cell">${esc(j.user_name || j.user_id)}</td>
          <td>${esc(j.moderator_name || j.moderator_id)}</td>
          <td class="reason-cell" title="${esc(j.reason)}">${esc(j.reason || "—")}</td>
          <td>${fmtTs(j.created_at)}</td>
          <td>${j.status === "active" ? timeRemaining(j.expires_at) : fmtDuration(j.created_at, j.released_at || j.expires_at)}</td>
          <td>${j.released_at ? fmtTs(j.released_at) : "—"}</td>
        </tr>
      `).join("");

      tableWrap.innerHTML = `
        <table class="data-table">
          <thead><tr>
            <th>Status</th><th>User</th><th>Moderator</th><th>Reason</th>
            <th>Jailed</th><th>${statusEl.value === "active" ? "Remaining" : "Duration"}</th><th>Released</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      `;
      tableWrap.querySelector("tbody")?.addEventListener("click", (e) => {
        const row = e.target.closest("tr.clickable-row");
        if (row) showTranscript(row.dataset.recordType, row.dataset.recordId);
      });
    } catch (err) {
      tableWrap.innerHTML = `<div class="error">${err.message}</div>`;
    }
  }

  statusEl.addEventListener("change", refresh);
  refresh();

  return { unmount() {} };
}

function esc(s) {
  if (!s) return "";
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}
