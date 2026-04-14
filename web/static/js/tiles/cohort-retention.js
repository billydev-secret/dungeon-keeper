import { badgeHTML } from "./tile-helpers.js";

export function renderTile(el, data) {
  const fmt = v => (v === null || v === undefined) ? "—" : `${v}%`;
  // Mini retention curve — skip checkpoints still in the future
  const raw = [{ v: 100 }, { v: data.d7 }, { v: data.d30 }];
  const w = 180, h = 32;
  const step = w / (raw.length - 1);
  const plotted = raw
    .map((p, i) => ({ ...p, x: i * step }))
    .filter(p => p.v !== null && p.v !== undefined);
  const pts = plotted.map(p => `${p.x},${h - (p.v / 100) * (h - 4) - 2}`);
  const svg = plotted.length >= 2 ? `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" style="display:block;">
    <polyline points="${pts.join(" ")}" fill="none" stroke="#E6B84C" stroke-width="2" stroke-linejoin="round"/>
    ${plotted.map(p => `<circle cx="${p.x}" cy="${h - (p.v / 100) * (h - 4) - 2}" r="3" fill="#E6B84C"/>`).join("")}
  </svg>` : "";

  el.innerHTML = `
    <div class="health-tile-header">
      <span class="health-tile-label">Cohort Retention</span>
      ${badgeHTML(data.badge)}
    </div>
    <div class="health-tile-metric">${fmt(data.d7)} <span class="health-tile-unit">D7</span></div>
    <div class="health-tile-sparkline">${svg}</div>
    <div class="health-tile-companions">
      <span>D30: <b>${fmt(data.d30)}</b></span>
      <span>Cohort: ${data.latest_cohort_size}</span>
    </div>
  `;
}
