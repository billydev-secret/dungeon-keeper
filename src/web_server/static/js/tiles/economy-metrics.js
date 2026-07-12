import { miniBarHTML, fmtNum } from "./tile-helpers.js";

// Faucet group → short display label, in the metrics module's stable order.
const FAUCET_LABELS = {
  logins: "Logins",
  activity: "Activity",
  quests: "Quests",
  games: "Games",
  grants: "Grants",
};

function parseFaucet(raw) {
  if (!raw) return {};
  try {
    return JSON.parse(raw) || {};
  } catch (_) {
    return {};
  }
}

export function renderTile(el, data) {
  const weeks = (data && data.weeks) || [];
  if (!weeks.length) {
    el.innerHTML = `
      <div class="health-tile-header">
        <span class="health-tile-label">Economy</span>
      </div>
      <div class="home-dim">No data yet — first weekly rollup pending.</div>
    `;
    return;
  }

  const cur = weeks[0];
  const prev = weeks.length >= 2 ? weeks[1] : null;

  const median = Math.round(cur.median_income || 0);
  const p90 = Math.round(cur.p90_income || 0);
  const minted = cur.minted || 0;
  const burned = cur.burned || 0;

  // Week-over-week net-mint (minted − burned) direction; only with ≥ 2 weeks.
  let wowArrow = "";
  if (prev) {
    const curNet = minted - burned;
    const prevNet = (prev.minted || 0) - (prev.burned || 0);
    const t = `title="net mint vs last week"`;
    if (curNet > prevNet) wowArrow = `<span ${t} style="color:var(--green)">▲</span>`;
    else if (curNet < prevNet) wowArrow = `<span ${t} style="color:var(--red)">▼</span>`;
    else wowArrow = `<span ${t} class="home-dim">▬</span>`;
  }

  // Faucet-mix mini bar (shares are 0-1 fractions; "{}" → no bars).
  const shares = parseFaucet(cur.faucet_mix);
  const barItems = Object.keys(FAUCET_LABELS)
    .map((k) => ({ label: FAUCET_LABELS[k], value: Math.round((shares[k] || 0) * 100) }))
    .filter((i) => i.value > 0);
  const faucetBar = barItems.length
    ? miniBarHTML(barItems, { maxVal: 100 })
    : `<div class="home-dim">No mint mix</div>`;

  // Rental holders as a share of active members (guard divide-by-zero).
  const active = cur.active_members || 0;
  const holders = cur.rental_holders || 0;
  const holderPct = active > 0 ? Math.round((holders / active) * 100) : 0;

  el.innerHTML = `
    <div class="health-tile-header">
      <span class="health-tile-label">Economy</span>
    </div>
    <div class="health-tile-metric">${fmtNum(median)} <span class="health-tile-unit">coins median</span></div>
    <div class="health-tile-companions">
      <span>p90 <b>${fmtNum(p90)}</b></span>
      <span>minted <b>${fmtNum(minted)}</b> / burned <b>${fmtNum(burned)}</b> ${wowArrow}</span>
    </div>
    ${faucetBar}
    <div class="health-tile-companions">
      <span>rental holders <b>${holderPct}%</b></span>
      <span>7+ day streaks <b>${fmtNum(cur.streaks_7plus || 0)}</b></span>
    </div>
  `;
}
