import { api, esc } from "../api.js";
import { makeBarChart, makeHorizontalBarChart } from "../charts.js";
import { renderSortableTable } from "../table.js";

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Voice Activity</h2>
        <div class="subtitle">Voice channel usage — top users and peak hours</div>
      </header>
      <div class="controls">
        <label>Days (empty = all time)
          <input type="number" data-control="days" min="1" max="3650" value="${initialParams.days || 7}" placeholder="all" />
        </label>
      </div>
      <div data-stats class="subtitle" style="margin-bottom:8px;"></div>
      <div class="chart-wrap"><canvas data-chart-hour></canvas></div>
      <div class="chart-wrap" style="margin-top:12px;"><canvas data-chart-users></canvas></div>
      <div data-table-wrap style="margin-top:12px; max-height:350px; overflow-y:auto;"></div>
    </div>
  `;

  const daysEl = container.querySelector('[data-control="days"]');
  const statsEl = container.querySelector("[data-stats]");
  const tableWrap = container.querySelector("[data-table-wrap]");
  let chartHour = null;
  let chartUsers = null;

  function fmtMin(m) {
    if (m < 60) return `${Math.round(m)}m`;
    return `${(m / 60).toFixed(1)}h`;
  }

  async function refresh() {
    const params = {};
    const d = parseInt(daysEl.value);
    if (!isNaN(d) && d > 0) params.days = d;

    const qs = new URLSearchParams();
    if (params.days) qs.set("days", params.days);
    history.replaceState(null, "", `#/voice-activity?${qs}`);

    try {
      const data = await api("/api/reports/voice-activity", params);
      if (chartHour) { chartHour.destroy(); chartHour = null; }
      if (chartUsers) { chartUsers.destroy(); chartUsers = null; }

      statsEl.textContent = data.total_sessions
        ? `Sessions: ${data.total_sessions}  ·  Total: ${fmtMin(data.total_minutes)}  ·  Avg: ${fmtMin(data.avg_session_minutes)}`
        : "No voice data.";

      // Hour chart
      const hourWrap = container.querySelector("[data-chart-hour]").parentElement;
      if (data.by_hour.length) {
        hourWrap.innerHTML = '<canvas data-chart-hour></canvas>';
        chartHour = makeBarChart(container.querySelector("[data-chart-hour]"), {
          labels: data.by_hour.map((h) => h.label),
          data: data.by_hour.map((h) => h.total_minutes),
          title: "Voice Minutes by Hour of Day",
          yLabel: "Minutes",
          color: "#7F8F3A",
        });
      }

      // Users chart
      const userWrap = container.querySelector("[data-chart-users]").parentElement;
      const users = data.top_users.slice(0, 15);
      if (users.length) {
        userWrap.innerHTML = '<canvas data-chart-users></canvas>';
        chartUsers = makeHorizontalBarChart(container.querySelector("[data-chart-users]"), {
          labels: users.map((u) => u.user_name || u.user_id),
          data: users.map((u) => u.total_minutes),
          title: "Top Voice Users",
          xLabel: "Minutes",
          color: "#B36A92",
        });
      }

      if (data.top_users.length) {
        renderSortableTable(tableWrap, {
          columns: [
            { key: "user_name", label: "User", format: (v, r) => r.user_name || r.user_id },
            { key: "total_minutes", label: "Total Time", format: (v) => fmtMin(v) },
            { key: "session_count", label: "Sessions" },
            { key: "avg_minutes", label: "Avg Session", format: (v) => fmtMin(v) },
          ],
          data: data.top_users,
          defaultSort: "total_minutes",
        });
      } else { tableWrap.innerHTML = ""; }
    } catch (err) {
      statsEl.textContent = "";
      container.querySelector("[data-chart-hour]").parentElement.innerHTML = `<div class="error">${esc(err.message)}</div>`;
      tableWrap.innerHTML = "";
    }
  }

  daysEl.addEventListener("change", refresh);
  refresh();

  return {
    unmount() {
      if (chartHour) { chartHour.destroy(); chartHour = null; }
      if (chartUsers) { chartUsers.destroy(); chartUsers = null; }
    },
  };
}
