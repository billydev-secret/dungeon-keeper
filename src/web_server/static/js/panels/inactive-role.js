import { api, esc } from "../api.js";
import { rangePicker, withLoading } from "../report-helpers.js";
import { loadRoles, roleSelect, metaLoadFailed } from "../config-helpers.js";
import { renderSortableTable } from "../table.js";
import { renderEmpty, renderError } from "../states.js";

export function mount(container, initialParams) {
  const html = `
    <div class="panel">
      <header>
        <h2>Inactive Role Members</h2>
        <div class="subtitle">Members who hold a role but haven't posted recently. Pairs with Config › Auto-Remove Role (Inactive).</div>
      </header>
      <div class="controls">
        <label>Role
          <select data-control="role"><option value="0">Loading roles…</option></select>
        </label>
      </div>
      <div data-status></div>
      <div data-table-wrap style="margin-top:12px; max-height:500px; overflow-y:auto;">
        ${renderEmpty("Pick a role above to see which of its members have gone quiet.")}
      </div>
    </div>
  `;
  container.innerHTML = html;

  const roleEl = container.querySelector('[data-control="role"]');
  const rangeEl = rangePicker({ value: initialParams.days || 7, allowAll: false, label: "Days" });
  container.querySelector(".controls").appendChild(rangeEl);
  const daysEl = rangeEl.querySelector("select");
  const statusEl = container.querySelector('[data-status]');
  const tableWrap = container.querySelector('[data-table-wrap]');

  (async () => {
    const roles = await loadRoles();
    roleEl.innerHTML = roleSelect(roles, initialParams.role_id || "0");
    if (metaLoadFailed()) {
      statusEl.className = "error";
      statusEl.textContent = "Couldn't load the role list — reload the page to try again.";
      return;
    }
    if (initialParams.role_id && initialParams.role_id !== "0") refresh();
  })();

  async function refresh() {
    if (!roleEl.value || roleEl.value === "0") {
      tableWrap.innerHTML = renderEmpty("Pick a role above to see which of its members have gone quiet.");
      return;
    }
    const days = parseInt(daysEl.value) || 7;
    statusEl.className = "";
    statusEl.textContent = "Loading members…";
    history.replaceState(null, "", `#/inactive-role?role_id=${encodeURIComponent(roleEl.value)}&days=${days}`);
    try {
      const data = await withLoading(tableWrap, api("/api/reports/inactive-role", { role_id: roleEl.value, days }));
      const pct = data.total ? ((data.inactive_count / data.total) * 100).toFixed(1) : 0;
      statusEl.textContent =
        `@${data.role_name} — ${data.inactive_count} of ${data.total} members inactive (${pct}%) `
        + `over the last ${data.days} day${data.days === 1 ? "" : "s"}. `
        + `Activity is tracked for ${data.tracking_coverage} of ${data.total}.`;
      renderSortableTable(tableWrap, {
        columns: [
          { key: "display_name", label: "Member", format: (v, r) => esc(r.display_name || r.user_id) },
          { key: "days_since_last", label: "Days Idle", format: (v) => v == null ? "Never tracked" : v },
        ],
        data: data.members,
        defaultSort: "days_since_last",
        emptyMsg: "Nobody with this role has been idle that long. Widen the day range to catch more members.",
        maxRows: 500,
      });
    } catch (err) {
      statusEl.textContent = "";
      tableWrap.innerHTML = renderError(`Couldn't load inactive members for that role — try again. (${err.message})`);
    }
  }
  roleEl.addEventListener("change", refresh);
  daysEl.addEventListener("change", refresh);
  return { unmount() {} };
}
