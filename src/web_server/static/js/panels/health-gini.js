import { api } from "../api.js";
import { makeLineChart, makeHorizontalBarChart, makeDoughnutChart } from "../charts.js";

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

export function mount(container) {
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading Gini data...</div></div>';
  const charts = [];

  async function load() {
    const d = await api("/api/health/gini");
    const panel = container.querySelector(".panel");

    const tiers = d.tiers || {};

    panel.innerHTML = `
      <header>
        <h2>Participation Gini</h2>
        <div class="subtitle">Message distribution inequality &middot; ${d.gini} (${d.badge})</div>
      </header>

      <details class="panel-about">
        <summary>About this report</summary>
        <div class="note">
          The <strong>Gini coefficient</strong> measures how evenly messages are spread across members.
          0 means everyone posts equally; 1 means one person writes everything. Most healthy communities land between 0.5–0.75.
          The <strong>Lorenz curve</strong> visualizes this — the further it bows from the diagonal, the more concentrated activity is.
          The <strong>Palma ratio</strong> compares the top 10% to the bottom 40% — a high ratio means a small group dominates conversation.
        </div>
      </details>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Gini Coefficient</div>
          <div class="home-card-big">${d.gini}</div>
          <div class="home-card-sub">0 = equal, 1 = one person posts all</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Top 5% Share</div>
          <div class="home-card-big">${d.top5_share}%</div>
          <div class="home-card-sub">Top 10%: ${d.top10_share}%</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Palma Ratio</div>
          <div class="home-card-big">${d.palma}</div>
          <div class="home-card-sub">Top 10% / Bottom 40%. Target: &lt;4.0</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Weighted Gini</div>
          <div class="home-card-big">${d.weighted_gini}</div>
          <div class="home-card-sub">Msgs + reactions + voice</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">XP Gini</div>
          <div class="home-card-big">${d.xp_gini}</div>
          <div class="home-card-sub">XP distribution inequality</div>
        </div>
      </div>

      <div class="home-grid">
        <div class="home-card home-card-wide">
          <div class="home-card-label">Gini Over Time</div>
          <div class="chart-wrap" style="height:260px"><canvas id="gini-history-chart"></canvas></div>
        </div>
      </div>

      <div class="home-grid">
        <div class="home-card home-card-wide">
          <div class="home-card-label">Lorenz Curve</div>
          <div class="chart-wrap" style="height:320px"><canvas id="lorenz-chart"></canvas></div>
        </div>
      </div>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Participation Tiers</div>
          <div class="chart-wrap" style="height:260px"><canvas id="tier-chart"></canvas></div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Per-Channel Gini</div>
          <div class="chart-wrap" style="min-height:260px"><canvas id="ch-gini-chart"></canvas></div>
        </div>
      </div>
    `;

    // Gini over time
    const histCanvas = panel.querySelector("#gini-history-chart");
    if (histCanvas && d.gini_history?.length) {
      charts.push(makeLineChart(histCanvas, {
        labels: d.gini_history.map(p => p.label),
        series: [
          { label: "Gini", counts: d.gini_history.map(p => p.gini), color: "#E6B84C" },
        ],
        title: "Weekly Gini coefficient (12 weeks)",
      }));
    }

    // Lorenz curve
    const lorenzCanvas = panel.querySelector("#lorenz-chart");
    if (lorenzCanvas && d.lorenz) {
      const labels = d.lorenz.map(p => p.x + "%");
      charts.push(makeLineChart(lorenzCanvas, {
        labels,
        series: [
          { label: "Equality", counts: d.lorenz.map(p => p.x), color: "#949ba4" },
          { label: "Actual", counts: d.lorenz.map(p => p.y), color: "#E6B84C" },
        ],
        title: "Lorenz Curve (cumulative messages vs population)",
      }));
    }

    // Tier doughnut
    const tierCanvas = panel.querySelector("#tier-chart");
    if (tierCanvas) {
      charts.push(makeDoughnutChart(tierCanvas, {
        labels: ["Lurker (0)", "Light (1-5/wk)", "Moderate (6-20)", "Active (21-50)", "Power (50+)"],
        data: [tiers.lurker || 0, tiers.light || 0, tiers.moderate || 0, tiers.active || 0, tiers.power || 0],
        title: "Participation Tiers",
        colors: ["#949ba4", "#B88A2C", "#E6B84C", "#7F8F3A", "#B36A92"],
      }));
    }

    // Per-channel Gini
    const chCanvas = panel.querySelector("#ch-gini-chart");
    if (chCanvas && d.per_channel) {
      charts.push(makeHorizontalBarChart(chCanvas, {
        labels: d.per_channel.map(c => "#" + (c.channel_name || c.channel_id)),
        data: d.per_channel.map(c => c.gini),
        title: "Gini by Channel",
        xLabel: "Gini coefficient",
        colors: d.per_channel.map(c => c.gini > 0.85 ? "#9E3B2E" : c.gini > 0.7 ? "#E6B84C" : "#7F8F3A"),
      }));
    }
  }

  load().catch(err => {
    container.querySelector(".panel").innerHTML = `<div class="error">${esc(err.message)}</div>`;
  });

  return { unmount() { charts.forEach(c => c.destroy()); } };
}
