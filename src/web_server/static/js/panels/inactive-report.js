import { api } from "../api.js";
import { withLoading } from "../report-helpers.js";
import { loadChannels, channelSelect, loadRoles, roleSelect, metaLoadFailed } from "../config-helpers.js";
import { renderSortableTable } from "../table.js";
import { renderLoading, renderError } from "../states.js";

const PRESETS = [
  { label: "Any (show everyone)", value: 0 },
  { label: "1 day", value: 1 },
  { label: "3 days", value: 3 },
  { label: "7 days", value: 7 },
  { label: "14 days", value: 14 },
  { label: "30 days", value: 30 },
  { label: "60 days", value: 60 },
  { label: "90 days", value: 90 },
];

export function mount(container, initialParams) {
  const opts = PRESETS.map((p) => `<option value="${p.value}">${p.label}</option>`).join("");
  const html = `
    <div class="panel">
      <header>
        <h2>Inactive Report</h2>
        <div class="subtitle">One member list over last-activity data — scope by role, filter by idle time, oldest first. Pairs with Config › Auto-Remove Role (Inactive) and Inactive Sweep.</div>
      </header>
      <div class="controls">
        <label style="max-width:100%; min-width:0;">Scope
          <select data-control="scope" style="max-width:100%;">
            <option value="all">Everyone</option>
            <option value="with">Has role…</option>
            <option value="without">Lacks role…</option>
          </select>
        </label>
        <label data-role-label hidden style="max-width:100%; min-width:0;">Role
          <select data-control="role" style="max-width:100%;"><option value="0">Loading roles…</option></select>
        </label>
        <label style="max-width:100%; min-width:0;">Quiet For
          <select data-control="days" style="max-width:100%;">${opts}</select>
        </label>
        <label style="max-width:100%; min-width:0;">Channel (Optional)
          <select data-control="channel" style="max-width:100%;"><option value="0">All channels</option></select>
        </label>
      </div>
      <div data-status></div>
      <div data-table-wrap style="margin-top:12px; max-height:500px; overflow-y:auto;">
        ${renderLoading("Loading members…")}
      </div>
    </div>
  `;
  container.innerHTML = html;

  const scopeEl = container.querySelector('[data-control="scope"]');
  const roleLabel = container.querySelector("[data-role-label]");
  const roleEl = container.querySelector('[data-control="role"]');
  const daysEl = container.querySelector('[data-control="days"]');
  const channelEl = container.querySelector('[data-control="channel"]');
  const statusEl = container.querySelector('[data-status]');
  const tableWrap = container.querySelector('[data-table-wrap]');

  daysEl.value = initialParams.days != null ? initialParams.days : 7;
  if (initialParams.role_id && initialParams.role_id !== "0") {
    scopeEl.value = initialParams.role_mode === "without" ? "without" : "with";
    roleLabel.hidden = false;
  }

  (async () => {
    const [channels, roles] = await Promise.all([loadChannels(), loadRoles()]);
    channelEl.innerHTML = channelSelect(channels, initialParams.channel_id || "0");
    roleEl.innerHTML = roleSelect(roles, initialParams.role_id || "0");
    if (metaLoadFailed()) {
      statusEl.className = "error";
      statusEl.textContent = "Couldn't load the role/channel lists — reload the page to try again.";
      return;
    }
    refresh();
  })();

  async function refresh() {
    const scope = scopeEl.value;
    roleLabel.hidden = scope === "all";
    if (scope !== "all" && (!roleEl.value || roleEl.value === "0")) {
      statusEl.className = "";
      statusEl.textContent = "Pick a role to scope the list.";
      return;
    }
    const params = { days: parseInt(daysEl.value, 10) || 0 };
    if (scope !== "all") {
      params.role_id = roleEl.value;
      params.role_mode = scope;
    }
    if (channelEl.value && channelEl.value !== "0") params.channel_id = channelEl.value;
    statusEl.className = "";
    statusEl.textContent = "Loading members…";
    try {
      const data = await withLoading(tableWrap, api("/api/reports/inactive-report", params));
      const scopeText = data.role_name
        ? (data.role_mode === "without" ? `members without @${data.role_name}` : `members of @${data.role_name}`)
        : "all members";
      const idleText = data.days > 0
        ? `quiet for ${data.days}+ day${data.days === 1 ? "" : "s"}`
        : "sorted oldest activity first";
      statusEl.textContent =
        `${data.total} of ${data.total_scoped} ${scopeText} — ${idleText}`
        + `${data.channel_id ? ", measured in the selected channel" : ""}. `
        + `Activity is tracked for ${data.tracking_coverage} of ${data.total_scoped}.`;
      renderSortableTable(tableWrap, {
        columns: [
          // table.js escapes cell text — no esc() here or it double-escapes.
          { key: "display_name", label: "Member", format: (v, r) => r.display_name || r.user_id },
          { key: "days_since_last", label: "Days Idle", format: (v) => v == null ? "Never tracked" : v },
        ],
        data: data.members,
        defaultSort: "days_since_last",
        emptyMsg: "Nobody in this scope has been quiet that long. Widen the window or change the scope.",
        maxRows: 500,
      });
    } catch (err) {
      statusEl.textContent = "";
      tableWrap.innerHTML = renderError(`Couldn't load the member list — try again. (${err.message})`);
    }
  }
  scopeEl.addEventListener("change", refresh);
  roleEl.addEventListener("change", refresh);
  daysEl.addEventListener("change", refresh);
  channelEl.addEventListener("change", refresh);
  return { unmount() {} };
}
