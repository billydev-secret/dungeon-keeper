import { api } from "../api.js";
import { makeLineChart, makeHorizontalBarChart, ROLE_COLORS } from "../charts.js";

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function mount(container) {
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading sentiment data...</div></div>';
  const charts = [];

  async function load() {
    const d = await api("/api/health/sentiment");
    const panel = container.querySelector(".panel");

    const emotions = d.emotions || {};
    const emotionHTML = Object.entries(emotions)
      .sort((a, b) => b[1] - a[1])
      .map(([name, pct]) => `<span class="emotion-tag">${name}: ${pct}%</span>`)
      .join(" ");

    const spikeRows = (d.spike_log || []).map(s => `
      <tr>
        <td>${fmtTime(s.timestamp)}</td>
        <td>${s.avg_sentiment}</td>
        <td>${s.msg_count} msgs</td>
      </tr>
    `).join("");

    panel.innerHTML = `
      <header>
        <h2>Sentiment & Tone</h2>
        <div class="subtitle">Emotional temperature of the community</div>
      </header>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Average Sentiment</div>
          <div class="home-card-big">${d.avg_sentiment > 0 ? "+" : ""}${d.avg_sentiment}</div>
          <div class="home-card-sub">${d.scored_count} messages scored</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Positive : Negative</div>
          <div class="home-card-big">${d.pos_neg_ratio}:1</div>
          <div class="home-card-sub">Target: &gt;3:1</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Negative Spikes (7d)</div>
          <div class="home-card-big">${d.spikes_7d}</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Emotion Categories</div>
          <div class="home-card-sub">${emotionHTML}</div>
        </div>
      </div>

      <div class="home-card home-card-wide" style="margin-top:14px;">
        <div class="home-card-label">30-Day Sentiment Trend</div>
        <div class="chart-wrap" style="height:280px"><canvas id="sentiment-trend"></canvas></div>
      </div>

      <div class="home-grid" style="margin-top:14px;">
        <div class="home-card">
          <div class="home-card-label">Per-Channel Sentiment</div>
          <div class="chart-wrap" style="min-height:280px"><canvas id="ch-sentiment"></canvas></div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Negative Spike Log</div>
          ${spikeRows ? `<table class="data-table">
            <thead><tr><th>Time</th><th>Sentiment</th><th>Volume</th></tr></thead>
            <tbody>${spikeRows}</tbody>
          </table>` : '<div class="home-dim">No spikes this week</div>'}
        </div>
      </div>
    `;

    // Trend chart
    const trendCanvas = panel.querySelector("#sentiment-trend");
    if (trendCanvas && d.sparkline) {
      const labels = d.sparkline.map((_, i) => i === d.sparkline.length - 1 ? "today" : `${d.sparkline.length - 1 - i}d`);
      charts.push(makeLineChart(trendCanvas, {
        labels,
        series: [{ label: "Avg Sentiment", counts: d.sparkline, color: "#E6B84C" }],
        title: "Daily Average Sentiment",
      }));
    }

    // Per-channel sentiment
    const chCanvas = panel.querySelector("#ch-sentiment");
    if (chCanvas && d.per_channel) {
      const sorted = [...d.per_channel].sort((a, b) => b.avg_sentiment - a.avg_sentiment).slice(0, 15);
      charts.push(makeHorizontalBarChart(chCanvas, {
        labels: sorted.map(c => "#" + (c.channel_name || c.channel_id)),
        data: sorted.map(c => c.avg_sentiment),
        title: "Sentiment by Channel",
        xLabel: "Avg Sentiment",
        colors: sorted.map(c => c.avg_sentiment >= 0 ? "#7F8F3A" : "#9E3B2E"),
      }));
    }
  }

  load().catch(err => {
    container.querySelector(".panel").innerHTML = `<div class="error">${esc(err.message)}</div>`;
  });

  return { unmount() { charts.forEach(c => c.destroy()); } };
}
