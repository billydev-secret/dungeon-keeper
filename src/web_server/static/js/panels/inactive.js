import { api, esc } from "../api.js";
import { withLoading } from "../report-helpers.js";
import { loadChannels, channelSelect } from "../config-helpers.js";
import { renderSortableTable } from "../table.js";
import { renderLoading, renderError } from "../states.js";

const PRESETS = [
  { label: "1 day", value: 86400 },
  { label: "3 days", value: 3 * 86400 },
  { label: "7 days", value: 7 * 86400 },
  { label: "14 days", value: 14 * 86400 },
  { label: "30 days", value: 30 * 86400 },
  { label: "60 days", value: 60 * 86400 },
  { label: "90 days", value: 90 * 86400 },
];

export function mount(container, initialParams) {
  const opts = PRESETS.map((p) => `<option value="${p.value}">${p.label}</option>`).join("");
  const html = `
    <div class="panel">
      <header>
        <h2>Inactive Members</h2>
        <div class="subtitle">Everyone in the server who hasn't posted inside the chosen window</div>
      </header>
      <div class="controls">
        <label>Quiet For
          <select data-control="period">${opts}</select>
        </label>
        <label>Channel (Optional)
          <select data-control="channel"><option value="0">All channels</option></select>
        </label>
      </div>
      <div data-status></div>
      <div data-table-wrap style="margin-top:12px; max-height:500px; overflow-y:auto;">
        ${renderLoading("Loading inactive members…")}
      </div>
    </div>
  `;
  container.innerHTML = html;

  const periodEl = container.querySelector('[data-control="period"]');
  const channelEl = container.querySelector('[data-control="channel"]');
  const statusEl = container.querySelector('[data-status]');
  const tableWrap = container.querySelector('[data-table-wrap]');

  periodEl.value = initialParams.period_seconds || (7 * 86400);

  (async () => {
    const channels = await loadChannels();
    channelEl.innerHTML = channelSelect(channels, initialParams.channel_id || "0");
    refresh();
  })();

  async function refresh() {
    const params = { period_seconds: parseInt(periodEl.value) || (7 * 86400) };
    if (channelEl.value && channelEl.value !== "0") params.channel_id = channelEl.value;
    statusEl.className = "";
    statusEl.textContent = "Loading inactive members…";
    try {
      const data = await withLoading(tableWrap, api("/api/reports/inactive", params));
      statusEl.textContent =
        `${data.total} member${data.total === 1 ? "" : "s"} inactive over the last ${data.period_label}`
        + `${data.channel_id ? " in the selected channel" : ""}.`;
      renderSortableTable(tableWrap, {
        columns: [
          { key: "display_name", label: "Member", format: (v, r) => esc(r.display_name || r.user_id) },
          { key: "days_since_last", label: "Days Idle", format: (v) => v == null ? "Never tracked" : v },
        ],
        data: data.members,
        defaultSort: "days_since_last",
        emptyMsg: "Nobody has been quiet that long — everyone posted inside this window. "
          + "Pick a longer period to widen the net.",
        maxRows: 500,
      });
    } catch (err) {
      statusEl.textContent = "";
      tableWrap.innerHTML = renderError(`Couldn't load the inactive member list — try again. (${err.message})`);
    }
  }
  periodEl.addEventListener("change", refresh);
  channelEl.addEventListener("change", refresh);
  return { unmount() {} };
}
