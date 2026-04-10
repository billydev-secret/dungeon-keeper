import { api } from "../api.js";
import { makeLineChart } from "../charts.js";
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
        <h2>Role Growth</h2>
        <div class="subtitle">Net role membership over time</div>
      </header>
      <div class="controls">
        <label>Resolution
          <select data-control="resolution">
            ${RESOLUTIONS.map((r) => `<option value="${r.value}">${r.label}</option>`).join("")}
          </select>
        </label>
        <label>Roles (multi-select — Ctrl/Cmd for multi)
          <select data-control="roles" multiple size="6"></select>
        </label>
        <button data-control="reset" type="button">Show all</button>
      </div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-slider-wrap></div>
    </div>
  `;

  const resolutionEl = container.querySelector('[data-control="resolution"]');
  const rolesEl = container.querySelector('[data-control="roles"]');
  const resetEl = container.querySelector('[data-control="reset"]');
  const canvas = container.querySelector("[data-chart]");

  resolutionEl.value = initialParams.resolution || "week";
  if (initialParams.roles) {
    // Selection applied once the roles list has been populated.
  }

  let chart = null;
  let slider = null;
  const sliderWrap = container.querySelector("[data-slider-wrap]");
  let rolesLoaded = false;

  async function loadRoles() {
    try {
      const roles = await api("/api/meta/roles");
      rolesEl.innerHTML = roles
        .map((r) => `<option value="${r.name}">${r.name} (${r.member_count})</option>`)
        .join("");
      const defaultRoles = initialParams.roles || "denizen,spicy";
      const wanted = new Set(defaultRoles.split(",").map((s) => s.trim().toLowerCase()));
      for (const opt of rolesEl.options) {
        if (wanted.has(opt.value.toLowerCase())) opt.selected = true;
      }
      rolesLoaded = true;
    } catch (err) {
      console.warn("could not load roles list:", err);
    }
  }

  function selectedRoles() {
    return Array.from(rolesEl.selectedOptions).map((o) => o.value);
  }

  function updateHashState() {
    const sel = selectedRoles();
    const params = new URLSearchParams();
    params.set("resolution", resolutionEl.value);
    if (sel.length) params.set("roles", sel.join(","));
    history.replaceState(null, "", `#/role-growth?${params.toString()}`);
  }

  async function refresh() {
    updateHashState();
    const sel = selectedRoles();
    const params = { resolution: resolutionEl.value };
    if (sel.length) params.roles = sel.join(",");
    try {
      const data = await api("/api/reports/role-growth", params);
      if (chart) { chart.destroy(); chart = null; }
      if (slider) { slider.destroy(); slider = null; }
      if (!data.series.length) {
        const wrap = container.querySelector(".chart-wrap");
        wrap.innerHTML = `<div class="empty">No role grant history recorded yet.</div>`;
        sliderWrap.innerHTML = "";
        return;
      }

      function renderChart(lo, hi) {
        if (chart) chart.destroy();
        const wrap = container.querySelector(".chart-wrap");
        wrap.innerHTML = '<canvas data-chart></canvas>';
        chart = makeLineChart(container.querySelector("[data-chart]"), {
          labels: data.labels.slice(lo, hi + 1),
          series: data.series.map((s) => ({ ...s, counts: s.counts.slice(lo, hi + 1) })),
          title: `Role Growth — ${data.window_label}`,
        });
      }
      renderChart(0, data.labels.length - 1);
      sliderWrap.innerHTML = "";
      slider = mountTimeSlider(sliderWrap, {
        totalPoints: data.labels.length,
        labels: data.labels,
        onChange: renderChart,
      });
    } catch (err) {
      const wrap = container.querySelector(".chart-wrap");
      wrap.innerHTML = `<div class="error">${err.message}</div>`;
    }
  }

  resolutionEl.addEventListener("change", refresh);
  rolesEl.addEventListener("change", refresh);
  resetEl.addEventListener("click", () => {
    for (const opt of rolesEl.options) opt.selected = false;
    refresh();
  });

  (async () => {
    await loadRoles();
    await refresh();
  })();

  return {
    unmount() {
      if (chart) { try { chart.destroy(); } catch (_) {} chart = null; }
      if (slider) { slider.destroy(); slider = null; }
    },
  };
}
