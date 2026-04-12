import { badgeHTML } from "./tile-helpers.js";

export function renderTile(el, data) {
  const f = data.funnel || {};
  const stages = [
    { label: "Joined", count: f.joined },
    { label: "1st Msg", count: f.first_message },
    { label: "Got Reply", count: f.first_reply },
    { label: "3+ Ch", count: f.three_channels },
    { label: "D7 Return", count: f.d7_return },
  ];
  const max = Math.max(f.joined, 1);
  const funnelBars = stages.map(s => {
    const pct = Math.round((s.count / max) * 100);
    return `<div class="funnel-stage">
      <div class="funnel-bar" style="width:${pct}%">${s.count}</div>
      <span class="funnel-label">${s.label}</span>
    </div>`;
  }).join("");

  el.innerHTML = `
    <div class="health-tile-header">
      <span class="health-tile-label">Newcomer Funnel</span>
      ${badgeHTML(data.badge)}
    </div>
    <div class="health-tile-metric">${data.activation_rate}%</div>
    <div class="funnel-mini">${funnelBars}</div>
    <div class="health-tile-companions">
      <span>1st msg: ${data.time_to_first_msg}h</span>
      <span>Reply: ${data.first_response_latency}m</span>
    </div>
  `;
}
