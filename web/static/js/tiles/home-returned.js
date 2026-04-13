import { esc } from "./tile-helpers.js";

function fmtGap(hours) {
  if (hours < 24) return Math.round(hours) + "h";
  return Math.round(hours / 24) + "d";
}

export function renderTile(el, d) {
  const html = d.returned_users && d.returned_users.length
    ? d.returned_users.map((u) => `
        <div class="home-rank-row">
          <span class="home-rank-name">${esc(u.user_name || u.user_id)}</span>
          <span class="home-rank-val" style="color:#7F8F3A;">after ${fmtGap(u.gap_hours)}</span>
        </div>
      `).join("")
    : '<div class="home-dim">No returning users right now</div>';

  el.innerHTML = `
    <div class="home-card-label">Returned After Break</div>
    ${html}
  `;
}
