import { api, esc } from "../api.js";
import { withLoading } from "../report-helpers.js";
import { renderSortableTable } from "../table.js";
import {
  CHART_TEXT, CHART_GRID, CHART_SURFACE, GENDER_COLORS, seriesColor,
  renderChartLegend, renderChartTable,
} from "../charts.js";

// Four fixed slots off the shared categorical palette, in the same order the
// score weights are listed everywhere else (engagement, consistency,
// resonance, posting). These used to be four hard-coded hexes that happened
// to be exactly the OLD ROLE_COLORS values (amber/moss/amber/wine) from
// before that palette was fixed for CVD contrast — drift that quietly kept
// the failing colours alive here after charts.js moved on from them.
const COMPONENT_COLORS = {
  engagement_given:    seriesColor(0),
  consistency_recency: seriesColor(1),
  content_resonance:   seriesColor(2),
  posting_activity:    seriesColor(3),
};

// A genuine semantic status ramp (good/mid/bad), not a per-series categorical
// colour — score bands mean the same thing everywhere they're shown, so this
// is likely fine to keep as its own fixed trio rather than pulling from
// ROLE_COLORS. FLAGGED, not fixed: "#7F8F3A" here is only a coincidental
// match to a value the old (pre-fix) ROLE_COLORS also used, not a value
// chosen deliberately as a status colour — it and its two siblings want a
// contrast check against CHART_SURFACE before anyone treats them as settled.
function scoreColor(s) {
  if (s >= 0.6) return "var(--green-text)";
  if (s >= 0.35) return "var(--yellow)";
  return "var(--red-text)";
}

function makeBreakdownChart(canvas, entries, _title) {
  const minH = Math.max(200, entries.length * 28 + 60);
  canvas.parentElement.style.minHeight = `${minH}px`;

  return new Chart(canvas, {
    type: "bar",
    data: {
      labels: entries.map((e) => e.user_name || e.user_id),
      datasets: [
        { label: "Engagement (40%)",  data: entries.map((e) => (e.engagement_given * 40).toFixed(1)),     backgroundColor: COMPONENT_COLORS.engagement_given,
          borderColor: CHART_SURFACE, borderWidth: { left: 2 }, borderSkipped: false },
        { label: "Consistency (25%)", data: entries.map((e) => (e.consistency_recency * 25).toFixed(1)),  backgroundColor: COMPONENT_COLORS.consistency_recency,
          borderColor: CHART_SURFACE, borderWidth: { left: 2 }, borderSkipped: false },
        { label: "Resonance (20%)",   data: entries.map((e) => (e.content_resonance * 20).toFixed(1)),    backgroundColor: COMPONENT_COLORS.content_resonance,
          borderColor: CHART_SURFACE, borderWidth: { left: 2 }, borderSkipped: false },
        { label: "Posting (15%)",     data: entries.map((e) => (e.posting_activity * 15).toFixed(1)),     backgroundColor: COMPONENT_COLORS.posting_activity,
          borderColor: CHART_SURFACE, borderWidth: { left: 2 }, borderSkipped: false },
      ],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        // Both drawn in HTML instead — see .chart-caption + renderChartLegend,
        // wired by the caller below.
        title: { display: false },
        legend: { display: false },
        tooltip: { backgroundColor: "#18191c", borderColor: CHART_GRID, borderWidth: 1 },
      },
      scales: {
        x: { stacked: true, grid: { color: CHART_GRID }, ticks: { color: CHART_TEXT }, beginAtZero: true, max: 100,
             title: { display: true, text: "Weighted score contribution", color: CHART_TEXT } },
        y: { stacked: true, grid: { color: CHART_GRID }, ticks: { color: CHART_TEXT } },
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
        <label>Minimum Active Days
          <input type="number" data-control="min_days" min="1" max="90" value="${initialParams.min_days || 7}" />
        </label>
        <label>Status
          <select data-control="status">
            <option value="Active">Active members only</option>
            <option value="">Everyone</option>
          </select>
        </label>
      </div>
      <div data-gender-summary style="display:flex; gap:24px; margin-bottom:16px; font-weight:700;"></div>

      <div class="chart-caption" data-top-caption></div>
      <div class="chart-wrap" data-top-chart style="margin-bottom:8px;"><canvas></canvas></div>
      <div data-top-legend></div>
      <div data-top-table style="margin-bottom:16px;"></div>

      <div class="chart-caption" data-bottom-caption></div>
      <div class="chart-wrap" data-bottom-chart style="margin-bottom:8px;"><canvas></canvas></div>
      <div data-bottom-legend></div>
      <div data-bottom-table style="margin-bottom:16px;"></div>

      <div data-table-wrap style="max-height:500px; overflow-y:auto;"></div>
    </div>
  `;

  const daysEl   = container.querySelector('[data-control="days"]');
  const minDaysEl = container.querySelector('[data-control="min_days"]');
  const statusEl = container.querySelector('[data-control="status"]');
  const tableWrap = container.querySelector("[data-table-wrap]");
  const topCaptionEl = container.querySelector("[data-top-caption]");
  const topWrap       = container.querySelector("[data-top-chart]");
  const topLegendEl   = container.querySelector("[data-top-legend]");
  const topTableEl    = container.querySelector("[data-top-table]");
  const botCaptionEl  = container.querySelector("[data-bottom-caption]");
  const botWrap        = container.querySelector("[data-bottom-chart]");
  const botLegendEl    = container.querySelector("[data-bottom-legend]");
  const botTableEl     = container.querySelector("[data-bottom-table]");
  let topChart = null, bottomChart = null;

  daysEl.value = initialParams.days || "30";
  statusEl.value = initialParams.status ?? "Active";

  // Clears just the caption/legend/table trio, leaving topWrap's own content
  // (a canvas, an empty-state message, or an error message) to whichever
  // caller sets it next.
  function clearTopAside() {
    topCaptionEl.textContent = "";
    topLegendEl.replaceChildren();
    topTableEl.replaceChildren();
  }

  function clearBottomChart() {
    botWrap.innerHTML = "";
    botCaptionEl.textContent = "";
    botLegendEl.replaceChildren();
    botTableEl.replaceChildren();
  }

  async function refresh() {
    const days = parseInt(daysEl.value) || 30;
    const minDays = parseInt(minDaysEl.value) || 7;
    const statusFilter = statusEl.value;
    const qs = new URLSearchParams({ days, min_days: minDays });
    if (statusFilter) qs.set("status", statusFilter);
    history.replaceState(null, "", `#/quality-score?${qs}`);

    try {
      const data = await withLoading(topWrap, api("/api/reports/quality-score", { days, min_active_days: minDays }));
      if (topChart) { topChart.destroy(); topChart = null; }
      if (bottomChart) { bottomChart.destroy(); bottomChart = null; }

      let entries = data.entries;
      if (statusFilter) entries = entries.filter((e) => e.status === statusFilter);

      // Gender totals always cover every member returned by the API, regardless
      // of the Status filter or the Top/Bottom 10 chart slices.
      const summaryEl = container.querySelector("[data-gender-summary]");
      if (data.entries.length) {
        const maleEntries = data.entries.filter((e) => e.gender === "male");
        const femaleEntries = data.entries.filter((e) => e.gender === "female");
        const maleSum = maleEntries.reduce((sum, e) => sum + e.final_score, 0) * 100;
        const femaleSum = femaleEntries.reduce((sum, e) => sum + e.final_score, 0) * 100;
        summaryEl.innerHTML = `
          <span style="color:${GENDER_COLORS.male}">Male total: ${maleSum.toFixed(1)} across ${maleEntries.length} members</span>
          <span style="color:${GENDER_COLORS.female}">Female total: ${femaleSum.toFixed(1)} across ${femaleEntries.length} members</span>
        `;
      } else {
        summaryEl.innerHTML = "";
      }

      if (!entries.length) {
        topWrap.innerHTML = `<div class="empty">No members clear the bar yet. Quality Score needs members with at least the minimum active days above — lower that number, widen the period, or switch Status to Everyone.</div>`;
        clearTopAside();
        clearBottomChart();
        tableWrap.innerHTML = "";
        return;
      }

      // Top 10
      const top10 = entries.slice(0, 10);
      const topTitle = "Top 10 — Score Breakdown";
      topWrap.innerHTML = "<canvas></canvas>";
      topChart = makeBreakdownChart(topWrap.querySelector("canvas"), top10, topTitle);
      // The caption lives in HTML so it wears the page's type rather than
      // whatever the canvas was handed, and can be selected and read aloud.
      topCaptionEl.textContent = topTitle;
      // Four components stacked per bar — always 2+ series, so this always
      // earns a legend (unlike a single-series chart, where the caption alone
      // would be enough).
      topLegendEl.replaceChildren();
      renderChartLegend(topLegendEl, topChart);
      // Tooltips enhance; they must never be the only way to read a value.
      renderChartTable(topTableEl, {
        labels: top10.map((e) => e.user_name || e.user_id),
        datasets: topChart.data.datasets.map((d) => ({ label: d.label, data: d.data })),
        indexLabel: "Member",
      });

      // Bottom 10
      const scored = entries.filter((e) => e.final_score > 0);
      const bottom10 = scored.slice(-10).reverse();
      if (bottom10.length && scored.length > 10) {
        const botTitle = "Bottom 10 — Score Breakdown";
        botWrap.innerHTML = "<canvas></canvas>";
        bottomChart = makeBreakdownChart(botWrap.querySelector("canvas"), bottom10, botTitle);
        botCaptionEl.textContent = botTitle;
        botLegendEl.replaceChildren();
        renderChartLegend(botLegendEl, bottomChart);
        renderChartTable(botTableEl, {
          labels: bottom10.map((e) => e.user_name || e.user_id),
          datasets: bottomChart.data.datasets.map((d) => ({ label: d.label, data: d.data })),
          indexLabel: "Member",
        });
      } else {
        clearBottomChart();
      }

      renderSortableTable(tableWrap, {
        columns: [
          { key: "user_name", label: "Member", format: (v, r) => r.user_name || r.user_id },
          // html: the markup is the point here, and the interpolated value is a
          // computed number — never a name (see table.js's ESCAPING note).
          { key: "final_score", label: "Score", html: true, format: (v) => `<span style="color:${scoreColor(v)};font-weight:700">${(v * 100).toFixed(1)}</span>` },
          { key: "engagement_given", label: "Engagement", format: (v) => (v * 100).toFixed(0) },
          { key: "consistency_recency", label: "Consistency", format: (v) => (v * 100).toFixed(0) },
          { key: "content_resonance", label: "Resonance", format: (v) => (v * 100).toFixed(0) },
          { key: "posting_activity", label: "Posting", format: (v) => (v * 100).toFixed(0) },
          { key: "status", label: "Status" },
          { key: "active_days", label: "Active Days" },
          { key: "active_weeks", label: "Active Weeks" },
        ],
        data: entries,
        defaultSort: "final_score",
        emptyMsg: "No members match this filter.",
        maxRows: 300,
      });
    } catch (err) {
      topWrap.innerHTML = `<div class="error">Couldn’t load quality scores — try again. (${esc(err.message)})</div>`;
      clearTopAside();
      clearBottomChart();
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
