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
        <h2>Cohort Retention</h2>
        <div class="subtitle">D7: ${d.d7}% &middot; D30: ${d.d30}%</div>
      </header>

      <details class="panel-about" style="margin:8px 0 14px;">
        <summary style="cursor:pointer; font-size:0.85rem; color:var(--text-muted, #949ba4);">About this report</summary>
        <div style="margin:6px 0 0; padding:10px 14px; background:var(--bg-secondary, #2b2d31); border-radius:6px; font-size:0.85rem; line-height:1.6; color:var(--text-muted, #949ba4);">
          Members are grouped into weekly <strong style="color:var(--text-normal, #dbdee1);">cohorts</strong> by when they joined.
          "D7 Retention" means the percentage of a cohort that sent at least one message 7 days after joining — and so on for D30, D60, D90.
          The table below tracks each cohort week over time, and the curves show whether your onboarding is improving or declining.
        </div>
      </details>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">D7 Retention</div>
          <div class="home-card-big">${d.d7}%</div>
          <div class="home-card-sub">Still active 7 days after joining (target: &gt;60%)</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">D30 Retention</div>
          <div class="home-card-big">${d.d30}%</div>
          <div class="home-card-sub">Still active 30 days after joining (target: &gt;40%)</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">D90 Retention</div>
          <div class="home-card-big">${d.d90}%</div>
          <div class="home-card-sub">Still active 90 days after joining (target: &gt;25%)</div>
        </div>
      </div>

      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">Retention Curves by Cohort</div>
        <div class="chart-wrap" style="height:320px"><canvas id="retention-curves"></canvas></div>
      </div>

      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">Cohort Retention Table</div>
        <div class="data-table-scroll">
        <table class="data-table">
          <thead><tr><th>Cohort</th><th>Size</th>${headerCells}</tr></thead>
          <tbody>${tableRows}</tbody>
        </table>
        </div>
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
