import { esc } from "./tile-helpers.js";

export function renderTile(el, d) {
  const html = d.conversation_starters && d.conversation_starters.length
    ? d.conversation_starters.map((u) => `
        <div class="home-rank-row">
          <span class="home-rank-name">${esc(u.user_name || u.user_id)}</span>
          <span class="home-rank-val">${u.starts}</span>
        </div>
      `).join("")
    : '<div class="home-dim">Not enough data yet</div>';

  el.innerHTML = `
    <div class="home-card-label">Conversation Starters (24h)</div>
    ${html}
  `;
}
