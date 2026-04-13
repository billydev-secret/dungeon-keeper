import { presenceBar } from "./tile-helpers.js";

export function renderTile(el, d) {
  el.innerHTML = `
    <div class="home-card-label">Presence</div>
    ${presenceBar(d.presence)}
  `;
}
