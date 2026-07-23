import { api, esc } from "../api.js";
import { withLoading } from "../report-helpers.js";
import { renderSortableTable } from "../table.js";
import { renderLoading, renderError } from "../states.js";

export function mount(container, initialParams) {
  const html = `
    <div class="panel">
      <header>
        <h2>Oldest SFW Members</h2>
        <div class="subtitle">Members without NSFW access, ranked by longest silence</div>
      </header>
      <div class="controls">
        <label>Members to Show
          <input type="number" data-control="count" min="1" max="100" value="${initialParams.count || 10}" />
        </label>
      </div>
      <div data-status></div>
      <div data-table-wrap style="margin-top:12px; max-height:500px; overflow-y:auto;">
        ${renderLoading("Loading SFW members…")}
      </div>
    </div>
  `;
  container.innerHTML = html;

  const countEl = container.querySelector('[data-control="count"]');
  const statusEl = container.querySelector('[data-status]');
  const tableWrap = container.querySelector('[data-table-wrap]');

  async function refresh() {
    statusEl.textContent = "Loading…";
    try {
      const data = await withLoading(tableWrap, api("/api/reports/oldest-sfw", { count: parseInt(countEl.value) || 10 }));
      statusEl.textContent = data.nsfw_role_name
        ? `Showing ${data.members.length} of ${data.sfw_total} members who don't have @${data.nsfw_role_name}.`
        : `Showing ${data.members.length} of ${data.sfw_total} members. No NSFW role is set yet — `
          + "pick one in Config › Role Grants to make this list meaningful.";
      renderSortableTable(tableWrap, {
        columns: [
          { key: "display_name", label: "Member", format: (v, r) => esc(r.display_name || r.user_id) },
          { key: "days_since_last", label: "Days Since Last Message", format: (v) => v == null ? "Never tracked" : v },
        ],
        data: data.members,
        defaultSort: "days_since_last",
        emptyMsg: "No members without NSFW access were found. Either everyone already has the "
          + "role, or the NSFW role isn't set yet in Config › Role Grants.",
        maxRows: 200,
      });
    } catch (err) {
      statusEl.textContent = "";
      tableWrap.innerHTML = renderError(`Couldn't load the SFW member list — try again. (${err.message})`);
    }
  }
  countEl.addEventListener("change", refresh);
  refresh();
  return { unmount() {} };
}
