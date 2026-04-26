import { api } from "../api.js";
import { makeBarChart } from "../charts.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  const html = `
    <div class="panel">
      <header>
        <h2>XP Level Review</h2>
        <div class="subtitle">Time-to-reach histogram for any level</div>
      </header>
      <div class="controls">
        <label>Level
          <input type="number" data-control="level" min="2" max="100" value="${initialParams.level || 5}" />
        </label>
        <label>Window
          <select data-control="days">
            <option value="">All time</option>
            <option value="30">30 days</option>
            <option value="90">90 days</option>
            <option value="180">180 days</option>
            <option value="365">1 year</option>
          </select>
        </label>
      </div>
      <div data-status></div>
      <div data-chart-wrap></div>
      <div data-table-wrap style="margin-top:12px; max-height:400px; overflow-y:auto;"></div>
    </div>
  `;
  container.innerHTML = html;

  const levelEl = container.querySelector('[data-control="level"]');
  const daysEl = container.querySelector('[data-control="days"]');
  const statusEl = container.querySelector('[data-status]');
  const chartWrap = container.querySelector('[data-chart-wrap]');
  const tableWrap = container.querySelector('[data-table-wrap]');
  if (initialParams.days) daysEl.value = initialParams.days;
  let chart = null;

  async function refresh() {
    const level = parseInt(levelEl.value) || 5;
    const params = { level };
    if (daysEl.value) params.days = parseInt(daysEl.value);
    history.replaceState(null, "", `#/xp-level-review?level=${level}${daysEl.value ? `&days=${daysEl.value}` : ""}`);
    statusEl.textContent = "Loading…";
    try {
      const data = await api("/api/reports/xp-level-review", params);
      if (!data.count) {
        statusEl.textContent = `No members have reached level ${level} (${Math.round(data.xp_required)} XP required).`;
        chartWrap.textContent = ""; tableWrap.textContent = "";
        return;
      }
      statusEl.textContent = `Level ${level} • ${data.window_label} • ${data.count} members • mean ${data.mean_days}d, median ${data.median_days}d, mode ${data.mode_days}d`;
      if (chart) { chart.destroy(); chart = null; }
      chartWrap.innerHTML = '<canvas data-chart></canvas>';
      chart = makeBarChart(chartWrap.querySelector("[data-chart]"), {
        labels: data.histogram.map((b) => b.label),
        data: data.histogram.map((b) => b.count),
        title: `Days to reach level ${level}`,
        yLabel: "Members",
      });
      renderSortableTable(tableWrap, {
        columns: [
          { key: "display_name", label: "Member", format: (v, r) => r.display_name || r.user_id },
          { key: "days", label: "Days" },
        ],
        data: data.members,
        defaultSort: "days",
        defaultAsc: true,
      });
    } catch (err) {
      statusEl.textContent = `Error: ${err.message}`;
    }
  }
  levelEl.addEventListener("change", refresh);
  daysEl.addEventListener("change", refresh);
  refresh();
  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
