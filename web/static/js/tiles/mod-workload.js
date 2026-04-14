import { badgeHTML, miniBarHTML } from "./tile-helpers.js";

export function renderTile(el, data, names) {
  const bars = (data.mod_actions || []).slice(0, 5).map(m => ({
    label: names.users[m.user_id] || m.user_id,
    value: m.count,
  }));

  el.innerHTML = `
    <div class="health-tile-header">
      <span class="health-tile-label">Mod Workload</span>
      ${badgeHTML(data.badge)}
    </div>
    <div class="health-tile-metric">${data.median_response_time}<span class="health-tile-unit">min</span></div>
    ${miniBarHTML(bars, { color: "var(--info)" })}
    <div class="health-tile-companions">
      <span>Gini: ${data.workload_gini || "—"}</span>
      <span>${data.total_actions_7d} actions (7d)</span>
    </div>
  `;
}
