import { api } from "../api.js";
import { makeCandlestickChart } from "../charts.js";
import { mountTimeSlider } from "../slider.js";

const RESOLUTIONS = [
  { value: "hour",        label: "Hourly (24h)" },
  { value: "day",         label: "Daily (30d)" },
  { value: "week",        label: "Weekly (12wk)" },
  { value: "month",       label: "Monthly (12mo)" },
  { value: "hour_of_day", label: "By Hour of Day" },
  { value: "day_of_week", label: "By Day of Week" },
];

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Message Cadence</h2>
        <div class="subtitle">Time between messages — min, P20, median, P80, max (in minutes)</div>
      </header>
      <div class="controls">
        <label>Resolution
          <select data-control="resolution">
            ${RESOLUTIONS.map((r) => `<option value="${r.value}">${r.label}</option>`).join("")}
          </select>
        </label>
        <label>Channel
          <select data-control="channel"><option value="">All channels</option></select>
        </label>
      </div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-slider-wrap></div>
    </div>
  `;

  const resEl = container.querySelector('[data-control="resolution"]');
  const chanEl = container.querySelector('[data-control="channel"]');
  resEl.value = initialParams.resolution || "day";

  let chart = null;
  let slider = null;
  const sliderWrap = container.querySelector("[data-slider-wrap]");

  async function loadChannels() {
    try {
      const channels = await api("/api/meta/channels");
      for (const ch of channels) {
        const opt = document.createElement("option");
        opt.value = ch.id;
        opt.textContent = ch.name;
        chanEl.appendChild(opt);
      }
      if (initialParams.channel_id) chanEl.value = initialParams.channel_id;
    } catch (_) {}
  }

  async function refresh() {
    const params = { resolution: resEl.value };
    if (chanEl.value) params.channel_id = chanEl.value;

    history.replaceState(null, "", `#/message-cadence?${new URLSearchParams(params)}`);
    try {
      const data = await api("/api/reports/message-cadence", params);
      if (chart) { chart.destroy(); chart = null; }
      if (slider) { slider.destroy(); slider = null; }
      const wrap = container.querySelector(".chart-wrap");
      if (!data.buckets.length) {
        wrap.innerHTML = `<div class="empty">No message data for this period.</div>`;
        sliderWrap.innerHTML = "";
        return;
      }
      const labels = data.buckets.map((b) => b.label);
      function renderChart(lo, hi) {
        if (chart) chart.destroy();
        wrap.innerHTML = '<canvas data-chart></canvas>';
        chart = makeCandlestickChart(container.querySelector("[data-chart]"), {
          buckets: data.buckets.slice(lo, hi + 1),
          title: `Message Cadence — ${data.window_label}`,
        });
      }
      renderChart(0, data.buckets.length - 1);
      sliderWrap.innerHTML = "";
      slider = mountTimeSlider(sliderWrap, { totalPoints: data.buckets.length, labels, onChange: renderChart });
    } catch (err) {
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${err.message}</div>`;
    }
  }

  resEl.addEventListener("change", refresh);
  chanEl.addEventListener("change", refresh);

  (async () => { await loadChannels(); await refresh(); })();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } if (slider) { slider.destroy(); slider = null; } } };
}
