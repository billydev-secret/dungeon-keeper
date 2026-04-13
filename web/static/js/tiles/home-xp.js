export function renderTile(el, d) {
  el.innerHTML = `
    <div class="home-card-label">XP Today</div>
    <div class="home-card-big">${d.xp_today.toLocaleString()}</div>
    <div class="home-card-sub">${d.xp_users_today} users earned XP</div>
  `;
}
