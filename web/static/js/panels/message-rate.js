import { api } from "../api.js";
import { makeBarChart } from "../charts.js";
import { mountTimeSlider } from "../slider.js";

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Message Rate</h2>
        <div class="subtitle">Average message volume in 10-minute windows throughout the day</div>
      </header>
      <div class="controls">
        <label>Days to average
          <input type="number" data-control="days" min="1" max="365" value="${initialParams.days || 30}" />
        </label>
      </div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-slider-wrap></div>
    </div>
  `;

  const daysEl = container.querySelector('[data-control="days"]');
  let chart = null;
  let slider = null;
  const sliderWrap = container.querySelector("[data-slider-wrap]");

  function bucketLabels() {
    const out = [];
    for (let i = 0; i < 144; i++) {
      const h = Math.floor(i / 6);
      const m = (i % 6) * 10;
      out.push(`${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`);
    }
    return out;
  }

  async function refresh() {
    const days = Math.max(1, Math.min(365, parseInt(daysEl.value) || 7));
    history.replaceState(null, "", `#/message-rate?days=${days}`);
    try {
      const data = await api("/api/reports/message-rate", { days });
      if (chart) { chart.destroy(); chart = null; }
      if (slider) { slider.destroy(); slider = null; }
      const wrap = container.querySelector(".chart-wrap");
      if (!data.avg_per_day.some((v) => v > 0)) {
        wrap.innerHTML = `<div class="empty">No message activity for the selected window.</div>`;
        sliderWrap.innerHTML = "";
        return;
      }
      const labels = bucketLabels();
      const title = `Message Rate — Last ${days} day${days === 1 ? "" : "s"} (${data.tz_label})`;
      function renderChart(lo, hi) {
        if (chart) chart.destroy();
        wrap.innerHTML = '<canvas data-chart></canvas>';
        chart = makeBarChart(container.querySelector("[data-chart]"), {
          labels: labels.slice(lo, hi + 1),
          data: data.avg_per_day.slice(lo, hi + 1),
          title,
          yLabel: "Messages / 10 min (avg/day)",
        });
      }
      renderChart(0, labels.length - 1);
      sliderWrap.innerHTML = "";
      slider = mountTimeSlider(sliderWrap, { totalPoints: labels.length, labels, onChange: renderChart });
    } catch (err) {
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${err.message}</div>`;
    }
  }

  daysEl.addEventListener("change", refresh);
  refresh();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } if (slider) { slider.destroy(); slider = null; } } };
}
