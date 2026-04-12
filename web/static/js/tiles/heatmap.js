import { badgeHTML } from "./tile-helpers.js";

export function renderTile(el, data) {
  // Mini 7x24 heatmap
  const maxVal = Math.max(...data.grid.flat(), 1);
  const days = ["M", "T", "W", "T", "F", "S", "S"];
  let cells = "";
  for (let d = 0; d < 7; d++) {
    for (let h = 0; h < 24; h++) {
      const v = data.grid[d][h];
      const intensity = Math.round((v / maxVal) * 255);
      const bg = `rgba(230,184,76,${(intensity / 255).toFixed(2)})`;
      cells += `<div class="heatmap-cell" style="background:${bg}" title="${days[d]} ${h}:00 — ${v} msgs/hr"></div>`;
    }
  }

  el.innerHTML = `
    <div class="health-tile-header">
      <span class="health-tile-label">Activity Heatmap</span>
    </div>
    <div class="health-tile-metric" style="font-size:16px;">${data.peak_slot}</div>
    <div class="heatmap-mini-grid">${cells}</div>
    <div class="health-tile-companions">
      <span>Quiet: ${data.quiet_slot}</span>
      <span>${data.dead_hours} dead hrs/wk</span>
    </div>
  `;
}
