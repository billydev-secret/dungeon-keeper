import { api } from "../api.js";
import { makeBarChart } from "../charts.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Greeter Response</h2>
        <div class="subtitle">How long new members wait for their first greeter message</div>
      </header>
      <div class="controls">
        <label>Days (empty = all time)
          <input type="number" data-control="days" min="1" max="3650" value="${initialParams.days || ""}" placeholder="all" />
        </label>
      </div>
      <div data-stats class="subtitle" style="margin-bottom:8px;"></div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-table-wrap style="margin-top:12px; max-height:350px; overflow-y:auto;"></div>
    </div>
  `;

  const daysEl = container.querySelector('[data-control="days"]');
  const statsEl = container.querySelector("[data-stats]");
  const tableWrap = container.querySelector("[data-table-wrap]");
  let chart = null;

  function fmtDur(s) {
    if (s < 60) return `${Math.round(s)}s`;
    if (s < 3600) return `${Math.round(s / 60)}m`;
    return `${(s / 3600).toFixed(1)}h`;
  }

  function fmtDate(ts) {
    return new Date(ts * 1000).toLocaleDateString(undefined, {
      month: "short", day: "numeric", year: "numeric",
    });
  }

  function renderTable(entries) {
    if (!entries || !entries.length) { tableWrap.innerHTML = ""; return; }
    renderSortableTable(tableWrap, {
      columns: [
        { key: "user_name", label: "Member", format: (v, r) => r.user_name || r.user_id },
        { key: "joined_at", label: "Joined", format: (v) => fmtDate(v) },
        { key: "response_seconds", label: "Response Time", format: (v) => fmtDur(v) },
        { key: "greeter_name", label: "Greeted By", format: (v, r) => r.greeter_name || r.greeter_id },
      ],
      data: entries,
      defaultSort: "joined_at",
    });
  }

  async function refresh() {
    const raw = parseInt(daysEl.value);
    const params = {};
    if (!isNaN(raw) && raw > 0) params.days = raw;
    const qs = new URLSearchParams();
    if (params.days) qs.set("days", params.days);
    history.replaceState(null, "", `#/greeter-response?${qs}`);

    try {
      const data = await api("/api/reports/greeter-response", params);
      if (chart) { chart.destroy(); chart = null; }

      statsEl.textContent = data.count
        ? `Median: ${fmtDur(data.median_seconds)}  ·  Mean: ${fmtDur(data.mean_seconds)}  ·  n=${data.count}`
        : "";

      const wrap = container.querySelector(".chart-wrap");
      if (!data.histogram.length || data.count === 0) {
        wrap.innerHTML = `<div class="empty">No greeter response data for the selected period.</div>`;
        tableWrap.innerHTML = "";
        return;
      }
      wrap.innerHTML = '<canvas data-chart></canvas>';
      chart = makeBarChart(container.querySelector("[data-chart]"), {
        labels: data.histogram.map((b) => b.label),
        data: data.histogram.map((b) => b.count),
        title: `Greeter Response Time — ${data.window_label}`,
        yLabel: "Joins",
      });

      renderTable(data.entries || []);
    } catch (err) {
      statsEl.textContent = "";
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${err.message}</div>`;
      tableWrap.innerHTML = "";
    }
  }

  daysEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
