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
      <div class="controls" style="align-items:flex-start;">
        <label>Resolution
          <select data-control="resolution">
            ${RESOLUTIONS.map((r) => `<option value="${r.value}">${r.label}</option>`).join("")}
          </select>
        </label>
        <label>Add Role
          <div class="filter-select" data-role-search>
            <input class="filter-select-input" data-role-input type="text" placeholder="Search roles…" autocomplete="off" />
            <div class="filter-select-list" data-role-list></div>
          </div>
        </label>
      </div>
      <div data-selected-roles style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px;"></div>
      <div class="chart-wrap"><canvas data-chart></canvas></div>
      <div data-slider-wrap></div>
    </div>
  `;

  const resolutionEl = container.querySelector('[data-control="resolution"]');
  const roleInput = container.querySelector("[data-role-input]");
  const roleList = container.querySelector("[data-role-list]");
  const selectedWrap = container.querySelector("[data-selected-roles]");

  resolutionEl.value = initialParams.resolution || "week";

  let chart = null;
  let slider = null;
  const sliderWrap = container.querySelector("[data-slider-wrap]");
  let allRoles = [];
  const selected = new Set();

  // ── Role search dropdown ──────────────────────────────────────────

  function renderDropdown(filter) {
    const q = filter.toLowerCase();
    const matches = allRoles.filter(
      (r) => r.name.toLowerCase().includes(q) && !selected.has(r.name)
    ).slice(0, 20);
    roleList.innerHTML = matches.map(
      (r) => `<div class="filter-select-item" data-value="${r.name}">${r.name} (${r.member_count})</div>`
    ).join("");
    roleList.style.display = matches.length ? "block" : "none";
  }

  roleInput.addEventListener("focus", () => renderDropdown(roleInput.value));
  roleInput.addEventListener("input", () => renderDropdown(roleInput.value));
  roleList.addEventListener("mousedown", (e) => {
    const item = e.target.closest(".filter-select-item");
    if (!item) return;
    e.preventDefault();
    addRole(item.dataset.value);
    roleInput.value = "";
    roleList.style.display = "none";
  });
  roleInput.addEventListener("blur", () => {
    setTimeout(() => { roleList.style.display = "none"; }, 150);
  });

  // ── Selected role pills ───────────────────────────────────────────

  function addRole(name) {
    if (selected.has(name)) return;
    selected.add(name);
    renderPills();
    refresh();
  }

  function removeRole(name) {
    selected.delete(name);
    renderPills();
    refresh();
  }

  function renderPills() {
    selectedWrap.innerHTML = [...selected].map((name) => `
      <button class="role-pill" data-role="${name}" style="
        display:inline-flex;align-items:center;gap:4px;
        background:var(--bg-alt);border:1px solid var(--grid);border-radius:14px;
        padding:3px 10px 3px 10px;font-size:12px;color:var(--text);cursor:pointer;
      ">${name} <span style="color:var(--text-dim);font-weight:700;">&times;</span></button>
    `).join("");
    updateHashState();
  }

  selectedWrap.addEventListener("click", (e) => {
    const pill = e.target.closest(".role-pill");
    if (pill) removeRole(pill.dataset.role);
  });

  // ── Data loading ──────────────────────────────────────────────────

  async function loadRoles() {
    try {
      allRoles = await api("/api/meta/roles");
      // Apply initial selection
      const defaults = initialParams.roles || "denizen,spicy";
      for (const name of defaults.split(",").map((s) => s.trim())) {
        const match = allRoles.find((r) => r.name.toLowerCase() === name.toLowerCase());
        if (match) selected.add(match.name);
      }
      renderPills();
    } catch (err) {
      console.warn("could not load roles list:", err);
    }
  }

  function updateHashState() {
    const params = new URLSearchParams();
    params.set("resolution", resolutionEl.value);
    if (selected.size) params.set("roles", [...selected].join(","));
    history.replaceState(null, "", `#/role-growth?${params.toString()}`);
  }

  async function refresh() {
    updateHashState();
    const params = { resolution: resolutionEl.value };
    if (selected.size) params.roles = [...selected].join(",");
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
