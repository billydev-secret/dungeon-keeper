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
  container.innerHTML = '<div class="panel"><div class="panel-loading">Loading message feed\u2026</div></div>';

  let filters = {
    sentiment_min: -1.0,
    sentiment_max: 1.0,
    engagement_min: 0,
    replies: "all",
    connections_min: 0,
    hours: 168,
  };
  let currentOffset = 0;
  let allMessages = [];
  let total = 0;

  async function load(append) {
    if (!append) { currentOffset = 0; allMessages = []; }
    const params = new URLSearchParams({
      ...filters,
      offset: currentOffset,
      limit: 50,
    });
    const d = await api("/api/health/message-feed?" + params);
    total = d.total;
    if (append) {
      allMessages = allMessages.concat(d.messages);
    } else {
      allMessages = d.messages;
    }
    render();
  }

  function render() {
    const panel = container.querySelector(".panel");

    const rows = allMessages.map(m => {
      const eng = m.engagement || 0;
      const engColor = eng >= 5 ? "var(--green)" : eng >= 2 ? "var(--yellow)" : "var(--ink-dim)";
      const sentColor = m.sentiment >= 0.3 ? "var(--green)" : m.sentiment <= -0.3 ? "var(--red)" : "var(--ink-dim)";
      const emotionColor = EMOTION_COLORS[m.emotion] || "#949ba4";
      const channel = m.channel_name ? "#" + esc(m.channel_name) : m.channel_id;

      return `
        <tr>
          <td style="color:${engColor};font-weight:600;text-align:center;">${eng}</td>
          <td style="color:${sentColor};font-weight:600;white-space:nowrap;">${(m.sentiment > 0 ? "+" : "") + m.sentiment.toFixed(2)}</td>
          <td><span style="color:${emotionColor}">${esc(m.emotion || "")}</span></td>
          <td class="sf-panel-author">${esc(m.author_name || m.author_id)}</td>
          <td class="sf-panel-channel">${channel}</td>
          <td class="sf-panel-content">${esc(m.content || "")}</td>
          <td style="text-align:center;">${m.is_reply ? "\u21A9" : ""}</td>
          <td style="text-align:center;">${m.connections || 0}</td>
          <td style="white-space:nowrap;color:var(--ink-dim);font-size:12px;">${fmtTime(m.ts)}</td>
        </tr>
      `;
    }).join("");

    const replyBtn = (val, label) =>
      `<button class="sf-filter-btn ${filters.replies === val ? "sf-filter-active" : ""}" data-reply="${val}">${label}</button>`;

    const hoursOpt = (val, label) =>
      `<option value="${val}" ${filters.hours === val ? "selected" : ""}>${label}</option>`;

    panel.innerHTML = `
      <header>
        <h2>Message Feed</h2>
        <div class="subtitle">Messages ranked by engagement with configurable filters</div>
      </header>

      <div class="home-grid">
        <div class="home-card">
          <div class="home-card-label">Messages Matching</div>
          <div class="home-card-big">${total.toLocaleString()}</div>
        </div>
      </div>

      <div class="home-grid">
        <div class="home-card home-card-wide">
          <div class="mf-filters" style="display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin-bottom:12px;">
            <label style="display:flex;align-items:center;gap:4px;font-size:13px;">
              Min engagement
              <input type="range" min="0" max="20" step="1" value="${filters.engagement_min}" id="mf-eng-min" style="width:100px;">
              <span id="mf-eng-val">${filters.engagement_min}</span>
            </label>
            <label style="display:flex;align-items:center;gap:4px;font-size:13px;">
              Sentiment
              <input type="range" min="-10" max="10" step="1" value="${Math.round(filters.sentiment_min * 10)}" id="mf-sent-min" style="width:80px;">
              <span id="mf-sent-min-val">${filters.sentiment_min.toFixed(1)}</span>
              to
              <input type="range" min="-10" max="10" step="1" value="${Math.round(filters.sentiment_max * 10)}" id="mf-sent-max" style="width:80px;">
              <span id="mf-sent-max-val">${filters.sentiment_max.toFixed(1)}</span>
            </label>
            <div style="display:flex;align-items:center;gap:4px;font-size:13px;">
              Replies
              ${replyBtn("all", "All")}
              ${replyBtn("replies", "Replies")}
              ${replyBtn("originals", "Originals")}
            </div>
            <label style="display:flex;align-items:center;gap:4px;font-size:13px;">
              Min connections
              <input type="range" min="0" max="50" step="1" value="${filters.connections_min}" id="mf-conn-min" style="width:100px;">
              <span id="mf-conn-val">${filters.connections_min}</span>
            </label>
            <label style="display:flex;align-items:center;gap:4px;font-size:13px;">
              Time range
              <select id="mf-hours">
                ${hoursOpt(1, "1h")}
                ${hoursOpt(6, "6h")}
                ${hoursOpt(24, "24h")}
                ${hoursOpt(168, "7d")}
                ${hoursOpt(720, "30d")}
              </select>
            </label>
          </div>

          <div class="data-table-scroll">
          <table class="data-table">
            <thead><tr>
              <th>Eng.</th><th>Sent.</th><th>Emotion</th><th>Author</th>
              <th>Channel</th><th>Content</th><th>Reply</th><th>Conn.</th><th>Time</th>
            </tr></thead>
            <tbody>${rows || '<tr><td colspan="9" style="text-align:center;color:var(--ink-dim)">No messages match filters</td></tr>'}</tbody>
          </table>
          </div>
          ${allMessages.length < total ? '<div style="text-align:center;margin-top:10px;"><button class="sf-filter-btn" id="mf-load-more">Load more</button></div>' : ""}
        </div>
      </div>
    `;

    // Wire up filter controls
    const engSlider = panel.querySelector("#mf-eng-min");
    const engVal = panel.querySelector("#mf-eng-val");
    if (engSlider) {
      engSlider.addEventListener("input", () => { engVal.textContent = engSlider.value; });
      engSlider.addEventListener("change", () => { filters.engagement_min = +engSlider.value; load(); });
    }

    const sentMinSlider = panel.querySelector("#mf-sent-min");
    const sentMinVal = panel.querySelector("#mf-sent-min-val");
    if (sentMinSlider) {
      sentMinSlider.addEventListener("input", () => { sentMinVal.textContent = (sentMinSlider.value / 10).toFixed(1); });
      sentMinSlider.addEventListener("change", () => { filters.sentiment_min = sentMinSlider.value / 10; load(); });
    }

    const sentMaxSlider = panel.querySelector("#mf-sent-max");
    const sentMaxVal = panel.querySelector("#mf-sent-max-val");
    if (sentMaxSlider) {
      sentMaxSlider.addEventListener("input", () => { sentMaxVal.textContent = (sentMaxSlider.value / 10).toFixed(1); });
      sentMaxSlider.addEventListener("change", () => { filters.sentiment_max = sentMaxSlider.value / 10; load(); });
    }

    panel.querySelectorAll("[data-reply]").forEach(btn => {
      btn.addEventListener("click", () => { filters.replies = btn.dataset.reply; load(); });
    });

    const connSlider = panel.querySelector("#mf-conn-min");
    const connVal = panel.querySelector("#mf-conn-val");
    if (connSlider) {
      connSlider.addEventListener("input", () => { connVal.textContent = connSlider.value; });
      connSlider.addEventListener("change", () => { filters.connections_min = +connSlider.value; load(); });
    }

    const hoursSelect = panel.querySelector("#mf-hours");
    if (hoursSelect) {
      hoursSelect.addEventListener("change", () => { filters.hours = +hoursSelect.value; load(); });
    }

    const loadMore = panel.querySelector("#mf-load-more");
    if (loadMore) {
      loadMore.addEventListener("click", () => { currentOffset += 50; load(true); });
    }
  }

  load().catch(err => {
    container.querySelector(".panel").innerHTML = `<div class="error">${esc(err.message)}</div>`;
  });

  return { unmount() {} };
}
