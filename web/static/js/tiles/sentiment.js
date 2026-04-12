import { sparklineSVG, badgeHTML } from "./tile-helpers.js";

export function renderTile(el, data) {
  // Emotion category mini bars
  const emotions = data.emotions || {};
  const emotionBar = Object.entries(emotions)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5)
    .map(([name, pct]) => {
      const colors = { joy: "#7F8F3A", playful: "#E6B84C", neutral: "#949ba4", frustration: "#B36A92", anger: "#9E3B2E" };
      return `<div class="emotion-bar-seg" style="width:${pct}%;background:${colors[name] || "#949ba4"}" title="${name}: ${pct}%"></div>`;
    }).join("");

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
  `;
}
