import { api } from "../api.js";
import { makeLineChart, ROLE_COLORS } from "../charts.js";

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

export function mount(container) {
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading retention data...</div></div>';
  const charts = [];

  async function load() {
    const d = await api("/api/health/cohort-retention");
    const panel = container.querySelector(".panel");

    // Cohort table
    const checkpoints = ["d1", "d7", "d14", "d30", "d60", "d90"];
    const headerCells = checkpoints.map(c => `<th>${c.toUpperCase()}</th>`).join("");
    const tableRows = (d.cohorts || []).map(c => {
      const cells = checkpoints.map(cp => {
        const val = c[cp] || 0;
        const bg = val >= 60 ? "rgba(127,143,58,0.3)" : val >= 30 ? "rgba(230,184,76,0.3)" : val > 0 ? "rgba(158,59,46,0.3)" : "transparent";
        return `<td style="background:${bg}">${val}%</td>`;
      }).join("");
      return `<tr><td>${esc(c.label)}</td><td>${c.size}</td>${cells}</tr>`;
    }).join("");

    panel.innerHTML = `
      <header>
        <h2>Cohort Retention Curves</h2>
        <div class="subtitle">D7: ${d.d7}% &middot; D30: ${d.d30}%</div>
      </header>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">D7 Retention</div>
          <div class="home-card-big">${d.d7}%</div>
          <div class="home-card-sub">Target: &gt;60%</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">D30 Retention</div>
          <div class="home-card-big">${d.d30}%</div>
          <div class="home-card-sub">Target: &gt;40%</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">D90 Retention</div>
          <div class="home-card-big">${d.d90}%</div>
          <div class="home-card-sub">Target: &gt;25%</div>
        </div>
      </div>

      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">Retention Curves by Cohort</div>
        <div class="chart-wrap" style="height:320px"><canvas id="retention-curves"></canvas></div>
      </div>

      <div class="home-card home-card-wide" style="margin-top:14px;overflow-x:auto;">
        <div class="home-card-label">Cohort Retention Table</div>
        <table class="data-table">
          <thead><tr><th>Cohort</th><th>Size</th>${headerCells}</tr></thead>
          <tbody>${tableRows}</tbody>
        </table>
      </div>
    `;

    // Multi-line retention curves
    const curvesCanvas = panel.querySelector("#retention-curves");
    if (curvesCanvas && d.cohorts && d.cohorts.length) {
      const labels = ["Join", "D1", "D7", "D14", "D30", "D60", "D90"];
      const series = d.cohorts.slice(-6).map((c, i) => ({
        label: c.label,
        counts: [100, c.d1 || 0, c.d7 || 0, c.d14 || 0, c.d30 || 0, c.d60 || 0, c.d90 || 0],
        color: ROLE_COLORS[i % ROLE_COLORS.length],
      }));
      charts.push(makeLineChart(curvesCanvas, { labels, series, title: "Retention by Weekly Cohort" }));
    }
  }

  load().catch(err => {
    container.querySelector(".panel").innerHTML = `<div class="error">${esc(err.message)}</div>`;
  });

  return { unmount() { charts.forEach(c => c.destroy()); } };
}
