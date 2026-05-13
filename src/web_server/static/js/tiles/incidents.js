import { badgeHTML } from "./tile-helpers.js";

export function renderTile(el, data) {
  // 7-day timeline dots
  const days = ["6d", "5d", "4d", "3d", "2d", "1d", "now"];
  const maxDot = Math.max(...(data.timeline || []), 1);
  const dots = (data.timeline || []).map((cnt, i) => {
    const size = cnt ? Math.max(6, Math.round((cnt / maxDot) * 16)) : 4;
    const color = cnt ? "var(--red)" : "var(--ink-dim)";
    return `<div class="incident-dot" style="width:${size}px;height:${size}px;background:${color}" title="${days[i]}: ${cnt}"></div>`;
  }).join("");

  // Category indicators
  const cats = data.categories || {};
  const catHTML = ["velocity_spike", "report_cluster", "raid_attempt", "sentiment_storm"]
    .map(c => {
      const active = (cats[c] || 0) > 0;
      return `<span class="incident-cat ${active ? "incident-cat-active" : ""}">${c.replace(/_/g, " ")}</span>`;
    }).join("");

  el.innerHTML = `
    <div class="health-tile-header">
      <span class="health-tile-label">Incidents</span>
      ${badgeHTML(data.badge)}
    </div>
    <div class="health-tile-metric">${data.active_count}</div>
    <div class="incident-timeline">${dots}</div>
    <div class="incident-categories">${catHTML}</div>
  `;
}
