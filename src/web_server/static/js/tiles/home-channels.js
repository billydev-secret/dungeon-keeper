import { esc } from "./tile-helpers.js";

export function renderTile(el, d) {
  const html = d.top_channels.length
    ? d.top_channels.map((c, i) => `
        <div class="home-rank-row">
          <span class="home-rank-pos">${i + 1}</span>
          <span class="home-rank-name">#${esc(c.channel_name || c.channel_id)}</span>
          <span class="home-rank-val">${c.count}</span>
        </div>
      `).join("")
    : '<div class="home-dim">No messages this hour</div>';

  el.innerHTML = `
    <div class="home-card-label">Hottest Channels (1h)</div>
    ${html}
  `;
}
