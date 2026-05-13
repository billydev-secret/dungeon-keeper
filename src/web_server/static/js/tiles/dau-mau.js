import { sparklineSVG, badgeHTML } from "./tile-helpers.js";

export function renderTile(el, data) {
  el.innerHTML = `
    <div class="health-tile-header">
      <span class="health-tile-label">DAU / MAU Stickiness</span>
      ${badgeHTML(data.badge)}
    </div>
    <div class="health-tile-metric">${data.dau_mau}%</div>
    <div class="health-tile-sparkline">${sparklineSVG(data.sparkline)}</div>
    <div class="health-tile-companions">
      <span>WAU/MAU <b>${data.wau_mau}%</b></span>
      <span>${data.dau} DAU of ${data.mau} MAU</span>
    </div>
  `;
}
