import { api, esc } from "../api.js";
import { makeBarChart } from "../charts.js";

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Time to Level 5</h2>
        <div class="subtitle">How long members take to reach level 5</div>
      </header>
      <div class="controls">
        <label>Days (empty = all time)
          <input type="number" data-control="days" min="1" max="3650" value="${initialParams.days || ""}" placeholder="all" />
        </label>
      </div>
      <div data-stats class="subtitle" style="margin-bottom:8px;"></div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-members style="margin-top:16px;"></div>
    </div>
  `;

  const daysEl = container.querySelector('[data-control="days"]');
  const statsEl = container.querySelector("[data-stats]");
  const membersEl = container.querySelector("[data-members]");
  let chart = null;

  async function refresh() {
    const raw = parseInt(daysEl.value);
    const params = {};
    if (!isNaN(raw) && raw > 0) params.days = raw;
    const qs = new URLSearchParams();
    if (params.days) qs.set("days", params.days);
    history.replaceState(null, "", `#/time-to-level5?${qs}`);

    try {
      const data = await api("/api/reports/time-to-level-5", params);
      if (chart) { chart.destroy(); chart = null; }

      statsEl.textContent = data.count
        ? `Mean: ${data.mean_days}d  \u00b7  Median: ${data.median_days}d  \u00b7  Std Dev: ${data.stddev_days}d  \u00b7  Mode: ${data.mode_days}d  \u00b7  n=${data.count}  \u00b7  ${data.xp_required} XP required`
        : "";

      const wrap = container.querySelector(".chart-wrap");
      if (!data.histogram.length || data.count === 0) {
        wrap.innerHTML = `<div class="empty">No time-to-level-5 data for the selected period.</div>`;
        membersEl.innerHTML = "";
        return;
      }
      wrap.innerHTML = "<canvas data-chart></canvas>";
      chart = makeBarChart(container.querySelector("[data-chart]"), {
        labels: data.histogram.map((b) => b.label),
        data: data.histogram.map((b) => b.count),
        title: `Time to Reach Level 5 \u2014 ${data.window_label}`,
        xLabel: "Days",
        yLabel: "Members",
      });

      if (data.members && data.members.length) {
        const rows = data.members
          .map(
            (m) =>
              `<tr><td>${m.display_name}</td><td>${m.first_at}</td><td>${m.reached_at}</td><td>${m.days}d</td></tr>`
          )
          .join("");
        membersEl.innerHTML = `
          <table class="data-table">
            <thead><tr><th>Member</th><th>First Active</th><th>Reached Lv5</th><th>Duration</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>`;
      } else {
        membersEl.innerHTML = "";
      }
    } catch (err) {
      statsEl.textContent = "";
      membersEl.innerHTML = "";
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${esc(err.message)}</div>`;
    }
  }

  daysEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
