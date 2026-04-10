import { api } from "../api.js";
import { makeStackedBarChart, makeLineChart } from "../charts.js";
import { mountTimeSlider } from "../slider.js";

const RESOLUTIONS = [
  { value: "day",   label: "Daily (30d)" },
  { value: "week",  label: "Weekly (12wk)" },
  { value: "month", label: "Monthly (12mo)" },
];

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>NSFW by Gender</h2>
        <div class="subtitle">Channel posting broken down by gender</div>
      </header>
      <div class="controls">
        <label>Resolution
          <select data-control="resolution">
            ${RESOLUTIONS.map((r) => `<option value="${r.value}">${r.label}</option>`).join("")}
          </select>
        </label>
        <label>Display
          <select data-control="display">
            <option value="bar">Stacked bar</option>
            <option value="line">Line chart</option>
          </select>
        </label>
        <label>Channel
          <select data-control="channel"><option value="">All NSFW</option></select>
        </label>
        <label style="flex-direction:row; align-items:center; gap:6px;">
          <input type="checkbox" data-control="media_only" />
          Media only
        </label>
      </div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-slider-wrap></div>
    </div>
  `;

  const resEl     = container.querySelector('[data-control="resolution"]');
  const dispEl    = container.querySelector('[data-control="display"]');
  const chanEl    = container.querySelector('[data-control="channel"]');
  const mediaEl   = container.querySelector('[data-control="media_only"]');

  resEl.value  = initialParams.resolution || "week";
  dispEl.value = initialParams.display || "line";
  mediaEl.checked = initialParams.media_only !== undefined ? initialParams.media_only === "1" : true;

  let chart = null;
  let slider = null;
  const sliderWrap = container.querySelector("[data-slider-wrap]");

  async function loadChannels() {
    try {
      const channels = await api("/api/meta/channels");
      for (const ch of channels.filter((c) => c.nsfw)) {
        const opt = document.createElement("option");
        opt.value = ch.id;
        opt.textContent = ch.name;
        chanEl.appendChild(opt);
      }
      if (initialParams.channel_id) chanEl.value = initialParams.channel_id;
    } catch (_) {}
  }

  async function refresh() {
    const params = {
      resolution: resEl.value,
      media_only: mediaEl.checked,
    };
    if (chanEl.value) params.channel_id = chanEl.value;
    const qs = new URLSearchParams({
      resolution: resEl.value,
      display: dispEl.value,
      media_only: mediaEl.checked ? "1" : "0",
    });
    if (chanEl.value) qs.set("channel_id", chanEl.value);
    history.replaceState(null, "", `#/nsfw-gender?${qs}`);

    try {
      const data = await api("/api/reports/nsfw-gender", params);
      if (chart) { chart.destroy(); chart = null; }
      if (slider) { slider.destroy(); slider = null; }
      const wrap = container.querySelector(".chart-wrap");
      if (!data.series.length) {
        wrap.innerHTML = `<div class="empty">No posting data for this period.</div>`;
        sliderWrap.innerHTML = "";
        return;
      }
      const title = `NSFW by Gender — ${data.window_label}`;
      function renderChart(lo, hi) {
        if (chart) chart.destroy();
        wrap.innerHTML = '<canvas data-chart></canvas>';
        const canvas = container.querySelector("[data-chart]");
        const slicedSeries = data.series.map((s) => ({ ...s, counts: s.counts.slice(lo, hi + 1) }));
        const slicedLabels = data.labels.slice(lo, hi + 1);
        if (dispEl.value === "line") {
          chart = makeLineChart(canvas, { labels: slicedLabels, series: slicedSeries.map((s) => ({ ...s, role: s.gender })), title });
        } else {
          chart = makeStackedBarChart(canvas, { labels: slicedLabels, series: slicedSeries, title });
        }
      }
      renderChart(0, data.labels.length - 1);
      sliderWrap.innerHTML = "";
      slider = mountTimeSlider(sliderWrap, { totalPoints: data.labels.length, labels: data.labels, onChange: renderChart });
    } catch (err) {
      container.querySelector(".chart-wrap").innerHTML = `<div class="error">${err.message}</div>`;
    }
  }

  for (const el of [resEl, dispEl, chanEl, mediaEl]) el.addEventListener("change", refresh);

  (async () => { await loadChannels(); await refresh(); })();

  return { unmount() { if (chart) { chart.destroy(); chart = null; } if (slider) { slider.destroy(); slider = null; } } };
}
