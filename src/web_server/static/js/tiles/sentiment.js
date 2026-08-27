import { badgeHTML, esc, fmtAgo } from "./tile-helpers.js";
import { seriesColor, SERIES_OVERFLOW } from "../charts.js";

export function renderTile(el, data, names) {
  const chNames = names ? names.channels || {} : {};
  const uNames = names ? names.users || {} : {};

  // Emotion category mini bars
  const emotions = data.emotions || {};
  const emotionBar = Object.entries(emotions)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5)
    .map(([name, pct]) => {
      // Five named emotions is a CATEGORICAL set, not a severity ramp — these
      // are different things, not degrees of one thing — so they take fixed
      // slots from the validated palette. They were five retired-palette
      // literals, including the amber/moss pair that measured ΔE 1.8 under
      // protanopia; joy and playful sat directly adjacent in this bar.
      const EMOTION_SLOT = { joy: 1, playful: 0, neutral: 3, frustration: 4, anger: 5 };
      const slot = EMOTION_SLOT[name];
      const color = slot === undefined ? SERIES_OVERFLOW : seriesColor(slot);
      return `<div class="emotion-bar-seg" style="width:${pct}%;background:${color}" title="${name}: ${pct}%"></div>`;
    }).join("");

  // Outlier messages (1σ above/below baseline)
  const outliers = data.outliers || { top: [], bottom: [] };

  function msgSnippet(m, _label) {
    const score = (m.sentiment > 0 ? "+" : "") + m.sentiment.toFixed(2);
    const scoreColor = m.sentiment >= 0 ? "var(--green)" : "var(--red)";
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
    <div style="margin-top:6px;border-top:1px solid var(--rule);padding-top:6px;">
      <div style="font-size:11px;color:var(--ink-dim);margin-bottom:4px;">Outliers (&plusmn;1&sigma;)</div>
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
