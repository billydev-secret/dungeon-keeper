import { esc } from "./tile-helpers.js";

export function renderTile(el, d) {
  const html = d.top_users.length
    ? d.top_users.map((u, i) => `
        <div class="home-rank-row">
          <span class="home-rank-pos">${i + 1}</span>
          <span class="home-rank-name">${esc(u.user_name || u.user_id)}</span>
          <span class="home-rank-val">${u.count}</span>
        </div>
      `).join("")
    : '<div class="home-dim">No messages this hour</div>';

  el.innerHTML = `
    <div class="home-card-label">Most Active Users (1h)</div>
    ${html}
  `;
}
