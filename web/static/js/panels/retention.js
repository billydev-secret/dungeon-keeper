import { api } from "../api.js";
import { makeBarChart } from "../charts.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Member Retention</h2>
        <div class="subtitle">Members whose activity dropped between two consecutive periods</div>
      </header>
      <div class="controls">
        <label>Period (days)
          <input type="number" data-control="period_days" min="1" max="365" value="${initialParams.period_days || 3}" />
        </label>
        <label>Min previous msgs
          <input type="number" data-control="min_previous" min="1" max="100" value="${initialParams.min_previous || 5}" />
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

  function fmtDate(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleDateString(undefined, {
      month: "short", day: "numeric", year: "numeric",
    });
  }

  async function refresh() {
    const params = {
      period_days: parseInt(periodEl.value) || 30,
      min_previous: parseInt(minEl.value) || 5,
    };
    const qs = new URLSearchParams(params);
    history.replaceState(null, "", `#/retention?${qs}`);

    try {
      const data = await api("/api/reports/retention", params);
      if (chart) { chart.destroy(); chart = null; }

      statsEl.textContent = `${data.total_dropoffs} members with activity drops over ${data.period_days}-day window`;

      data.entries.sort((a, b) => b.drop_pct - a.drop_pct);

      const wrap = container.querySelector(".chart-wrap");
      const entries = data.entries.slice(0, 20);
      if (!entries.length) {
        wrap.innerHTML = `<div class="empty">No dropoffs detected for this period.</div>`;
        tableWrap.innerHTML = "";
        return;
      }
      wrap.innerHTML = '<canvas data-chart></canvas>';
      chart = makeBarChart(container.querySelector("[data-chart]"), {
        labels: entries.map((e) => e.user_name || e.user_id),
        data: entries.map((e) => e.drop_pct),
        title: "Activity Drop %",
        yLabel: "Drop %",
        color: "#9E3B2E",
      });

      renderSortableTable(tableWrap, {
        columns: [
          { key: "user_name", label: "Member", format: (v, r) => r.user_name || r.user_id },
          { key: "msgs_prev", label: "Previous" },
          { key: "msgs_recent", label: "Recent" },
          { key: "drop_pct", label: "Drop", format: (v) => `<span style="color:#9E3B2E">${v}%</span>` },
          { key: "days_active_prev", label: "Days (prev)" },
          { key: "days_active_recent", label: "Days (recent)" },
          { key: "last_seen_ts", label: "Last Seen", format: (v) => fmtDate(v) },
          { key: "level", label: "Level" },
        ],
        data: data.entries,
        defaultSort: "drop_pct",
      });
    } catch (err) {
      statsEl.textContent = "";
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${err.message}</div>`;
      tableWrap.innerHTML = "";
    }
  }

  periodEl.addEventListener("change", refresh);
  minEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
