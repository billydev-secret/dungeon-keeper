export function renderTile(el, d) {
  el.innerHTML = `
    <div class="home-card-label">Recent Joins</div>
    <div class="home-rank-row"><span class="home-rank-name">24h</span><span class="home-rank-val">${d.joins_1d}</span></div>
    <div class="home-rank-row"><span class="home-rank-name">7d</span><span class="home-rank-val">${d.joins_7d} <span style="font-size:11px;color:var(--ink-dim);">(${d.joins_avg_daily_7d}/day)</span></span></div>
    <div class="home-rank-row"><span class="home-rank-name">30d</span><span class="home-rank-val">${d.joins_30d} <span style="font-size:11px;color:var(--ink-dim);">(${d.joins_avg_daily_30d}/day)</span></span></div>
  `;
}
