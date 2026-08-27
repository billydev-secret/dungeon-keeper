import { api, esc } from "../api.js";
import { withLoading } from "../report-helpers.js";
import { makeBarChart, renderChartTable } from "../charts.js";

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
        <label>Group By
          <select data-control="resolution">
            ${RESOLUTIONS.map((r) => `<option value="${r.value}">${r.label}</option>`).join("")}
          </select>
        </label>
      </div>
      <div class="chart-caption" data-caption></div>
      <div class="chart-wrap"><div class="empty">Loading join times…</div></div>
      <div data-chart-table></div>
    </div>
  `;

  const resEl = container.querySelector('[data-control="resolution"]');
  resEl.value = initialParams.resolution || "hour_of_day";
  const captionEl = container.querySelector("[data-caption]");
  const tableEl = container.querySelector("[data-chart-table]");
  let chart = null;

  async function refresh() {
    history.replaceState(null, "", `#/join-times?resolution=${resEl.value}`);
    const wrap = container.querySelector(".chart-wrap");
    const resolutionLabel = resEl.value === "hour_of_day" ? "By Hour of Day" : "By Day of Week";
    try {
      const data = await withLoading(wrap, api("/api/reports/join-times", { resolution: resEl.value }));
      if (chart) { chart.destroy(); chart = null; }
      if (!data.counts || !data.counts.some((n) => n > 0)) {
        wrap.innerHTML = '<div class="empty">No join history recorded yet. '
          + 'Dungeon Keeper logs joins from the moment it joined your server.</div>';
        captionEl.textContent = "";
        tableEl.replaceChildren();
        return;
      }
      const title = `Member Joins — ${resolutionLabel}`;
      wrap.innerHTML = '<canvas data-chart></canvas>';
      // The caption lives in HTML so it wears the page's type rather than
      // whatever the canvas was handed, and can be selected and read aloud.
      captionEl.textContent = title;
      chart = makeBarChart(container.querySelector("[data-chart]"), {
        labels: data.labels,
        data: data.counts,
        title,
        yLabel: "Members joined",
      });
      // One series: the caption already names it, so a legend would just
      // repeat itself. A table still stands in for the tooltip.
      renderChartTable(tableEl, {
        labels: data.labels,
        datasets: [{ label: "Members joined", data: data.counts }],
        indexLabel: resolutionLabel,
      });
    } catch (err) {
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">Couldn’t load join times — try again. (${esc(err.message)})</div>`;
      captionEl.textContent = "";
      tableEl.replaceChildren();
    }
  }

  resEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } } };
}
