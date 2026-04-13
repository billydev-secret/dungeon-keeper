import { api } from "../api.js";

function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
         d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Warnings</h2>
        <div class="subtitle">Active and expired warnings</div>
      </header>
      <div class="controls">
        <label><input type="checkbox" data-control="active-only"> Active only</label>
      </div>
      <div class="mod-stats" data-stats></div>
      <div class="table-scroll" data-table-wrap>
        <div class="empty">Loading...</div>
      </div>
    </div>
  `;

  const activeOnlyEl = container.querySelector('[data-control="active-only"]');
  const statsEl = container.querySelector("[data-stats]");
  const tableWrap = container.querySelector("[data-table-wrap]");

  async function refresh() {
    const params = {};
    if (activeOnlyEl.checked) params.active_only = "true";

    try {
      const data = await api("/api/moderation/warnings", params);

      statsEl.innerHTML = `
        <div class="stat-card stat-warning"><div class="stat-value">${data.active_count}</div><div class="stat-label">Active</div></div>
        <div class="stat-card"><div class="stat-value">${data.total_count}</div><div class="stat-label">Total</div></div>
      `;

      if (!data.warnings.length) {
        tableWrap.innerHTML = '<div class="empty">No warnings found.</div>';
        return;
      }

      const rows = data.warnings.map((w) => {
        const badge = w.revoked
          ? '<span class="badge badge-dim">Revoked</span>'
          : '<span class="badge badge-warning">Active</span>';
        return `
          <tr>
            <td>${badge}</td>
            <td class="user-cell">${esc(w.user_name || w.user_id)}</td>
            <td>${esc(w.moderator_name || w.moderator_id)}</td>
            <td class="reason-cell" title="${esc(w.reason)}">${esc(w.reason || "—")}</td>
            <td>${fmtTs(w.created_at)}</td>
            <td>${w.revoked ? esc(w.revoker_name || w.revoked_by || "") : "—"}</td>
            <td>${w.revoked ? esc(w.revoke_reason || "—") : "—"}</td>
          </tr>
        `;
      }).join("");

      tableWrap.innerHTML = `
        <table class="data-table">
          <thead><tr>
            <th>Status</th><th>User</th><th>Issued By</th><th>Reason</th>
            <th>Date</th><th>Revoked By</th><th>Revoke Reason</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      `;
    } catch (err) {
      tableWrap.innerHTML = `<div class="error">${err.message}</div>`;
    }
  }

  activeOnlyEl.addEventListener("change", refresh);
  refresh();

  return { unmount() {} };
}

function esc(s) {
  if (!s) return "";
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}
