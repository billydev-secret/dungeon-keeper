import { badgeHTML } from "./tile-helpers.js";

export function renderTile(el, data) {
  // Mini retention curve (simple SVG line)
  const points = [100, data.d7 || 0, data.d30 || 0];
  const w = 180, h = 32;
  const step = w / (points.length - 1);
  const pts = points.map((v, i) => `${i * step},${h - (v / 100) * (h - 4) - 2}`);
  const svg = `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" style="display:block;">
    <polyline points="${pts.join(" ")}" fill="none" stroke="#E6B84C" stroke-width="2" stroke-linejoin="round"/>
    ${pts.map((p, i) => `<circle cx="${i * step}" cy="${h - (points[i] / 100) * (h - 4) - 2}" r="3" fill="#E6B84C"/>`).join("")}
  </svg>`;

  el.innerHTML = `
    <div class="health-tile-header">
      <span class="health-tile-label">Cohort Retention</span>
      ${badgeHTML(data.badge)}
    </div>
    <div class="health-tile-metric">${data.d7}% <span class="health-tile-unit">D7</span></div>
    <div class="health-tile-sparkline">${svg}</div>
    <div class="health-tile-companions">
      <span>D30: <b>${data.d30}%</b></span>
      <span>Cohort: ${data.latest_cohort_size}</span>
    </div>
  `;
}
