import { badgeHTML, esc, fmtAgo } from "./tile-helpers.js";

export function renderTile(el, data, names) {
  const chNames = names ? names.channels || {} : {};
  const uNames = names ? names.users || {} : {};

  // Emotion category mini bars
  const emotions = data.emotions || {};
  const emotionBar = Object.entries(emotions)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5)
    .map(([name, pct]) => {
      const colors = { joy: "#7F8F3A", playful: "#E6B84C", neutral: "#949ba4", frustration: "#B36A92", anger: "#9E3B2E" };
      return `<div class="emotion-bar-seg" style="width:${pct}%;background:${colors[name] || "#949ba4"}" title="${name}: ${pct}%"></div>`;
    }).join("");

  // Outlier messages (1σ above/below baseline)
  const outliers = data.outliers || { top: [], bottom: [] };

  function msgSnippet(m, _label) {
    const score = (m.sentiment > 0 ? "+" : "") + m.sentiment.toFixed(2);
    const scoreColor = m.sentiment >= 0 ? "var(--success)" : "var(--danger)";
    const author = uNames[m.author_id] || "";
    const channel = chNames[m.channel_id] ? "#" + chNames[m.channel_id] : "";
    const snippet = m.content && m.content.length > 60
      ? m.content.slice(0, 60) + "\u2026"
      : (m.content || "");
    return `
      <div class="sf-row" style="padding:3px 0;">
        <span class="sf-score" style="color:${scoreColor};min-width:38px;">${score}</span>
        <span class="sf-body" style="min-width:0;">
          <span class="sf-meta">${esc(author)}${channel ? " in " + esc(channel) : ""}</span>
          <span class="sf-text">${esc(snippet)}</span>
        </span>
        <span class="sf-time">${fmtAgo(m.ts)}</span>
      </div>
    `;
  }

  const topMsg = outliers.top.length ? msgSnippet(outliers.top[0], "top") : "";
  const botMsg = outliers.bottom.length ? msgSnippet(outliers.bottom[0], "bottom") : "";
  const outlierHTML = (topMsg || botMsg) ? `
    <div style="margin-top:6px;border-top:1px solid var(--border, #3f4147);padding-top:6px;">
      <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">Outliers (&plusmn;1&sigma;)</div>
      ${topMsg}${botMsg}
    </div>
  ` : "";

  el.innerHTML = `
    <div class="health-tile-header">
      <span class="health-tile-label">Sentiment & Tone</span>
      ${badgeHTML(data.badge)}
    </div>
    <div class="health-tile-metric">${data.avg_sentiment > 0 ? "+" : ""}${data.avg_sentiment}</div>
    <div class="emotion-bar">${emotionBar}</div>
    <div class="health-tile-companions">
      <span>${data.spikes_7d} spikes (7d)</span>
      <span>Ratio: ${data.pos_neg_ratio}:1</span>
    </div>
    ${outlierHTML}
  `;
}
