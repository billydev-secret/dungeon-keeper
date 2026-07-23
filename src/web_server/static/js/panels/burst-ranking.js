import { api, esc } from "../api.js";
import { withLoading } from "../report-helpers.js";
import { makeHorizontalBarChart } from "../charts.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Burst Ranking</h2>
        <div class="subtitle">Who drives the most server activity when they start posting</div>
      </header>
      <div class="controls">
        <label>Period
          <select data-control="days">
            <option value="">All time</option>
            <option value="7">Last 7 days</option>
            <option value="14">Last 14 days</option>
            <option value="30">Last 30 days</option>
            <option value="60">Last 60 days</option>
            <option value="90">Last 90 days</option>
          </select>
        </label>
        <label>Minimum Sessions
          <input type="number" data-control="min_sessions" min="1" max="100" value="${initialParams.min_sessions || 3}" />
        </label>
      </div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-table-wrap style="margin-top:12px; max-height:400px; overflow-y:auto;"></div>
    </div>
  `;

  const daysEl = container.querySelector('[data-control="days"]');
  const minEl = container.querySelector('[data-control="min_sessions"]');
  const tableWrap = container.querySelector("[data-table-wrap]");
  let chart = null;

  daysEl.value = initialParams.days || "7";

  async function refresh() {
    const params = { min_sessions: parseInt(minEl.value) || 3 };
    if (daysEl.value) params.days = parseInt(daysEl.value);
    const qs = new URLSearchParams(params);
    history.replaceState(null, "", `#/burst-ranking?${qs}`);

    const wrap = container.querySelector(".chart-wrap");
    try {
      const data = await withLoading(wrap, api("/api/reports/burst-ranking", params));
      if (chart) { chart.destroy(); chart = null; }

      const top = data.entries.slice(0, 20);
      if (!top.length) {
        wrap.innerHTML = `<div class="empty">No burst data yet. A member needs at least the minimum number of posting sessions above before their burst effect can be measured — lower that number or widen the period.</div>`;
        tableWrap.innerHTML = "";
        return;
      }

      // Color bars: positive increase = green, negative = red
      const colors = top.map((e) => e.increase >= 0 ? "#7F8F3A" : "#9E3B2E");

      wrap.innerHTML = '<canvas data-chart></canvas>';
      chart = makeHorizontalBarChart(container.querySelector("[data-chart]"), {
        labels: top.map((e) => e.user_name || e.user_id),
        data: top.map((e) => e.increase),
        title: "Burst Increase — Average Messages After a Session Starts, Minus Before",
        xLabel: "Messages per 2 minutes",
        colors,
      });

      renderSortableTable(tableWrap, {
        columns: [
          { key: "user_name", label: "Member", format: (v, r) => r.user_name || r.user_id },
          { key: "pre_avg", label: "Before (Avg)" },
          { key: "post_avg", label: "After (Avg)" },
          { key: "increase", label: "Increase", format: (v) => `<span style="color:${v >= 0 ? '#7F8F3A' : '#9E3B2E'}">${v >= 0 ? '+' : ''}${v}</span>` },
          { key: "sessions", label: "Sessions" },
        ],
        data: data.entries,
        defaultSort: "increase",
        emptyMsg: "No members cleared the minimum-sessions bar for this period.",
        maxRows: 200,
      });
    } catch (err) {
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">Couldn’t load burst rankings — try again. (${esc(err.message)})</div>`;
      tableWrap.innerHTML = "";
    }
  }

  daysEl.addEventListener("change", refresh);
  minEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
