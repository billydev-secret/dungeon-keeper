import { sparklineSVG, badgeHTML } from "./tile-helpers.js";

export function renderTile(el, data) {
  el.innerHTML = `
    <div class="health-tile-header">
      <span class="health-tile-label">Participation Gini</span>
      ${badgeHTML(data.badge)}
    </div>
    <div class="health-tile-metric">${data.gini}</div>
    <div class="health-tile-sparkline">${sparklineSVG(data.sparkline, { color: data.gini > 0.7 ? "var(--yellow)" : "var(--green-text)" })}</div>
    <div class="health-tile-companions">
      <span>Top 5% share: <b>${data.top5_share}%</b></span>
    </div>
  `;
}
