import { api, esc } from "../api.js";

const ACTION_LABELS = {
  jail:         "Jail",
  unjail:       "Unjail",
  ticket_open:  "Ticket Open",
  ticket_close: "Ticket Close",
  ticket_reopen: "Ticket Reopen",
  ticket_delete: "Ticket Delete",
  ticket_claim: "Ticket Claim",
  ticket_escalate: "Ticket Escalate",
  warn:         "Warning",
  warn_revoke:  "Warning Revoke",
  pull:         "Pull to Channel",
  remove:       "Remove from Channel",
};

const ACTION_COLORS = {
  jail: "badge-danger",
  unjail: "badge-success",
  warn: "badge-warning",
  ticket_open: "badge-info",
  ticket_close: "badge-dim",
};

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
        <h2>Audit Log</h2>
        <div class="subtitle">Moderation action history</div>
      </header>
      <div class="controls">
        <label>Action
          <select data-control="action">
            <option value="">All</option>
            <option value="jail">Jail</option>
            <option value="unjail">Unjail</option>
            <option value="warn">Warning</option>
            <option value="ticket_open">Ticket Open</option>
            <option value="ticket_close">Ticket Close</option>
          </select>
        </label>
        <label>Show
          <select data-control="limit">
            <option value="50">50</option>
            <option value="100">100</option>
            <option value="200">200</option>
          </select>
        </label>
      </div>
      <div class="table-scroll" data-table-wrap>
        <div class="empty">Loading...</div>
      </div>
    </div>
  `;

  const actionEl = container.querySelector('[data-control="action"]');
  const limitEl = container.querySelector('[data-control="limit"]');
  const tableWrap = container.querySelector("[data-table-wrap]");

  async function refresh() {
    const params = { limit: limitEl.value };
    if (actionEl.value) params.action = actionEl.value;

    try {
      const data = await api("/api/moderation/audit", params);

      if (!data.entries.length) {
        tableWrap.innerHTML = '<div class="empty">No audit entries found.</div>';
        return;
      }

      const rows = data.entries.map((e) => {
        const label = ACTION_LABELS[e.action] || e.action;
        const cls = ACTION_COLORS[e.action] || "";
        const extra = e.extra && e.extra.reason ? esc(e.extra.reason) : "—";
        return `
          <tr>
            <td><span class="badge ${cls}">${esc(label)}</span></td>
            <td>${esc(e.actor_name || e.actor_id)}</td>
            <td class="user-cell">${e.target_name ? esc(e.target_name) : (e.target_id ? esc(e.target_id) : "—")}</td>
            <td class="reason-cell" title="${extra}">${extra}</td>
            <td>${fmtTs(e.created_at)}</td>
          </tr>
        `;
      }).join("");

      tableWrap.innerHTML = `
        <div style="color:var(--ink-dim);font-size:12px;margin-bottom:8px;">Showing ${data.entries.length} of ${data.total} entries</div>
        <table class="data-table">
          <thead><tr>
            <th>Action</th><th>Actor</th><th>Target</th><th>Details</th><th>Time</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      `;
    } catch (err) {
      tableWrap.innerHTML = `<div class="error">${esc(err.message)}</div>`;
    }
  }

  actionEl.addEventListener("change", refresh);
  limitEl.addEventListener("change", refresh);
  refresh();

  return { unmount() {} };
}
