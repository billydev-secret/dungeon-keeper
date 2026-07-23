import { api, esc } from "../api.js";
import { withLoading } from "../report-helpers.js";
import { loadRoles, roleSelect, metaLoadFailed } from "../config-helpers.js";
import { renderSortableTable } from "../table.js";
import { renderEmpty, renderError } from "../states.js";

export function mount(container, initialParams) {
  const html = `
    <div class="panel">
      <header>
        <h2>List Role</h2>
        <div class="subtitle">Everyone who holds a role, with how long they've been quiet</div>
      </header>
      <div class="controls">
        <label>Role
          <select data-control="role"><option value="0">Loading roles…</option></select>
        </label>
      </div>
      <div data-status></div>
      <div data-table-wrap style="margin-top:12px; max-height:500px; overflow-y:auto;">
        ${renderEmpty("Pick a role above to see everyone who has it.")}
      </div>
    </div>
  `;
  container.innerHTML = html;

  const roleEl = container.querySelector('[data-control="role"]');
  const statusEl = container.querySelector('[data-status]');
  const tableWrap = container.querySelector('[data-table-wrap]');

  (async () => {
    const roles = await loadRoles();
    roleEl.innerHTML = roleSelect(roles, initialParams.role_id || "0");
    if (metaLoadFailed()) {
      statusEl.textContent = "Couldn't load the role list — reload the page to try again.";
      statusEl.className = "error";
      return;
    }
    if (initialParams.role_id && initialParams.role_id !== "0") refresh();
  })();

  async function refresh() {
    if (!roleEl.value || roleEl.value === "0") {
      tableWrap.innerHTML = renderEmpty("Pick a role above to see everyone who has it.");
      return;
    }
    statusEl.className = "";
    statusEl.textContent = "Loading members…";
    history.replaceState(null, "", `#/list-role?role_id=${encodeURIComponent(roleEl.value)}`);
    try {
      const data = await withLoading(tableWrap, api("/api/reports/list-role", { role_id: roleEl.value }));
      statusEl.textContent = `@${data.role_name} — ${data.total} member${data.total === 1 ? "" : "s"}.`;
      renderSortableTable(tableWrap, {
        columns: [
          { key: "display_name", label: "Member", format: (v, r) => esc(r.display_name || r.user_id) },
          { key: "days_since_last", label: "Days Since Last Message", format: (v) => v == null ? "—" : v },
        ],
        data: data.members,
        defaultSort: "display_name",
        defaultAsc: true,
        emptyMsg: "Nobody holds this role right now. Grant it in Discord and the members will show up here.",
        maxRows: 500,
      });
    } catch (err) {
      statusEl.textContent = "";
      tableWrap.innerHTML = renderError(`Couldn't load the members of that role — try again. (${err.message})`);
    }
  }
  roleEl.addEventListener("change", refresh);
  return { unmount() {} };
}
