import { badgeHTML } from "./tile-helpers.js";

export function renderTile(el, data) {
  el.innerHTML = `
    <div class="health-tile-header">
      <span class="health-tile-label">Social Graph</span>
      ${badgeHTML(data.badge)}
    </div>
    <div class="health-tile-metric">${data.clustering_coefficient}</div>
    <div class="health-tile-sub">clustering coefficient</div>
    <div class="health-tile-companions">
      <span>Density: ${data.network_density}</span>
      <span>${data.bridge_count} bridges</span>
      <span>${data.isolates} isolates</span>
    </div>
  `;
}
