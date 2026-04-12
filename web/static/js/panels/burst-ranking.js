import { api } from "../api.js";
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
        <label>Min sessions
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

    try {
      const data = await api("/api/reports/burst-ranking", params);
      if (chart) { chart.destroy(); chart = null; }

      const wrap = container.querySelector(".chart-wrap");
      const top = data.entries.slice(0, 20);
      if (!top.length) {
        wrap.innerHTML = `<div class="empty">No burst data (need users with enough sessions).</div>`;
        tableWrap.innerHTML = "";
        return;
      }

      // Color bars: positive increase = green, negative = red
      const colors = top.map((e) => e.increase >= 0 ? "#7F8F3A" : "#9E3B2E");

      wrap.innerHTML = '<canvas data-chart></canvas>';
      chart = makeHorizontalBarChart(container.querySelector("[data-chart]"), {
        labels: top.map((e) => e.user_name || e.user_id),
        data: top.map((e) => e.increase),
        title: "Burst Increase (post − pre session avg msg rate)",
        xLabel: "Msgs/2min increase",
        colors,
      });

      renderSortableTable(tableWrap, {
        columns: [
          { key: "user_name", label: "Member", format: (v, r) => r.user_name || r.user_id },
          { key: "pre_avg", label: "Pre Avg" },
          { key: "post_avg", label: "Post Avg" },
          { key: "increase", label: "Increase", format: (v) => `<span style="color:${v >= 0 ? '#7F8F3A' : '#9E3B2E'}">${v >= 0 ? '+' : ''}${v}</span>` },
          { key: "sessions", label: "Sessions" },
        ],
        data: data.entries,
        defaultSort: "increase",
      });
    } catch (err) {
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${err.message}</div>`;
      tableWrap.innerHTML = "";
    }
  }

  daysEl.addEventListener("change", refresh);
  minEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
