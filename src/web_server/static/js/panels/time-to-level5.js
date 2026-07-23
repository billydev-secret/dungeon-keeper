import { api, esc, fmtTs } from "../api.js";
import { rangePicker, withLoading } from "../report-helpers.js";
import { makeBarChart } from "../charts.js";

// The endpoint hands back pre-formatted UTC strings ("2026-07-01 12:00").
// Re-stamp them as UTC so fmtTs can render them in the reader's own timezone,
// consistent with every other timestamp on the dashboard.
function utcToLocal(raw) {
  if (!raw) return "—";
  return fmtTs(`${String(raw).replace(" ", "T")}:00Z`);
}

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Time to Level 5</h2>
        <div class="subtitle">How long members take to reach level 5</div>
      </header>
      <div class="controls"></div>
      <div data-stats class="subtitle" style="margin-bottom:8px;"></div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-members style="margin-top:16px;"></div>
    </div>
  `;

  const rangeEl = rangePicker({ value: initialParams.days || "", allowAll: true, label: "Range" });
  container.querySelector(".controls").appendChild(rangeEl);
  const daysEl = rangeEl.querySelector("select");
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
      const data = await withLoading(container.querySelector(".chart-wrap"), api("/api/reports/time-to-level-5", params));
      if (chart) { chart.destroy(); chart = null; }

      statsEl.textContent = data.count
        ? `Mean: ${data.mean_days}d  \u00b7  Median: ${data.median_days}d  \u00b7  Std. deviation: ${data.stddev_days}d  \u00b7  Mode: ${data.mode_days}d  \u00b7  n=${data.count}  \u00b7  ${data.xp_required} XP required`
        : "";

      const wrap = container.querySelector(".chart-wrap");
      if (!data.histogram.length || data.count === 0) {
        wrap.innerHTML = `<div class="empty">Nobody reached level 5 in this window. Pick a longer range, or switch to All time.</div>`;
        membersEl.innerHTML = "";
        return;
      }
      wrap.innerHTML = "<canvas data-chart></canvas>";
      chart = makeBarChart(container.querySelector("[data-chart]"), {
        labels: data.histogram.map((b) => b.label),
        data: data.histogram.map((b) => b.count),
        title: `Time to Reach Level 5 — ${data.window_label}`,
        xLabel: "Days",
        yLabel: "Members",
      });

      if (data.members && data.members.length) {
        const rows = data.members
          .map(
            (m) =>
              `<tr><td>${esc(m.display_name)}</td><td>${esc(utcToLocal(m.first_at))}</td><td>${esc(utcToLocal(m.reached_at))}</td><td>${m.days}d</td></tr>`
          )
          .join("");
        membersEl.innerHTML = `
          <table class="data-table">
            <thead><tr><th>Member</th><th>First Active</th><th>Reached Level 5</th><th>Days Taken</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>`;
      } else {
        membersEl.innerHTML = "";
      }
    } catch (err) {
      statsEl.textContent = "";
      membersEl.innerHTML = "";
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">Couldn’t load time-to-level-5 — try again. (${esc(err.message)})</div>`;
    }
  }

  daysEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
