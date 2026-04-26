import { api } from "../api.js";
import { makeBarChart } from "../charts.js";

export function mount(container, initialParams) {
  const html = `
    <div class="panel">
      <header>
        <h2>Session Burst (per-member)</h2>
        <div class="subtitle">Server message rate before and after this member starts a session (gap ≥20m)</div>
      </header>
      <div class="controls">
        <label>Member ID
          <input type="text" data-control="user_id" placeholder="Discord user id" />
        </label>
        <button data-action="run">Run</button>
      </div>
      <div data-status></div>
      <div data-chart-wrap></div>
    </div>
  `;
  container.innerHTML = html;

  const userEl = container.querySelector('[data-control="user_id"]');
  const runBtn = container.querySelector('[data-action="run"]');
  const statusEl = container.querySelector('[data-status]');
  const chartWrap = container.querySelector('[data-chart-wrap]');
  let chart = null;

  if (initialParams.user_id) userEl.value = initialParams.user_id;

  async function refresh() {
    const uid = userEl.value.trim();
    if (!uid) { statusEl.textContent = "Enter a user ID."; return; }
    history.replaceState(null, "", `#/session-burst?user_id=${encodeURIComponent(uid)}`);
    statusEl.textContent = "Loading…";
    try {
      const data = await api("/api/reports/session-burst", { user_id: uid });
      if (!data.sessions) {
        statusEl.textContent = `No session data for ${data.user_name || uid} (need ≥2 messages).`;
        chartWrap.textContent = "";
        return;
      }
      statusEl.textContent = `${data.user_name || uid} — ${data.sessions} sessions, pre avg ${data.pre_avg.toFixed(2)}, post avg ${data.post_avg.toFixed(2)}, server baseline ${data.overall_rate.toFixed(2)}/${data.bin_minutes}m`;
      const labels = [];
      const allBins = [...data.pre_bins, ...data.post_bins];
      const colors = [];
      for (let i = 0; i < data.pre_bins.length; i++) {
        labels.push(`-${(data.pre_bins.length - i) * data.bin_minutes}m`);
        colors.push("#9E3B2E");
      }
      for (let i = 0; i < data.post_bins.length; i++) {
        labels.push(`+${(i + 1) * data.bin_minutes}m`);
        colors.push("#7F8F3A");
      }
      if (chart) { chart.destroy(); chart = null; }
      chartWrap.innerHTML = '<canvas data-chart></canvas>';
      chart = makeBarChart(chartWrap.querySelector("[data-chart]"), {
        labels,
        data: allBins,
        title: "Server messages per bin around session start",
        yLabel: "Messages",
        color: colors,
      });
    } catch (err) {
      statusEl.textContent = `Error: ${err.message}`;
    }
  }
  runBtn.addEventListener("click", refresh);
  userEl.addEventListener("change", refresh);
  if (userEl.value) refresh();
  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
