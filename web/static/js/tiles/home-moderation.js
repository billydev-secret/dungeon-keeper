export function renderTile(el, d) {
  el.innerHTML = `
    <div class="home-card-label">Moderation</div>
    <div class="home-mod-stats">
      <span class="home-mod-pill home-mod-danger">${d.active_jails} jailed</span>
      <span class="home-mod-pill home-mod-info">${d.open_tickets} tickets</span>
      <span class="home-mod-pill home-mod-warn">${d.active_warnings} warnings</span>
    </div>
  `;
}
