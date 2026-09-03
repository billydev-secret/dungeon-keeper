import { api } from "../api.js";
import { renderEmpty, renderError } from "../states.js";
import { mountBotToggle, mountReloadable, rangePicker, syncHash } from "../report-helpers.js";
import { makeLineChart, makeBarChart, renderChartTable, CHART_BAR } from "../charts.js";

// Windows offered for the trend chart below. The DAU/WAU/MAU tiles never
// move with this — they're fixed 1/7/30-day definitions (see
// compute_dau_mau) — so the shortest option here starts past a week; a
// "1-day trend" would just be the DAU tile again.
const TREND_RANGES = [7, 14, 30, 60, 90, 180, 365];

export function mount(container, initialParams = {}) {
  let includeBots = initialParams.include_bots === "true";
  let days = Number(initialParams.days) || 30;
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading DAU/MAU data…</div></div>';
  const charts = [];

  async function load() {
    syncHash("health-dau-mau", {
      days,
      ...(includeBots ? { include_bots: "true" } : {}),
    });
    const params = { days };
    if (includeBots) params.include_bots = "true";
    const d = await api("/api/health/dau-mau", params);
    const panel = container.querySelector(".panel");

    // The range control (below) makes reload() a routine user action rather
    // than a rare bot-toggle flip, so a chart instance leaked per pass would
    // add up fast — destroy last render's before this one replaces the
    // canvases underneath them.
    charts.forEach((c) => c.destroy());
    charts.length = 0;

    if (!d.mau) {
      panel.innerHTML = `<header><h2>DAU / MAU Stickiness</h2><div class="subtitle">Engagement depth and daily return rate</div></header>` +
        renderEmpty("Nobody has been active in the last 30 days, so there's no stickiness to measure. This fills in as members start posting.");
      return;
    }

    const compParts = [];
    if (d.composition) {
      compParts.push(`<span style="color:var(--green-text)">${d.composition.returning} returning</span>`);
      compParts.push(`<span style="color:var(--yellow-text)">${d.composition.reactivated} reactivated</span>`);
      compParts.push(`<span style="color:var(--plum)">${d.composition.new} new</span>`);
    }

    panel.innerHTML = `
      <header>
        <h2>DAU / MAU Stickiness</h2>
        <div class="subtitle">Engagement depth and daily return rate</div>
      </header>

      <details class="panel-about">
        <summary>About this report</summary>
        <div class="note">
          <strong>DAU/MAU</strong> (daily active / monthly active) measures how "sticky" the server is — what fraction of your monthly members show up on any given day.
          20–30% is solid for a Discord community. The <strong>engagement funnel</strong> shows how many members progress from lurking to daily participation.
          <strong>Lurker activation</strong> tracks members who broke their silence in the last 30 days.
          <strong>Trend range</strong> below only resizes the trend chart — DAU/WAU/MAU above always use their own fixed 1/7/30-day windows.
        </div>
      </details>

      <div class="controls">
        <label data-slot="range"></label>
      </div>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">DAU / MAU</div>
          <div class="home-card-big">${d.dau_mau}%</div>
          <div class="home-card-sub">WAU/MAU: ${d.wau_mau}% &middot; ${d.dau} DAU of ${d.mau} MAU</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Lurker Activation</div>
          <div class="home-card-big">${d.lurker_activation}%</div>
          <div class="home-card-sub">Members who sent first message in last 30d</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Today's Composition</div>
          <div class="home-card-sub">${compParts.join(" &middot; ")}</div>
        </div>
      </div>

      <div class="home-grid">
        <div class="home-card home-card-wide">
          <div class="home-card-label">Engagement Depth Funnel</div>
          <div class="funnel-full">
            ${_funnelHTML(d.funnel)}
          </div>
        </div>
      </div>

      <div class="home-grid">
        <div class="home-card home-card-wide">
          <div class="home-card-label">DAU Trend</div>
          <div class="chart-caption" data-caption="dau-trend"></div>
          <div class="home-dim" data-trend-note hidden></div>
          <div class="chart-wrap" style="height:280px"><canvas id="dau-trend-chart"></canvas></div>
          <div data-chart-table="dau-trend"></div>
        </div>
      </div>

      <div class="home-grid">
        <div class="home-card home-card-wide">
          <div class="home-card-label">Average DAU by Day of Week</div>
          <div class="chart-caption" data-caption="dow"></div>
          <div class="chart-wrap" style="height:240px"><canvas id="dow-chart"></canvas></div>
          <div data-chart-table="dow"></div>
        </div>
      </div>
    `;

    // Shared day-range picker so this report offers the same windows as the
    // rest — only the trend chart below listens to it.
    const rangeCtl = rangePicker({ value: days, label: "Trend range", ranges: TREND_RANGES });
    const daysEl = rangeCtl.querySelector("select");
    panel.querySelector('[data-slot="range"]').replaceWith(rangeCtl);
    daysEl.addEventListener("change", () => {
      days = Number(daysEl.value);
      reload();
    });

    // DAU trend chart. One series (DAU) — the caption already names it, so
    // per the "none for one" rule this gets a caption + table, no legend.
    // The backend caps the sparkline to however much message history the
    // guild actually has (see compute_dau_mau / _dau_sparkline) rather than
    // zero-filling past it, so a requested window longer than that comes
    // back shorter — trend_days < trend_days_requested — and a note explains
    // the gap instead of the chart drawing a false collapse to zero.
    const trendCanvas = panel.querySelector("#dau-trend-chart");
    const trendNoteEl = panel.querySelector("[data-trend-note]");
    if (trendCanvas && d.sparkline && d.sparkline.length) {
      const n = d.sparkline.length;
      const labels = d.sparkline.map((_, i) => i === n - 1 ? "today" : `${n - 1 - i}d`);
      const trendTitle = `Daily Active Users (${n} day${n === 1 ? "" : "s"})`;
      charts.push(makeLineChart(trendCanvas, {
        labels,
        // CHART_BAR (not a literal "#E6B84C") — same gold, from the shared
        // chart palette rather than a locally duplicated hex.
        series: [{ label: "DAU", counts: d.sparkline, color: CHART_BAR }],
        title: trendTitle,
      }));
      panel.querySelector('[data-caption="dau-trend"]').textContent = trendTitle;
      renderChartTable(panel.querySelector('[data-chart-table="dau-trend"]'), {
        labels,
        datasets: [{ label: "DAU", data: d.sparkline }],
        indexLabel: "Day",
      });
      if (trendNoteEl) {
        const capped = d.trend_days_requested && n < d.trend_days_requested;
        trendNoteEl.textContent = capped
          ? `Message history only goes back ${n} day${n === 1 ? "" : "s"} — showing that instead of the ${d.trend_days_requested} days requested.`
          : "";
        trendNoteEl.hidden = !capped;
      }
    } else if (trendNoteEl) {
      trendNoteEl.textContent = "";
      trendNoteEl.hidden = true;
    }

    // Day-of-week chart. Also one series — caption + table, no legend.
    const dowCanvas = panel.querySelector("#dow-chart");
    if (dowCanvas && d.day_of_week) {
      const dowLabels = d.day_of_week.map(row => row.day);
      const dowData = d.day_of_week.map(row => row.avg_dau);
      const dowTitle = "Avg DAU by Weekday";
      charts.push(makeBarChart(dowCanvas, {
        labels: dowLabels,
        data: dowData,
        title: dowTitle,
      }));
      panel.querySelector('[data-caption="dow"]').textContent = dowTitle;
      renderChartTable(panel.querySelector('[data-chart-table="dow"]'), {
        labels: dowLabels,
        datasets: [{ label: "Avg DAU", data: dowData }],
        indexLabel: "Day of week",
      });
    }
  }

  // Bots are excluded from every metric by default; this is the per-report
  // opt-in. Re-injected after each render because load() rewrites the panel.
  function decorate() {
    mountBotToggle(container, includeBots, (v) => {
      includeBots = v;
      reload();
    });
  }

  // Every pass is guarded, not just the first — see mountReloadable.
  const reload = mountReloadable(container, {
    load, decorate, renderError, describe: "DAU/MAU stickiness",
  });

  return { unmount() { charts.forEach(c => c.destroy()); } };
}

function _funnelHTML(f) {
  if (!f) return "";
  const stages = [
    { label: "Total Members", count: f.total_members },
    { label: "Monthly Active", count: f.mau },
    { label: "Weekly Active", count: f.wau },
    { label: "Daily Active", count: f.dau },
    { label: "Voice Active", count: f.voice_active },
  ];
  const max = Math.max(f.total_members, 1);
  return stages.map((s, i) => {
    const pct = Math.round((s.count / max) * 100);
    const convRate = i > 0 ? ` (${stages[i-1].count ? Math.round(s.count / stages[i-1].count * 100) : 0}%)` : "";
    return `<div class="funnel-stage-full">
      <div class="funnel-bar-full" style="width:${pct}%">${s.count}${convRate}</div>
      <span class="funnel-label-full">${s.label}</span>
    </div>`;
  }).join("");
}
