import { api, esc } from "../api.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  const html = `
    <div class="panel">
      <header>
        <h2>Oldest SFW Members</h2>
        <div class="subtitle">Members without NSFW access, ranked by longest silence</div>
      </header>
      <div class="controls">
        <label>Count
          <input type="number" data-control="count" min="1" max="100" value="${initialParams.count || 10}" />
        </label>
      </div>
      <div data-status></div>
      <div data-table-wrap style="margin-top:12px; max-height:500px; overflow-y:auto;"></div>
    </div>
  `;
  container.innerHTML = html;

  const countEl = container.querySelector('[data-control="count"]');
  const statusEl = container.querySelector('[data-status]');
  const tableWrap = container.querySelector('[data-table-wrap]');

  async function refresh() {
    statusEl.textContent = "Loading…";
    try {
      const data = await api("/api/reports/oldest-sfw", { count: parseInt(countEl.value) || 10 });
      const roleLabel = data.nsfw_role_name || "(NSFW role not configured)";
      statusEl.textContent = `${data.members.length} of ${data.sfw_total} SFW members shown (no @${roleLabel}).`;
      renderSortableTable(tableWrap, {
        columns: [
          { key: "display_name", label: "Member", format: (v, r) => esc(r.display_name || r.user_id) },
          { key: "days_since_last", label: "Days since last msg", format: (v) => v == null ? "(never tracked)" : v },
        ],
        data: data.members,
        defaultSort: "days_since_last",
      });
    } catch (err) {
      statusEl.textContent = `Error: ${err.message}`;
      tableWrap.textContent = "";
    }
  }
  countEl.addEventListener("change", refresh);
  refresh();
  return { unmount() {} };
}
