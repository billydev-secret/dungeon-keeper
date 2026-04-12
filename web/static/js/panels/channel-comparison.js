import { api } from "../api.js";
import { makeHorizontalBarChart } from "../charts.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Channel Comparison</h2>
        <div class="subtitle">Channel activity rankings and trends</div>
      </header>
      <div class="controls">
        <label>Days
          <input type="number" data-control="days" min="1" max="365" value="${initialParams.days || 1}" />
        </label>
      </div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-table-wrap style="margin-top:12px; max-height:400px; overflow-y:auto;"></div>
    </div>
  `;

  const daysEl = container.querySelector('[data-control="days"]');
  const tableWrap = container.querySelector("[data-table-wrap]");
  let chart = null;

  async function refresh() {
    const params = { days: parseInt(daysEl.value) || 30 };
    const qs = new URLSearchParams(params);
    history.replaceState(null, "", `#/channel-comparison?${qs}`);

    try {
      const data = await api("/api/reports/channel-comparison", params);
      if (chart) { chart.destroy(); chart = null; }

      const wrap = container.querySelector(".chart-wrap");
      const channels = data.channels.slice(0, 25);
      if (!channels.length) {
        wrap.innerHTML = `<div class="empty">No channel data for the selected period.</div>`;
        tableWrap.innerHTML = "";
        return;
      }

      wrap.innerHTML = '<canvas data-chart></canvas>';
      chart = makeHorizontalBarChart(container.querySelector("[data-chart]"), {
        labels: channels.map((c) => c.channel_name || c.channel_id),
        data: channels.map((c) => c.message_count),
        title: `Channel Activity (last ${params.days} days)`,
        xLabel: "Messages",
      });

      renderSortableTable(tableWrap, {
        columns: [
          { key: "channel_name", label: "Channel", format: (v, r) => r.channel_name || r.channel_id },
          { key: "message_count", label: "Messages", format: (v) => v.toLocaleString() },
          { key: "unique_authors", label: "Authors" },
          { key: "prev_count", label: "1st Half", format: (v) => v.toLocaleString() },
          { key: "recent_count", label: "2nd Half", format: (v) => v.toLocaleString() },
          { key: "trend_pct", label: "Trend", format: (v) => {
            const color = v > 0 ? "#7F8F3A" : v < 0 ? "#9E3B2E" : "#dbdee1";
            return `<span style="color:${color}">${v > 0 ? '+' : ''}${v}%</span>`;
          }},
        ],
        data: data.channels,
        defaultSort: "message_count",
      });
    } catch (err) {
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${err.message}</div>`;
      tableWrap.innerHTML = "";
    }
  }

  daysEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
