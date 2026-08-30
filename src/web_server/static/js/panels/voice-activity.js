import { api, esc } from "../api.js";
import { rangePicker, withLoading } from "../report-helpers.js";
import { makeBarChart, makeHorizontalBarChart, renderChartTable, CHART_ACCENT } from "../charts.js";
import { renderSortableTable } from "../table.js";

/**
 * Voice Activity — the voice usage report (Reports → Engagement).
 * Moderator-level information — no gating applies here. The settings that
 * shape these rooms live on Config → Voice → Voice Control
 * (config-voice-master), cross-linked via `related:`.
 */
export function mount(container, initialParams = {}) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Voice Activity</h2>
        <div class="subtitle">Voice channel usage — top users and peak hours</div>
      </header>
      <div class="controls"></div>
      <div data-stats class="subtitle" style="margin-bottom:8px;"></div>
      <div class="chart-caption" data-caption-hour></div>
      <div class="chart-wrap"><canvas data-chart-hour></canvas></div>
      <div data-chart-table-hour></div>
      <div class="chart-caption" data-caption-users style="margin-top:12px;"></div>
      <div class="chart-wrap" style="margin-top:12px;"><canvas data-chart-users></canvas></div>
      <div data-chart-table-users></div>
      <div data-table-wrap style="margin-top:12px; max-height:350px; overflow-y:auto;"></div>
    </div>
  `;

  const rangeEl = rangePicker({ value: initialParams.days || 7, allowAll: true, label: "Range" });
  container.querySelector(".controls").appendChild(rangeEl);
  const daysEl = rangeEl.querySelector("select");
  const statsEl = container.querySelector("[data-stats]");
  // Both charts below carry exactly one series (one bar per hour, one bar per
  // member) — "none for one": the caption already names the chart, so neither
  // gets a legend, but a tooltip must never be the only way to read a value,
  // so both still get a table.
  const captionHourEl = container.querySelector("[data-caption-hour]");
  const tableHourEl = container.querySelector("[data-chart-table-hour]");
  const captionUsersEl = container.querySelector("[data-caption-users]");
  const tableUsersEl = container.querySelector("[data-chart-table-users]");
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
      const data = await withLoading(container.querySelector(".chart-wrap"), api("/api/reports/voice-activity", params));
      if (chartHour) { chartHour.destroy(); chartHour = null; }
      if (chartUsers) { chartUsers.destroy(); chartUsers = null; }

      statsEl.textContent = data.total_sessions
        ? `Sessions: ${data.total_sessions}  ·  Total: ${fmtMin(data.total_minutes)}  ·  Avg: ${fmtMin(data.avg_session_minutes)}`
        : "No voice sessions in this window. Voice time is tracked from the moment Dungeon Keeper joined — widen the range, or wait for members to hop into a voice channel.";

      // Hour chart
      const hourWrap = container.querySelector("[data-chart-hour]").parentElement;
      const hourTitle = "Voice Minutes by Hour of Day";
      if (data.by_hour.length) {
        hourWrap.innerHTML = '<canvas data-chart-hour></canvas>';
        chartHour = makeBarChart(container.querySelector("[data-chart-hour]"), {
          labels: data.by_hour.map((h) => h.label),
          data: data.by_hour.map((h) => h.total_minutes),
          title: hourTitle,
          yLabel: "Minutes",
          color: "#7F8F3A",
        });
        captionHourEl.textContent = hourTitle;
        renderChartTable(tableHourEl, {
          labels: data.by_hour.map((h) => h.label),
          datasets: [{ label: "Minutes", data: data.by_hour.map((h) => h.total_minutes) }],
          indexLabel: "Hour",
        });
      } else {
        captionHourEl.textContent = "";
        tableHourEl.replaceChildren();
      }

      // Users chart
      const userWrap = container.querySelector("[data-chart-users]").parentElement;
      const users = data.top_users.slice(0, 15);
      const usersTitle = "Top Voice Users";
      if (users.length) {
        userWrap.innerHTML = '<canvas data-chart-users></canvas>';
        chartUsers = makeHorizontalBarChart(container.querySelector("[data-chart-users]"), {
          labels: users.map((u) => u.user_name || u.user_id),
          data: users.map((u) => u.total_minutes),
          title: usersTitle,
          xLabel: "Minutes",
          color: CHART_ACCENT,
        });
        captionUsersEl.textContent = usersTitle;
        renderChartTable(tableUsersEl, {
          labels: users.map((u) => u.user_name || u.user_id),
          datasets: [{ label: "Minutes", data: users.map((u) => u.total_minutes) }],
          indexLabel: "Member",
        });
      } else {
        captionUsersEl.textContent = "";
        tableUsersEl.replaceChildren();
      }

      if (data.top_users.length) {
        renderSortableTable(tableWrap, {
          columns: [
            { key: "user_name", label: "Member", format: (v, r) => r.user_name || r.user_id },
            { key: "total_minutes", label: "Total Time", format: (v) => fmtMin(v) },
            { key: "session_count", label: "Sessions" },
            { key: "avg_minutes", label: "Avg Session", format: (v) => fmtMin(v) },
          ],
          data: data.top_users,
          defaultSort: "total_minutes",
          emptyMsg: "No voice sessions in this window.",
          maxRows: 200,
        });
      } else {
        tableWrap.innerHTML = `<div class="empty">No voice sessions in this window.</div>`;
      }
    } catch (err) {
      statsEl.textContent = "";
      // A failed fetch throws before the destroy calls above run, so both
      // chart instances from the last successful load are still alive. Only
      // the Hour chart used to be replaced with an error — Users kept showing
      // a stale, now-untitled chart. Destroy and replace both the same way.
      if (chartHour) { chartHour.destroy(); chartHour = null; }
      if (chartUsers) { chartUsers.destroy(); chartUsers = null; }
      const errMsg = `<div class="error">Couldn’t load voice activity — try again. (${esc(err.message)})</div>`;
      container.querySelector("[data-chart-hour]").parentElement.innerHTML = errMsg;
      container.querySelector("[data-chart-users]").parentElement.innerHTML = errMsg;
      captionHourEl.textContent = "";
      tableHourEl.replaceChildren();
      captionUsersEl.textContent = "";
      tableUsersEl.replaceChildren();
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
