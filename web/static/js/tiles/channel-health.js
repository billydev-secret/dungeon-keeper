import { badgeHTML, esc, miniBarHTML } from "./tile-helpers.js";

export function renderTile(el, data, names) {
  const bars = (data.top5 || []).map(ch => ({
    label: names.channels[ch.channel_id] ? "#" + names.channels[ch.channel_id] : "#" + ch.channel_id,
    value: Math.round(ch.score),
  }));
  const flagged = data.flagged_count
    ? `<span class="health-tile-badge" style="background:var(--warning)">${data.flagged_count} flagged</span>`
    : "";

  el.innerHTML = `
    <div class="health-tile-header">
      <span class="health-tile-label">Channel Health</span>
      ${flagged}
    </div>
    <div class="health-tile-metric">${data.active_count} <span class="health-tile-unit">active</span></div>
    ${miniBarHTML(bars, { maxVal: 100, color: "var(--success)" })}
    <div class="health-tile-companions">
      <span>${data.dormant_count} dormant</span>
      <span>${data.archive_count || 0} archive</span>
    </div>
  `;
}
