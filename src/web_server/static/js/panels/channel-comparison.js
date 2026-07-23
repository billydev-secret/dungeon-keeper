import { api, esc } from "../api.js";
import { rangePicker, withLoading } from "../report-helpers.js";
import { makeHorizontalBarChart } from "../charts.js";
import { renderSortableTable } from "../table.js";

const METRICS = [
  { value: "message_count",  label: "Messages" },
  { value: "total_xp",       label: "XP Earned" },
  { value: "gini",           label: "Gini Coefficient (Conversation Spread)" },
  { value: "avg_sentiment",  label: "Average Sentiment" },
  { value: "unique_authors", label: "Unique Authors" },
  { value: "trend_pct",      label: "Trend (Percent Change)" },
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
        <label>Metric
          <select data-control="metric">${metricOptions}</select>
        </label>
      </div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-table-wrap style="margin-top:12px; max-height:400px; overflow-y:auto;"></div>
    </div>
  `;

  const rangeEl = rangePicker({ value: initialParams.days || 1, allowAll: false, label: "Days" });
  container.querySelector(".controls").prepend(rangeEl);
  const daysEl   = rangeEl.querySelector("select");
  const metricEl = container.querySelector('[data-control="metric"]');
  const tableWrap = container.querySelector("[data-table-wrap]");
  let chart = null;

  async function refresh() {
    const days   = parseInt(daysEl.value) || 1;
    const metric = metricEl.value;
    const metricDef = METRICS.find((m) => m.value === metric) || METRICS[0];

    const qs = new URLSearchParams({ days, metric });
    history.replaceState(null, "", `#/channel-comparison?${qs}`);

    const wrap = container.querySelector(".chart-wrap");
    try {
      const data = await withLoading(wrap, api("/api/reports/channel-comparison", { days }));
      if (chart) { chart.destroy(); chart = null; }

      // Sort by selected metric (nulls last)
      const sorted = [...data.channels].sort((a, b) => {
        const av = a[metric] ?? -Infinity;
        const bv = b[metric] ?? -Infinity;
        return bv - av;
      });
      const channels = sorted.slice(0, 25);

      if (!channels.length) {
        wrap.innerHTML = `<div class="empty">No channel activity in this window. Pick a longer range, or check that Dungeon Keeper can read your busy channels.</div>`;
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
        emptyMsg: "No channel activity in this window.",
      });
    } catch (err) {
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">Couldn’t load the channel comparison — try again. (${esc(err.message)})</div>`;
      tableWrap.innerHTML = "";
    }
  }

  daysEl.addEventListener("change", refresh);
  metricEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
