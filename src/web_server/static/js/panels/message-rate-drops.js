import { api, esc } from "../api.js";
import { makeBarChart } from "../charts.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Message Rate Drops</h2>
        <div class="subtitle">Members whose message rate dropped recently</div>
      </header>
      <div class="controls">
        <label>Period (days)
          <input type="number" data-control="period_days" min="1" max="365" value="${initialParams.period_days || 2}" />
        </label>
        <label>Min previous msgs
          <input type="number" data-control="min_previous" min="1" max="100" value="${initialParams.min_previous || 100}" />
        </label>
      </div>
      <div data-stats class="subtitle" style="margin-bottom:8px;"></div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-table-wrap style="margin-top:12px; max-height:400px; overflow-y:auto;"></div>
    </div>
  `;

  const periodEl = container.querySelector('[data-control="period_days"]');
  const minEl = container.querySelector('[data-control="min_previous"]');
  const statsEl = container.querySelector("[data-stats]");
  const tableWrap = container.querySelector("[data-table-wrap]");
  let chart = null;

  async function refresh() {
    const params = {
      period_days: parseInt(periodEl.value) || 14,
      min_previous: parseInt(minEl.value) || 5,
    };
    const qs = new URLSearchParams(params);
    history.replaceState(null, "", `#/message-rate-drops?${qs}`);

    try {
      const data = await api("/api/reports/message-rate-drops", params);
      if (chart) { chart.destroy(); chart = null; }

      const srvSign = data.server_drop_pct > 0 ? "" : "+";
      statsEl.textContent = data.entries.length
        ? `Server baseline: ${data.server_prev.toLocaleString()} → ${data.server_recent.toLocaleString()} (${srvSign}${-data.server_drop_pct}%)  ·  Adjusted = raw drop minus server trend`
        : "";

      data.entries.sort((a, b) => b.adjusted_drop_pct - a.adjusted_drop_pct);

      const wrap = container.querySelector(".chart-wrap");
      const entries = data.entries.slice(0, 20);
      if (!entries.length) {
        wrap.innerHTML = `<div class="empty">No significant drops detected in the ${data.period_days}-day window.</div>`;
        tableWrap.innerHTML = "";
        return;
      }
      wrap.innerHTML = '<canvas data-chart></canvas>';
      chart = makeBarChart(container.querySelector("[data-chart]"), {
        labels: entries.map((e) => e.user_name || e.user_id),
        data: entries.map((e) => e.adjusted_drop_pct),
        title: `Adjusted Drop % (${data.period_days}-day windows, server trend removed)`,
        yLabel: "Adjusted Drop %",
        color: "#9E3B2E",
      });

      renderSortableTable(tableWrap, {
        columns: [
          { key: "user_name", label: "Member", format: (v, r) => r.user_name || r.user_id },
          { key: "prev_count", label: "Previous" },
          { key: "recent_count", label: "Recent" },
          { key: "drop_pct", label: "Raw Drop", format: (v) => `<span style="color:#9E3B2E">${v}%</span>` },
          { key: "adjusted_drop_pct", label: "Adjusted Drop", format: (v) => {
            const color = v > 0 ? "#9E3B2E" : "#7F8F3A";
            return `<span style="color:${color}">${v}%</span>`;
          }},
        ],
        data: data.entries,
        defaultSort: "adjusted_drop_pct",
      });
    } catch (err) {
      statsEl.textContent = "";
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${esc(err.message)}</div>`;
      tableWrap.innerHTML = "";
    }
  }

  periodEl.addEventListener("change", refresh);
  minEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
