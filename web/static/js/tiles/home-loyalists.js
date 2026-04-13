import { esc } from "./tile-helpers.js";

export function renderTile(el, d) {
  const html = d.channel_loyalists && d.channel_loyalists.length
    ? d.channel_loyalists.map((u) => `
        <div class="home-rank-row">
          <span class="home-rank-name">${esc(u.user_name || u.user_id)}</span>
          <span class="home-rank-val">#${esc(u.channel_name || u.channel_id)} <span style="color:var(--text-dim);font-size:11px;">${u.pct}%</span></span>
        </div>
      `).join("")
    : '<div class="home-dim">No loyalists today (need 10+ msgs, 80%+ in one channel)</div>';

  el.innerHTML = `
    <div class="home-card-label">Channel Loyalists (24h)</div>
    ${html}
  `;
}
