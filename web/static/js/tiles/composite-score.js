import { badgeHTML } from "./tile-helpers.js";

export function renderTile(el, data) {
  // Half-circle gauge
  const score = data.score || 0;
  const angle = (score / 100) * 180;
  const colors = { excellent: "#7F8F3A", healthy: "#7F8F3A", needs_work: "#E6B84C", critical: "#9E3B2E" };
  const gaugeColor = colors[data.badge] || "#949ba4";

  const gauge = `<svg viewBox="0 0 200 110" width="200" height="110" style="display:block;margin:0 auto;">
    <path d="M 10 100 A 90 90 0 0 1 190 100" fill="none" stroke="var(--bg)" stroke-width="14" stroke-linecap="round"/>
    <path d="M 10 100 A 90 90 0 0 1 190 100" fill="none" stroke="${gaugeColor}33" stroke-width="14" stroke-linecap="round"/>
    <path d="M 10 100 A 90 90 0 ${angle > 180 ? 1 : 0} 1 ${100 + 90 * Math.cos(Math.PI - angle * Math.PI / 180)} ${100 - 90 * Math.sin(Math.PI - angle * Math.PI / 180)}" fill="none" stroke="${gaugeColor}" stroke-width="14" stroke-linecap="round"/>
    <text x="100" y="90" text-anchor="middle" fill="var(--ink)" font-size="32" font-weight="bold">${score}</text>
    <text x="100" y="108" text-anchor="middle" fill="var(--ink-dim)" font-size="12">/100</text>
  </svg>`;

  // Dimension mini bars
  const dims = (data.dimensions || []).map(d => {
    const color = d.score >= 60 ? "var(--green)" : d.score >= 40 ? "var(--yellow)" : "var(--red)";
    return `<div class="health-dim-bar">
      <span class="health-dim-label">${d.name}</span>
      <div class="health-dim-track"><div class="health-dim-fill" style="width:${d.score}%;background:${color}"></div></div>
      <span class="health-dim-val">${d.score}</span>
    </div>`;
  }).join("");

  el.innerHTML = `
    <div class="health-tile-header">
      <span class="health-tile-label">Community Health</span>
      ${badgeHTML(data.badge)}
    </div>
    ${gauge}
    <div class="health-dim-bars">${dims}</div>
  `;
}
