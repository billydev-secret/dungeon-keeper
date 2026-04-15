import { api, esc } from "../api.js";
import { makeHorizontalBarChart } from "../charts.js";
import { renderSortableTable } from "../table.js";

const METRICS = [
  { value: "message_count",  label: "Messages" },
  { value: "total_xp",       label: "XP Earned" },
  { value: "gini",           label: "Gini Coefficient" },
  { value: "avg_sentiment",  label: "Avg Sentiment" },
  { value: "unique_authors", label: "Unique Authors" },
  { value: "trend_pct",      label: "Trend %" },
];

export function mount(container, initialParams) {
  const defaultMetric = initialParams.metric || "message_count";

  const metricOptions = METRICS.map(
    (m) => `<option value="${m.value}"${m.value === defaultMetric ? " selected" : ""}>${m.label}</option>`
  ).join("");

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
        <label>Metric
          <select data-control="metric">${metricOptions}</select>
        </label>
      </div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-table-wrap style="margin-top:12px; max-height:400px; overflow-y:auto;"></div>
    </div>
  `;

  const daysEl   = container.querySelector('[data-control="days"]');
  const metricEl = container.querySelector('[data-control="metric"]');
  const tableWrap = container.querySelector("[data-table-wrap]");
  let chart = null;

  async function refresh() {
    const days   = parseInt(daysEl.value) || 30;
    const metric = metricEl.value;
    const metricDef = METRICS.find((m) => m.value === metric) || METRICS[0];

    const qs = new URLSearchParams({ days, metric });
    history.replaceState(null, "", `#/channel-comparison?${qs}`);

    try {
      const data = await api("/api/reports/channel-comparison", { days });
      if (chart) { chart.destroy(); chart = null; }

      const wrap = container.querySelector(".chart-wrap");

      // Sort by selected metric (nulls last)
      const sorted = [...data.channels].sort((a, b) => {
        const av = a[metric] ?? -Infinity;
        const bv = b[metric] ?? -Infinity;
        return bv - av;
      });
      const channels = sorted.slice(0, 25);

      if (!channels.length) {
        wrap.innerHTML = `<div class="empty">No channel data for the selected period.</div>`;
        tableWrap.innerHTML = "";
        return;
      }

      wrap.innerHTML = '<canvas data-chart></canvas>';
      chart = makeHorizontalBarChart(container.querySelector("[data-chart]"), {
        labels: channels.map((c) => c.channel_name || c.channel_id),
        data:   channels.map((c) => c[metric] ?? 0),
        title:  `${metricDef.label} by Channel (last ${days} days)`,
        xLabel: metricDef.label,
      });

      renderSortableTable(tableWrap, {
        columns: [
          { key: "channel_name",  label: "Channel",   format: (v, r) => r.channel_name || r.channel_id },
          { key: "message_count", label: "Messages",  format: (v) => v.toLocaleString() },
          { key: "unique_authors",label: "Authors" },
          { key: "total_xp",      label: "XP",        format: (v) => Math.round(v).toLocaleString() },
          { key: "gini",          label: "Gini",       format: (v) => v.toFixed(3) },
          { key: "avg_sentiment", label: "Sentiment", format: (v) => {
            if (v == null) return "—";
            const color = v > 0.05 ? "#7F8F3A" : v < -0.05 ? "#9E3B2E" : "#dbdee1";
            return `<span style="color:${color}">${v.toFixed(3)}</span>`;
          }},
          { key: "trend_pct",     label: "Trend",     format: (v) => {
            const color = v > 0 ? "#7F8F3A" : v < 0 ? "#9E3B2E" : "#dbdee1";
            return `<span style="color:${color}">${v > 0 ? "+" : ""}${v}%</span>`;
          }},
        ],
        data: data.channels,
        defaultSort: metric,
      });
    } catch (err) {
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${esc(err.message)}</div>`;
      tableWrap.innerHTML = "";
    }
  }

  daysEl.addEventListener("change", refresh);
  metricEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
