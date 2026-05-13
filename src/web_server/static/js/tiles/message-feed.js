import { esc, fmtAgo } from "./tile-helpers.js";

export function renderTile(el, data, names) {
  const msgs = data.messages || [];
  const chNames = names ? names.channels || {} : {};
  const uNames = names ? names.users || {} : {};

  const rows = msgs.slice(0, 6).map(m => {
    const eng = m.engagement || 0;
    const engColor = eng >= 5 ? "var(--green)" : eng >= 2 ? "var(--yellow)" : "var(--ink-dim)";
    const sentColor = m.sentiment >= 0.3 ? "var(--green)" : m.sentiment <= -0.3 ? "var(--red)" : "var(--ink-dim)";
    const channel = chNames[m.channel_id] ? "#" + chNames[m.channel_id] : "";
    const author = uNames[m.author_id] || "";
    const snippet = m.content && m.content.length > 60
      ? m.content.slice(0, 60) + "\u2026"
      : (m.content || "");
    return `
      <div class="sf-row">
        <span class="sf-score" style="color:${engColor}" title="Engagement: ${eng}">${eng}</span>
        <span style="color:${sentColor};font-size:16px;line-height:1;" title="Sentiment: ${(m.sentiment || 0).toFixed(2)}">\u25CF</span>
        <span class="sf-body">
          <span class="sf-meta">${esc(author)}${channel ? " in " + esc(channel) : ""}${m.is_reply ? " \u21A9" : ""}</span>
          <span class="sf-text">${esc(snippet)}</span>
        </span>
        <span class="sf-time">${fmtAgo(m.ts)}</span>
      </div>
    `;
  }).join("");

  el.innerHTML = `
    <div class="health-tile-header">
      <span class="health-tile-label">Message Feed</span>
    </div>
    <div class="health-tile-companions" style="margin-bottom:6px;">
      <span>${(data.total_24h || 0).toLocaleString()} messages (24h)</span>
      <span style="color:var(--green)">${data.high_engagement_24h || 0} high engagement</span>
    </div>
    ${rows || '<div class="home-dim">No messages yet</div>'}
  `;
}
