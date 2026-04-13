import { api } from "../api.js";

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

function fmtTime(ts) {
  const dt = new Date(ts * 1000);
  return dt.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

const EMOTION_COLORS = {
  joy: "#7F8F3A",
  playful: "#E6B84C",
  neutral: "#949ba4",
  frustration: "#B36A92",
  anger: "#9E3B2E",
};

export function mount(container) {
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading sentiment feed...</div></div>';
  let filter = "all"; // all | positive | negative

  async function load() {
    const d = await api("/api/health/sentiment-feed");
    const panel = container.querySelector(".panel");
    render(panel, d);
  }

  function render(panel, d) {
    const msgs = (d.messages || []).filter(m => {
      if (filter === "positive") return m.sentiment >= 0.5;
      if (filter === "negative") return m.sentiment <= -0.5;
      return true;
    });

    const rows = msgs.map(m => {
      const positive = m.sentiment >= 0.5;
      const scoreColor = positive ? "var(--success)" : "var(--danger)";
      const score = (m.sentiment > 0 ? "+" : "") + m.sentiment.toFixed(2);
      const emotionColor = EMOTION_COLORS[m.emotion] || "#949ba4";
      const channel = m.channel_name ? "#" + esc(m.channel_name) : m.channel_id;

      return `
        <tr>
          <td style="color:${scoreColor};font-weight:600;white-space:nowrap;">${score}</td>
          <td><span style="color:${emotionColor}">${esc(m.emotion || "")}</span></td>
          <td class="sf-panel-author">${esc(m.author_name || m.author_id)}</td>
          <td class="sf-panel-channel">${channel}</td>
          <td class="sf-panel-content">${esc(m.content || "")}</td>
          <td style="white-space:nowrap;color:var(--text-dim);font-size:12px;">${fmtTime(m.ts)}</td>
        </tr>
      `;
    }).join("");

    const activeBtn = (val) => filter === val ? "sf-filter-active" : "";

    panel.innerHTML = `
      <header>
        <h2>Sentiment Feed</h2>
        <div class="subtitle">Recent messages with strong positive or negative sentiment</div>
      </header>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Positive (24h)</div>
          <div class="home-card-big" style="color:var(--success)">${d.positive_24h}</div>
          <div class="home-card-sub">Score &ge; 0.5</div>
        </div>
        <div class="home-card">
          <div class="home-card-label">Negative (24h)</div>
          <div class="home-card-big" style="color:var(--danger)">${d.negative_24h}</div>
          <div class="home-card-sub">Score &le; &minus;0.5</div>
        </div>
      </div>

      <div class="home-grid">
        <div class="home-card home-card-wide">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;">
            <div class="home-card-label" style="margin:0;">Messages</div>
            <button class="sf-filter-btn ${activeBtn("all")}" data-filter="all">All</button>
            <button class="sf-filter-btn ${activeBtn("positive")}" data-filter="positive">Positive</button>
            <button class="sf-filter-btn ${activeBtn("negative")}" data-filter="negative">Negative</button>
          </div>
          <div class="data-table-scroll">
          <table class="data-table">
            <thead><tr>
              <th>Score</th><th>Emotion</th><th>Author</th>
              <th>Channel</th><th>Content</th><th>Time</th>
            </tr></thead>
            <tbody>${rows || '<tr><td colspan="6" style="text-align:center;color:var(--text-dim)">No messages</td></tr>'}</tbody>
          </table>
          </div>
        </div>
      </div>
    `;

    panel.querySelectorAll(".sf-filter-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        filter = btn.dataset.filter;
        render(panel, d);
      });
    });
  }

  load().catch(err => {
    container.querySelector(".panel").innerHTML = `<div class="error">${esc(err.message)}</div>`;
  });

  return { unmount() {} };
}
