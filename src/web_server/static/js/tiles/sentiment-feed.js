import { esc, fmtAgo } from "./tile-helpers.js";

export function renderTile(el, data, names) {
  const msgs = data.messages || [];
  const chNames = names ? names.channels || {} : {};
  const uNames = names ? names.users || {} : {};

  const rows = msgs.slice(0, 6).map(m => {
    const positive = m.sentiment >= 0.5;
    const color = positive ? "var(--green)" : "var(--red)";
    const score = (m.sentiment > 0 ? "+" : "") + m.sentiment.toFixed(2);
    const channel = chNames[m.channel_id] ? "#" + chNames[m.channel_id] : "";
    const author = uNames[m.author_id] || "";
    const snippet = m.content && m.content.length > 60
      ? m.content.slice(0, 60) + "\u2026"
      : (m.content || "");
    return `
      <div class="sf-row">
        <span class="sf-score" style="color:${color}">${score}</span>
        <span class="sf-body">
          <span class="sf-meta">${esc(author)}${channel ? " in " + esc(channel) : ""}</span>
          <span class="sf-text">${esc(snippet)}</span>
        </span>
        <span class="sf-time">${fmtAgo(m.ts)}</span>
      </div>
    `;
  }).join("");

  el.innerHTML = `
    <div class="health-tile-header">
      <span class="health-tile-label">Sentiment Feed</span>
    </div>
    <div class="health-tile-companions" style="margin-bottom:6px;">
      <span style="color:var(--green)">${data.positive_24h} positive (24h)</span>
      <span style="color:var(--red)">${data.negative_24h} negative (24h)</span>
    </div>
    ${rows || '<div class="home-dim">No extreme sentiment messages yet</div>'}
  `;
}
