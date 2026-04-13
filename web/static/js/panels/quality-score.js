import { api } from "../api.js";
import { renderSortableTable } from "../table.js";

const COMPONENT_COLORS = {
  engagement_given:     "#E6B84C",
  consistency_recency:  "#7F8F3A",
  content_resonance:    "#B88A2C",
  posting_activity:     "#B36A92",
};

function scoreColor(s) {
  if (s >= 0.6) return "#7F8F3A";
  if (s >= 0.35) return "#E6B84C";
  return "#9E3B2E";
}

function makeBreakdownChart(canvas, entries, title) {
  const minH = Math.max(200, entries.length * 28 + 60);
  canvas.parentElement.style.minHeight = `${minH}px`;

  return new Chart(canvas, {
    type: "bar",
    data: {
      labels: entries.map((e) => e.user_name || e.user_id),
      datasets: [
        { label: "Engagement (40%)",  data: entries.map((e) => (e.engagement_given * 40).toFixed(1)),     backgroundColor: COMPONENT_COLORS.engagement_given },
        { label: "Consistency (25%)", data: entries.map((e) => (e.consistency_recency * 25).toFixed(1)),  backgroundColor: COMPONENT_COLORS.consistency_recency },
        { label: "Resonance (20%)",   data: entries.map((e) => (e.content_resonance * 20).toFixed(1)),    backgroundColor: COMPONENT_COLORS.content_resonance },
        { label: "Posting (15%)",     data: entries.map((e) => (e.posting_activity * 15).toFixed(1)),     backgroundColor: COMPONENT_COLORS.posting_activity },
      ],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        title: { display: true, text: title, color: "#dbdee1", font: { size: 14 } },
        legend: { position: "bottom", labels: { color: "#dbdee1" } },
        tooltip: { backgroundColor: "#18191c", borderColor: "#3f4147", borderWidth: 1 },
      },
      scales: {
        x: { stacked: true, grid: { color: "#3f4147" }, ticks: { color: "#dbdee1" }, beginAtZero: true, max: 100,
             title: { display: true, text: "Weighted score contribution", color: "#dbdee1" } },
        y: { stacked: true, grid: { color: "#3f4147" }, ticks: { color: "#dbdee1" } },
      },
    },
  });
}

export function mount(container, initialParams) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Quality Score</h2>
        <div class="subtitle">How meaningfully each member participates</div>
      </header>
      <div class="controls">
        <label>Period
          <select data-control="days">
            <option value="1">Last day</option>
            <option value="7">Last 7 days</option>
            <option value="30">Last 30 days</option>
            <option value="60">Last 60 days</option>
            <option value="90">Last 90 days</option>
            <option value="180">Last 180 days</option>
            <option value="365">Last year</option>
          </select>
        </label>
        <label>Min active days
          <input type="number" data-control="min_days" min="1" max="90" value="${initialParams.min_days || 7}" />
        </label>
        <label>Status
          <select data-control="status">
            <option value="Active">Active only</option>
            <option value="">All</option>
          </select>
        </label>
      </div>
      <div data-top-chart style="margin-bottom:16px;"><canvas></canvas></div>
      <div data-bottom-chart style="margin-bottom:16px;"><canvas></canvas></div>
      <div data-table-wrap style="max-height:500px; overflow-y:auto;"></div>
    </div>
  `;

  const daysEl   = container.querySelector('[data-control="days"]');
  const minDaysEl = container.querySelector('[data-control="min_days"]');
  const statusEl = container.querySelector('[data-control="status"]');
  const tableWrap = container.querySelector("[data-table-wrap]");
  let topChart = null, bottomChart = null;

  daysEl.value = initialParams.days || "1";
  statusEl.value = initialParams.status ?? "Active";

  async function refresh() {
    const days = parseInt(daysEl.value) || 90;
    const minDays = parseInt(minDaysEl.value) || 7;
    const statusFilter = statusEl.value;
    const qs = new URLSearchParams({ days, min_days: minDays });
    if (statusFilter) qs.set("status", statusFilter);
    history.replaceState(null, "", `#/quality-score?${qs}`);

    try {
      const data = await api("/api/reports/quality-score", { days, min_active_days: minDays });
      if (topChart) { topChart.destroy(); topChart = null; }
      if (bottomChart) { bottomChart.destroy(); bottomChart = null; }

      let entries = data.entries;
      if (statusFilter) entries = entries.filter((e) => e.status === statusFilter);

      if (!entries.length) {
        container.querySelector("[data-top-chart]").innerHTML = `<div class="empty">No quality score data.</div>`;
        container.querySelector("[data-bottom-chart]").innerHTML = "";
        tableWrap.innerHTML = "";
        return;
      }

      // Top 10
      const top10 = entries.slice(0, 10);
      const topWrap = container.querySelector("[data-top-chart]");
      topWrap.innerHTML = "<canvas></canvas>";
      topChart = makeBreakdownChart(topWrap.querySelector("canvas"), top10, "Top 10 — Score Breakdown");

      // Bottom 10
      const scored = entries.filter((e) => e.final_score > 0);
      const bottom10 = scored.slice(-10).reverse();
      const botWrap = container.querySelector("[data-bottom-chart]");
      if (bottom10.length && scored.length > 10) {
        botWrap.innerHTML = "<canvas></canvas>";
        bottomChart = makeBreakdownChart(botWrap.querySelector("canvas"), bottom10, "Bottom 10 — Score Breakdown");
      } else {
        botWrap.innerHTML = "";
      }

      renderSortableTable(tableWrap, {
        columns: [
          { key: "user_name", label: "Member", format: (v, r) => r.user_name || r.user_id },
          { key: "final_score", label: "Score", format: (v) => `<span style="color:${scoreColor(v)};font-weight:700">${(v * 100).toFixed(1)}</span>` },
          { key: "engagement_given", label: "Engage", format: (v) => (v * 100).toFixed(0) },
          { key: "consistency_recency", label: "Consist", format: (v) => (v * 100).toFixed(0) },
          { key: "content_resonance", label: "Reson", format: (v) => (v * 100).toFixed(0) },
          { key: "posting_activity", label: "Post", format: (v) => (v * 100).toFixed(0) },
          { key: "status", label: "Status" },
          { key: "active_days", label: "Days" },
          { key: "active_weeks", label: "Weeks" },
        ],
        data: entries,
        defaultSort: "final_score",
      });
    } catch (err) {
      container.querySelector("[data-top-chart]").innerHTML = `<div class="error">${err.message}</div>`;
      container.querySelector("[data-bottom-chart]").innerHTML = "";
      tableWrap.innerHTML = "";
    }
  }

  daysEl.addEventListener("change", refresh);
  minDaysEl.addEventListener("change", refresh);
  statusEl.addEventListener("change", refresh);
  refresh();

  return {
    unmount() {
      if (topChart) { topChart.destroy(); topChart = null; }
      if (bottomChart) { bottomChart.destroy(); bottomChart = null; }
    },
  };
}
