import { api } from "../api.js";
import { makeBarChart } from "../charts.js";

const RESOLUTIONS = [
  { value: "hour_of_day", label: "By Hour of Day" },
  { value: "day_of_week", label: "By Day of Week" },
];

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Join Times</h2>
        <div class="subtitle">When members joined the server</div>
      </header>
      <div class="controls">
        <label>Group by
          <select data-control="resolution">
            ${RESOLUTIONS.map((r) => `<option value="${r.value}">${r.label}</option>`).join("")}
          </select>
        </label>
      </div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
    </div>
  `;

  const resEl = container.querySelector('[data-control="resolution"]');
  resEl.value = initialParams.resolution || "hour_of_day";
  let chart = null;

  async function refresh() {
    history.replaceState(null, "", `#/join-times?resolution=${resEl.value}`);
    try {
      const data = await api("/api/reports/join-times", { resolution: resEl.value });
      if (chart) { chart.destroy(); chart = null; }
      const wrap = container.querySelector(".chart-wrap");
      wrap.innerHTML = '<canvas data-chart></canvas>';
      chart = makeBarChart(container.querySelector("[data-chart]"), {
        labels: data.labels,
        data: data.counts,
        title: `Member Joins — ${resEl.value === "hour_of_day" ? "By Hour of Day" : "By Day of Week"}`,
        yLabel: "Members joined",
      });
    } catch (err) {
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${err.message}</div>`;
    }
  }

  resEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
