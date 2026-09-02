import { api, esc } from "../api.js";
import { renderEmpty, renderError } from "../states.js";
import { mountBotToggle, mountReloadable } from "../report-helpers.js";
import { makeLineChart, makeBarChart, renderChartTable, CHART_BAR } from "../charts.js";


export function mount(container) {
  let includeBots = false;
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading DAU/MAU data…</div></div>';
  const charts = [];

  async function load() {
    const d = await api("/api/health/dau-mau", includeBots ? { include_bots: "true" } : undefined);
    const panel = container.querySelector(".panel");


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
        </div>
      </details>

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
          <div class="home-card-label">30-Day DAU Trend</div>
          <div class="chart-caption" data-caption="dau-trend"></div>
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

    // DAU trend chart. One series (DAU) — the caption already names it, so
    // per the "none for one" rule this gets a caption + table, no legend.
    const trendCanvas = panel.querySelector("#dau-trend-chart");
    if (trendCanvas && d.sparkline) {
      const labels = d.sparkline.map((_, i) => i === d.sparkline.length - 1 ? "today" : `${d.sparkline.length - 1 - i}d`);
      const trendTitle = "Daily Active Users (30 days)";
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
