import { api, esc } from "../api.js";
import { loadRoles, roleSelect } from "../config-helpers.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  const html = `
    <div class="panel">
      <header>
        <h2>Inactive Role Members</h2>
        <div class="subtitle">Members of a role inactive for N days</div>
      </header>
      <div class="controls">
        <label>Role
          <select data-control="role"><option value="0">Loading…</option></select>
        </label>
        <label>Days
          <input type="number" data-control="days" min="1" max="365" value="${initialParams.days || 7}" />
        </label>
      </div>
      <div data-status></div>
      <div data-table-wrap style="margin-top:12px; max-height:500px; overflow-y:auto;"></div>
    </div>
  `;
  container.innerHTML = html;

  const roleEl = container.querySelector('[data-control="role"]');
  const daysEl = container.querySelector('[data-control="days"]');
  const statusEl = container.querySelector('[data-status]');
  const tableWrap = container.querySelector('[data-table-wrap]');

  (async () => {
    const roles = await loadRoles();
    roleEl.innerHTML = roleSelect(roles, initialParams.role_id || "0");
    if (initialParams.role_id && initialParams.role_id !== "0") refresh();
  })();

  async function refresh() {
    if (!roleEl.value || roleEl.value === "0") return;
    const days = parseInt(daysEl.value) || 7;
    statusEl.textContent = "Loading…";
    history.replaceState(null, "", `#/inactive-role?role_id=${encodeURIComponent(roleEl.value)}&days=${days}`);
    try {
      const data = await api("/api/reports/inactive-role", { role_id: roleEl.value, days });
      const pct = data.total ? ((data.inactive_count / data.total) * 100).toFixed(1) : 0;
      statusEl.textContent = `${data.role_name} — ${data.inactive_count}/${data.total} inactive (${pct}%) over ${data.days}d. Tracking coverage: ${data.tracking_coverage}/${data.total}.`;
      renderSortableTable(tableWrap, {
        columns: [
          { key: "display_name", label: "Member", format: (v, r) => esc(r.display_name || r.user_id) },
          { key: "days_since_last", label: "Days idle", format: (v) => v == null ? "(never tracked)" : v },
        ],
        data: data.members,
        defaultSort: "days_since_last",
      });
    } catch (err) {
      statusEl.textContent = `Error: ${err.message}`;
      tableWrap.textContent = "";
    }
  }
  roleEl.addEventListener("change", refresh);
  daysEl.addEventListener("change", refresh);
  return { unmount() {} };
}
