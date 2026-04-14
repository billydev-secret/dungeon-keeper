import { api } from "../api.js";
function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

const DIM_COLORS = ["#E6B84C", "#B88A2C", "#7F8F3A", "#B36A92", "#9E3B2E", "#949ba4"];

export function mount(container) {
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading health score...</div></div>';
  const charts = [];

  async function load() {
    const d = await api("/api/health/composite-score");
    const panel = container.querySelector(".panel");

    const dims = d.dimensions || [];
    const dimBars = dims.map((dim, i) => {
      const color = DIM_COLORS[i % DIM_COLORS.length];
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

      <details class="panel-about" style="margin:8px 0 14px;">
        <summary style="cursor:pointer; font-size:0.85rem; color:var(--text-muted, #949ba4);">About this report</summary>
        <div style="margin:6px 0 0; padding:10px 14px; background:var(--bg-secondary, #2b2d31); border-radius:6px; font-size:0.85rem; line-height:1.6; color:var(--text-muted, #949ba4);">
          This score combines every health dimension into a single 0–100 number.
          Each dimension (activity, retention, sentiment, etc.) is scored individually, then weighted by how much it matters.
          The breakdown below shows which areas are strong and which are dragging the score down.
          The <strong style="color:var(--text-normal, #dbdee1);">radar chart</strong> makes imbalances easy to spot — a lopsided shape means some areas need attention.
          <strong style="color:var(--text-normal, #dbdee1);">Recommendations</strong> at the bottom suggest the highest-impact improvements.
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
        <div class="chart-wrap" style="height:360px;display:flex;justify-content:center"><canvas id="health-radar"></canvas></div>
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
            backgroundColor: "rgba(230,184,76,0.2)",
            borderColor: "#E6B84C",
            borderWidth: 2,
            pointBackgroundColor: dims.map((_, i) => DIM_COLORS[i % DIM_COLORS.length]),
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
                color: "#949ba4",
                backdropColor: "transparent",
              },
              grid: { color: "rgba(148,155,164,0.15)" },
              angleLines: { color: "rgba(148,155,164,0.15)" },
              pointLabels: { color: "#d4d9de", font: { size: 13 } },
            },
          },
          plugins: {
            legend: { display: false },
          },
        },
      });
      charts.push(chart);
    }
  }

  load().catch(err => {
    container.querySelector(".panel").innerHTML = `<div class="error">${esc(err.message)}</div>`;
  });

  return { unmount() { charts.forEach(c => c.destroy()); } };
}
