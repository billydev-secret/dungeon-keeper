import { badgeHTML, fmtNum } from "./tile-helpers.js";

export function renderTile(el, data) {
  el.innerHTML = `
    <div class="health-tile-header">
      <span class="health-tile-label">Churn Risk</span>
      ${badgeHTML(data.badge)}
    </div>
    <div class="health-tile-metric">${data.at_risk_count} <span class="health-tile-unit">at risk</span></div>
    <div class="health-tile-companions">
      <span class="risk-critical">${data.critical} critical</span>
      <span class="risk-declining">${data.declining} declining</span>
      <span class="risk-watch">${data.watch} watch</span>
    </div>
  `;
}
