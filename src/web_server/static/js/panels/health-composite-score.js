import { api, esc } from "../api.js";
import { renderEmpty, renderError } from "../states.js";
import { mountBotToggle } from "../report-helpers.js";
import { renderChartTable, seriesColor, CHART_BAR, CHART_TEXT, CHART_GRID } from "../charts.js";

/** Expand a "#rrggbb" literal to an rgba() string at the given alpha. */
function withAlpha(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

export function mount(container) {
  let includeBots = false;
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading health score…</div></div>';
  const charts = [];

  async function load() {
    const d = await api("/api/health/composite-score", includeBots ? { include_bots: "true" } : undefined);
    const panel = container.querySelector(".panel");


    if (!(d.dimensions || []).length) {
      panel.innerHTML = `<header><h2>Community Health Score</h2><div class="subtitle">Weighted aggregate of all health dimensions</div></header>` +
        renderEmpty("Not enough activity to score yet. The health score needs roughly a week of messages, joins, and moderator actions before its dimensions mean anything.");
      return;
    }

    const dims = d.dimensions || [];
    const dimBars = dims.map((dim, i) => {
      const color = seriesColor(i);
      const badge = dim.score >= 80 ? "excellent" : dim.score >= 60 ? "healthy" : dim.score >= 40 ? "needs_work" : "critical";
      return `<div class="health-dim-row">
        <span class="health-dim-name">${dim.name} <span class="home-dim">(${dim.weight}%)</span></span>
        <div class="health-dim-track">
          <div class="health-dim-fill health-dim-fill-${badge}" style="width:${dim.score}%;background:${color}"></div>
        </div>
        <span class="health-dim-val">${dim.score}</span>
      </div>`;
    }).join("");

    const recCards = (d.recommendations || []).map(r => `
      <div class="home-card" style="border-left:3px solid ${r.score < 40 ? "#9E3B2E" : "#E6B84C"}">
        <div class="home-card-label">${esc(r.dimension)} (Score: ${r.score})</div>
        <div class="home-card-sub">${esc(r.action)}</div>
        <div class="home-card-sub home-dim">Estimated impact: +${r.estimated_impact} points</div>
      </div>
    `).join("");

    const scoreColor = d.score >= 80 ? "#7F8F3A" : d.score >= 60 ? "#E6B84C" : d.score >= 40 ? "#B88A2C" : "#9E3B2E";

    panel.innerHTML = `
      <header>
        <h2>Community Health Score</h2>
        <div class="subtitle">Weighted aggregate of all health dimensions</div>
      </header>

      <details class="panel-about">
        <summary>About this report</summary>
        <div class="note">
          This score combines every health dimension into a single 0–100 number.
          Each dimension (activity, retention, sentiment, etc.) is scored individually, then weighted by how much it matters.
          The breakdown below shows which areas are strong and which are dragging the score down.
          The <strong>radar chart</strong> makes imbalances easy to spot — a lopsided shape means some areas need attention.
          <strong>Recommendations</strong> at the bottom suggest the highest-impact improvements.
        </div>
      </details>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Overall Score</div>
          <div class="home-card-big" style="color:${scoreColor};font-size:3em">${d.score}</div>
          <div class="home-card-sub">/100</div>
        </div>
        <div class="home-card" style="flex:2">
          <div class="home-card-label">Dimension Breakdown</div>
          ${dimBars}
        </div>
      </div>

      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">Health Radar</div>
        <div class="chart-caption">Score out of 100 on each dimension, most recent snapshot</div>
        <div class="chart-wrap" style="height:360px;display:flex;justify-content:center"><canvas id="health-radar"></canvas></div>
        <div data-chart-table></div>
      </div>

      ${recCards ? `
      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">Recommendations</div>
        <div class="home-grid">${recCards}</div>
      </div>` : ""}
    `;

    // Radar chart using Chart.js directly
    const radarCanvas = panel.querySelector("#health-radar");
    if (radarCanvas && dims.length) {
      const chart = new Chart(radarCanvas, {
        type: "radar",
        data: {
          labels: dims.map(d => d.name),
          datasets: [{
            label: "Score",
            data: dims.map(d => d.score),
            backgroundColor: withAlpha(CHART_BAR, 0.2),
            borderColor: CHART_BAR,
            borderWidth: 2,
            // seriesColor, not a local modulo — past 6 dimensions this folds to the
            // shared neutral overflow instead of silently repeating a hue.
            pointBackgroundColor: dims.map((_, i) => seriesColor(i)),
            pointRadius: 5,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            r: {
              min: 0,
              max: 100,
              ticks: {
                stepSize: 20,
                color: CHART_TEXT,
                backdropColor: "transparent",
              },
              grid: { color: CHART_GRID },
              angleLines: { color: CHART_GRID },
              pointLabels: { color: CHART_TEXT, font: { size: 13 } },
            },
          },
          plugins: {
            // A single "Score" dataset across several dimensions — one shared
            // radial scale, not a dual-axis chart. The caption above already
            // names it, so per "none for one" this needs no legend; the table
            // below carries the precise values a radar is notoriously hard to
            // eyeball.
            legend: { display: false },
          },
        },
      });
      charts.push(chart);

      renderChartTable(panel.querySelector("[data-chart-table]"), {
        labels: dims.map(dim => dim.name),
        datasets: [{ label: "Score", data: dims.map(dim => dim.score) }],
        indexLabel: "Dimension",
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

  function reload() {
    return load().then(decorate);
  }

  reload().catch(err => {
    container.querySelector(".panel").innerHTML = renderError(
      `Couldn't load the health score — ${err.message}. Reload the page to try again.`
    );
  });

  return { unmount() { charts.forEach(c => c.destroy()); } };
}
