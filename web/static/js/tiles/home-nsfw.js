import { sparklineSVG } from "./tile-helpers.js";

export function renderTile(el, d) {
  el.innerHTML = `
    <div class="home-card-label">NSFW (24h)</div>
    <div class="home-card-big">${d.nsfw_24h.toLocaleString()}</div>
    <div class="home-sparkline">${sparklineSVG(d.nsfw_sparkline, { color: "#9E3B2E" })}</div>
    <div class="home-card-sub">${d.nsfw_1h} in the last hour &middot; ${d.nsfw_unique} unique users today</div>
  `;
}
