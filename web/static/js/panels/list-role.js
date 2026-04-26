import { api, esc } from "../api.js";
import { loadRoles, roleSelect } from "../config-helpers.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  const html = `
    <div class="panel">
      <header>
        <h2>List Role</h2>
        <div class="subtitle">All members of a role with last activity</div>
      </header>
      <div class="controls">
        <label>Role
          <select data-control="role"><option value="0">Loading…</option></select>
        </label>
      </div>
      <div data-status></div>
      <div data-table-wrap style="margin-top:12px; max-height:500px; overflow-y:auto;"></div>
    </div>
  `;
  container.innerHTML = html;

  const roleEl = container.querySelector('[data-control="role"]');
  const statusEl = container.querySelector('[data-status]');
  const tableWrap = container.querySelector('[data-table-wrap]');

  (async () => {
    const roles = await loadRoles();
    roleEl.innerHTML = roleSelect(roles, initialParams.role_id || "0");
    if (initialParams.role_id && initialParams.role_id !== "0") refresh();
  })();

  async function refresh() {
    if (!roleEl.value || roleEl.value === "0") return;
    statusEl.textContent = "Loading…";
    history.replaceState(null, "", `#/list-role?role_id=${encodeURIComponent(roleEl.value)}`);
    try {
      const data = await api("/api/reports/list-role", { role_id: roleEl.value });
      statusEl.textContent = `${data.role_name} — ${data.total} members`;
      renderSortableTable(tableWrap, {
        columns: [
          { key: "display_name", label: "Member", format: (v, r) => esc(r.display_name || r.user_id) },
          { key: "days_since_last", label: "Days since last msg", format: (v) => v == null ? "—" : v },
        ],
        data: data.members,
        defaultSort: "display_name",
        defaultAsc: true,
      });
    } catch (err) {
      statusEl.textContent = `Error: ${err.message}`;
      tableWrap.textContent = "";
    }
  }
  roleEl.addEventListener("change", refresh);
  return { unmount() {} };
}
