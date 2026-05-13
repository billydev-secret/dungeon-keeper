import { esc } from "./tile-helpers.js";

export function renderTile(el, d) {
  const html = d.social_butterflies && d.social_butterflies.length
    ? d.social_butterflies.map((u) => `
        <div class="home-rank-row">
          <span class="home-rank-name">${esc(u.user_name || u.user_id)}</span>
          <span class="home-rank-val">${u.unique} people</span>
        </div>
      `).join("")
    : '<div class="home-dim">Not enough data yet</div>';

  el.innerHTML = `
    <div class="home-card-label">Social Butterflies (24h)</div>
    ${html}
  `;
}
