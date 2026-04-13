import { api } from "../api.js";
import { makeBarChart, makeHorizontalBarChart, ROLE_COLORS } from "../charts.js";

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

function timeAgo(ts) {
  const diff = (Date.now() / 1000) - ts;
  if (diff < 3600) return Math.round(diff / 60) + "m ago";
  if (diff < 86400) return Math.round(diff / 3600) + "h ago";
  return Math.round(diff / 86400) + "d ago";
}

const TIER_COLORS = { critical: "#9E3B2E", declining: "#E6B84C", watch: "#B88A2C" };

export function mount(container) {
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading churn risk data...</div></div>';
  const charts = [];

  async function load() {
    const d = await api("/api/health/churn-risk");
    const panel = container.querySelector(".panel");

    const signalLabels = ["Frequency", "Channels", "Reciprocity", "Sentiment", "Gap"];
    const signalKeys = ["frequency", "channels", "reciprocity", "sentiment", "gap"];

    const riskRows = (d.at_risk || []).map(r => {
      const signalBars = signalKeys.map((k, i) => {
        const val = r.signals[k] || 0;
        return `<div class="risk-signal">
          <span class="risk-signal-label">${signalLabels[i]}</span>
          <div class="risk-signal-track"><div class="risk-signal-fill" style="width:${val}%;background:${TIER_COLORS[r.tier]}"></div></div>
          <span class="risk-signal-val">${val}%</span>
        </div>`;
      }).join("");
      return `<tr>
        <td>${esc(r.user_name || r.user_id)}</td>
        <td><span class="risk-tier risk-${r.tier}">${r.tier}</span></td>
        <td><strong>${r.score}</strong></td>
        <td class="risk-signals-cell">${signalBars}</td>
        <td>${r.last_seen ? timeAgo(r.last_seen) : "—"}</td>
      </tr>`;
    }).join("");

    panel.innerHTML = `
      <header>
        <h2>Churn Risk</h2>
        <div class="subtitle">${d.at_risk_count} members at risk</div>
      </header>

      <details class="panel-about" style="margin:8px 0 14px;">
        <summary style="cursor:pointer; font-size:0.85rem; color:var(--text-muted, #949ba4);">About this report</summary>
        <div style="margin:6px 0 0; padding:10px 14px; background:var(--bg-secondary, #2b2d31); border-radius:6px; font-size:0.85rem; line-height:1.6; color:var(--text-muted, #949ba4);">
          Each member gets a risk score (0–100) based on five signals:
          <strong style="color:var(--text-normal, #dbdee1);">Frequency</strong> — are they posting less often?
          <strong style="color:var(--text-normal, #dbdee1);">Channels</strong> — are they visiting fewer channels?
          <strong style="color:var(--text-normal, #dbdee1);">Reciprocity</strong> — are fewer people replying to them?
          <strong style="color:var(--text-normal, #dbdee1);">Sentiment</strong> — has their tone turned negative?
          <strong style="color:var(--text-normal, #dbdee1);">Gap</strong> — how long since they were last seen?
          Members in the <em>critical</em> tier (80+) are likely about to leave.
        </div>
      </details>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">At-Risk Members</div>
          <div class="home-card-big">${d.at_risk_count}</div>
          <div class="home-card-sub">Showing signs of disengagement</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Critical</div>
          <div class="home-card-big" style="color:#9E3B2E">${d.critical}</div>
          <div class="home-card-sub">Score &ge; 80</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Declining</div>
          <div class="home-card-big" style="color:#E6B84C">${d.declining}</div>
          <div class="home-card-sub">Score 50 - 79</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Watch</div>
          <div class="home-card-big" style="color:#B88A2C">${d.watch}</div>
          <div class="home-card-sub">Score 30 - 49</div>
        </div>
      </div>

      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">Risk Score Distribution</div>
        <div class="chart-wrap" style="height:260px"><canvas id="risk-dist-chart"></canvas></div>
      </div>

      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">At-Risk Members</div>
        <div class="data-table-scroll">
        <table class="data-table">
          <thead><tr><th>Member</th><th>Tier</th><th>Score</th><th>Signal Breakdown</th><th>Last Seen</th></tr></thead>
          <tbody>${riskRows || '<tr><td colspan="5" class="home-dim">No at-risk members</td></tr>'}</tbody>
        </table>
        </div>
      </div>
    `;

    // Risk distribution histogram
    const distCanvas = panel.querySelector("#risk-dist-chart");
    if (distCanvas && d.risk_distribution) {
      const bucketLabels = ["0-9", "10-19", "20-29", "30-39", "40-49", "50-59", "60-69", "70-79", "80-89", "90-100"];
      const bucketColors = bucketLabels.map((_, i) =>
        i < 3 ? "#7F8F3A" : i < 5 ? "#B88A2C" : i < 8 ? "#E6B84C" : "#9E3B2E"
      );
      charts.push(makeBarChart(distCanvas, {
        labels: bucketLabels,
        datasets: [{ label: "Members", data: d.risk_distribution, backgroundColor: bucketColors }],
        title: "Risk Score Distribution",
        yLabel: "Members",
        xLabel: "Risk Score",
      }));
    }
  }

  load().catch(err => {
    container.querySelector(".panel").innerHTML = `<div class="error">${esc(err.message)}</div>`;
  });

  return { unmount() { charts.forEach(c => c.destroy()); } };
}
