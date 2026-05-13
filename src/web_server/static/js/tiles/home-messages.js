import { sparklineSVG } from "./tile-helpers.js";

export function renderTile(el, d) {
  el.innerHTML = `
    <div class="home-card-label">Messages (24h)</div>
    <div class="home-card-big">${d.msgs_24h.toLocaleString()}</div>
    <div class="home-sparkline">${sparklineSVG(d.msg_sparkline)}</div>
    <div class="home-card-sub">${d.msgs_1h} in the last hour &middot; ${d.unique_today} unique users today</div>
  `;
}
