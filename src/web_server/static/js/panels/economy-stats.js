// Economy — Statistics. Tuning-grade visibility into who holds what and
// how fast currency flows. Read-only; gated by the economy manager role (or
// admin), same as the Operations page. Everything is a single GET; the
// Refresh button re-fetches.
import { api, esc, fmtAge } from "../api.js";
import { loadMembers } from "../config-helpers.js";

const PERK_LABELS = {
  price_role_color: "Role color",
  price_role_name: "Role name",
  price_role_icon: "Role icon",
  price_role_gradient: "Role gradient",
  price_gift_color: "Gift color",
  price_text_room: "Text room",
  price_voice_room: "Voice room",
};

const FAUCET_LABELS = {
  logins: "Logins",
  activity: "Activity",
  quests: "Quests",
  games: "Games",
  grants: "Grants",
};

// Member-table columns: [key, label, numeric?]. `name` sorts by resolved name.
const MEMBER_COLS = [
  ["name", "Member", false],
  ["balance", "Balance", true],
  ["income_7d", "7d income", true],
  ["coins_per_day_7d", "Coins/day", true],
  ["income_30d", "30d income", true],
  ["spent_7d", "Spent 7d", true],
  ["top_faucet", "Top faucet", false],
  ["rentals_live", "Rentals", true],
  ["streak", "Streak", true],
];

function nowSec() { return Date.now() / 1000; }

function memberName(members, id) {
  const m = members.find((x) => String(x.id) === String(id));
  return m ? (m.display_name || m.name) : String(id);
}

function fmtNum(n) {
  return Number(n || 0).toLocaleString();
}

function fmtPct(frac) {
  return `${(Number(frac || 0) * 100).toFixed(1)}%`;
}

// Panel-local sort state for the member table.
const sortState = { key: "balance", dir: "desc" };

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading Statistics…</div></div>`;
  (async () => {
    const members = await loadMembers().catch(() => []);
    render(container, members);
  })();
  return null;
}

function render(container, members) {
  container.innerHTML = `
    <div class="panel">
      <header style="display:flex; align-items:flex-start; justify-content:space-between; gap:12px;">
        <div>
          <h2>Statistics</h2>
          <div class="subtitle">Who holds what, and how fast currency flows</div>
        </div>
        <button class="btn" data-refresh>Refresh</button>
      </header>

      <div class="card-grid" data-summary style="margin-bottom:4px;"></div>

      <section class="card">
        <div class="section-label">Balance distribution</div>
        <div data-distribution><div class="empty">Loading…</div></div>
      </section>

      <div class="card-grid" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr));">
        <section class="card"><div class="section-label">Engagement</div>
          <div data-engagement><div class="empty">Loading…</div></div></section>
        <section class="card"><div class="section-label">Affordability</div>
          <div class="field-hint">Solid color ≈ how many days of median daily income each perk costs.</div>
          <div data-affordability><div class="empty">Loading…</div></div></section>
      </div>

      <section class="card">
        <div class="section-label">Top transfers (30d)</div>
        <div class="field-hint">One-way volume over 500 is flagged — a possible farming/laundering signal worth an audit.</div>
        <div data-transfers><div class="empty">Loading…</div></div>
      </section>

      <section class="card">
        <div class="section-label">Members</div>
        <div data-members><div class="empty">Loading…</div></div>
      </section>
    </div>`;

  container.querySelector("[data-refresh]").addEventListener("click", () => {
    refresh(container, members);
  });
  refresh(container, members);
}

async function refresh(container, members) {
  let data;
  try {
    data = await api("/api/economy/stats", { limit: 100 });
  } catch (err) {
    container.querySelector("[data-summary]").innerHTML =
      `<div class="error">${esc(err.message)}</div>`;
    return;
  }
  renderSummary(container, data);
  renderDistribution(container, data.distribution);
  renderEngagement(container, data.engagement);
  renderAffordability(container, data.affordability);
  renderTransfers(container, data.transfers_top, members);
  renderMembers(container, data.members, members);
}

// ── summary ──────────────────────────────────────────────────────────

function renderSummary(container, data) {
  const s = data.supply;
  const flow = data.flow_7d;
  const cards = [
    { label: "Total supply", value: fmtNum(s.total) },
    { label: "Holders", value: fmtNum(s.holders) },
    { label: "Median balance", value: fmtNum(s.median_balance) },
    { label: "Top 10% share", value: fmtPct(s.top10_share), cls: "stat-info" },
    { label: "Gini", value: Number(s.gini || 0).toFixed(3), cls: "stat-info" },
    { label: "Burn rate 7d", value: fmtPct(flow.burn_rate), cls: "stat-warning" },
  ];
  container.querySelector("[data-summary]").innerHTML = cards.map((c) => `
    <div class="stat ${c.cls || ""}">
      <div class="stat-label">${c.label}</div>
      <div class="stat-value">${c.value}</div>
    </div>`).join("");
}

// ── distribution bar chart (pure DOM/CSS) ────────────────────────────

function bucketLabel(b) {
  if (b.hi === null || b.hi === undefined) return `${b.lo}+`;
  if (b.lo === b.hi) return `${b.lo}`;
  return `${b.lo}–${b.hi}`;
}

function renderDistribution(container, dist) {
  const host = container.querySelector("[data-distribution]");
  const buckets = dist || [];
  const max = Math.max(1, ...buckets.map((b) => b.count));
  if (!buckets.some((b) => b.count > 0)) {
    host.innerHTML = `<div class="empty">No holders yet.</div>`;
    return;
  }
  host.innerHTML = buckets.map((b) => {
    const pct = Math.round((b.count / max) * 100);
    return `
      <div style="display:flex; align-items:center; gap:10px; margin:4px 0;">
        <div style="width:72px; text-align:right; font-variant-numeric:tabular-nums; color:var(--ink-dim);">${esc(bucketLabel(b))}</div>
        <div style="flex:1; background:var(--rule-soft); border-radius:4px; height:16px; overflow:hidden;">
          <div style="width:${pct}%; height:100%; background:var(--plum,#9b6dd6);"></div>
        </div>
        <div style="width:44px; text-align:right; font-variant-numeric:tabular-nums;">${fmtNum(b.count)}</div>
      </div>`;
  }).join("");
}

// ── engagement ───────────────────────────────────────────────────────

function renderEngagement(container, eng) {
  const host = container.querySelector("[data-engagement]");
  const rows = [
    ["Active members (30d)", fmtNum(eng.active_members)],
    ["Earners (7d)", fmtNum(eng.earners_7d)],
    ["Earner ratio", fmtPct(eng.earner_ratio)],
    ["Spenders (7d)", fmtNum(eng.spenders_7d)],
    ["Quest claims (7d)", fmtNum(eng.quest_claims_7d)],
    [
      "Quest approval (30d)",
      eng.quest_approval_rate_30d == null ? "—" : fmtPct(eng.quest_approval_rate_30d),
    ],
    [
      "Hoard (weeks)",
      eng.hoard_weeks == null ? "—" : Number(eng.hoard_weeks).toFixed(1),
    ],
  ];
  host.innerHTML = `<table class="data-table"><tbody>${
    rows.map(([k, v]) =>
      `<tr><td>${esc(k)}</td><td class="num">${esc(String(v))}</td></tr>`).join("")
  }</tbody></table>`;
}

// ── affordability ────────────────────────────────────────────────────

function renderAffordability(container, aff) {
  const host = container.querySelector("[data-affordability]");
  const keys = Object.keys(aff || {});
  if (!keys.length) {
    host.innerHTML = `<div class="empty">No earners yet — nothing to price against.</div>`;
    return;
  }
  const max = Math.max(1, ...keys.map((k) => aff[k]));
  host.innerHTML = keys.map((k) => {
    const days = Number(aff[k]);
    const pct = Math.round((days / max) * 100);
    return `
      <div style="display:flex; align-items:center; gap:10px; margin:4px 0;">
        <div style="width:110px; color:var(--ink-dim);">${esc(PERK_LABELS[k] || k)}</div>
        <div style="flex:1; background:var(--rule-soft); border-radius:4px; height:14px; overflow:hidden;">
          <div style="width:${pct}%; height:100%; background:var(--gold-solid,#d9a441);"></div>
        </div>
        <div style="width:64px; text-align:right; font-variant-numeric:tabular-nums;">${days.toFixed(1)}d</div>
      </div>`;
  }).join("");
}

// ── top transfers ────────────────────────────────────────────────────

function renderTransfers(container, transfers, members) {
  const host = container.querySelector("[data-transfers]");
  const list = transfers || [];
  if (!list.length) {
    host.innerHTML = `<div class="empty">No transfers in the last 30 days.</div>`;
    return;
  }
  const rows = list.map((t) => {
    const flag = t.total > 500
      ? ` <span class="badge badge-warning" title="One-way volume over 500 — audit hint">flag</span>`
      : "";
    return `
      <tr>
        <td>${esc(memberName(members, t.from_id))}</td>
        <td>${esc(memberName(members, t.to_id))}</td>
        <td class="num">${fmtNum(t.total)}${flag}</td>
      </tr>`;
  }).join("");
  host.innerHTML = `
    <div style="overflow-x:auto;">
      <table class="data-table">
        <thead><tr><th>From</th><th>To</th><th class="num">Volume</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// ── member table (client-side sortable) ──────────────────────────────

function sortMembers(members, rows) {
  const { key, dir } = sortState;
  const mult = dir === "asc" ? 1 : -1;
  const sorted = rows.slice();
  sorted.sort((a, b) => {
    let av, bv;
    if (key === "name") {
      av = memberName(members, a.user_id).toLowerCase();
      bv = memberName(members, b.user_id).toLowerCase();
      return av < bv ? -mult : av > bv ? mult : 0;
    }
    if (key === "top_faucet") {
      av = a.top_faucet || "";
      bv = b.top_faucet || "";
      return av < bv ? -mult : av > bv ? mult : 0;
    }
    av = Number(a[key] || 0);
    bv = Number(b[key] || 0);
    return (av - bv) * mult;
  });
  return sorted;
}

function renderMembers(container, rows, members) {
  const host = container.querySelector("[data-members]");
  const list = rows || [];
  if (!list.length) {
    host.innerHTML = `<div class="empty">No holders yet.</div>`;
    return;
  }
  const sorted = sortMembers(members, list);
  const arrow = (k) =>
    sortState.key === k ? (sortState.dir === "asc" ? " ▲" : " ▼") : "";
  const head = MEMBER_COLS.map(([k, label, numeric]) =>
    `<th data-sort-col="${k}" style="cursor:pointer;${numeric ? "text-align:right;" : ""}">${esc(label)}${arrow(k)}</th>`
  ).join("");
  const body = sorted.map((m) => {
    const faucet = m.top_faucet
      ? (FAUCET_LABELS[m.top_faucet] || m.top_faucet)
      : "—";
    const earned = m.last_earned_at
      ? ` title="last earned ${esc(fmtAge(nowSec() - m.last_earned_at))} ago"`
      : "";
    return `
      <tr${earned}>
        <td>${esc(memberName(members, m.user_id))}</td>
        <td class="num">${fmtNum(m.balance)}</td>
        <td class="num">${fmtNum(m.income_7d)}</td>
        <td class="num">${Number(m.coins_per_day_7d || 0).toFixed(1)}</td>
        <td class="num">${fmtNum(m.income_30d)}</td>
        <td class="num">${fmtNum(m.spent_7d)}</td>
        <td>${esc(faucet)}</td>
        <td class="num">${fmtNum(m.rentals_live)}</td>
        <td class="num">${fmtNum(m.streak)}</td>
      </tr>`;
  }).join("");
  host.innerHTML = `
    <div style="overflow-x:auto;">
      <table class="data-table">
        <thead><tr>${head}</tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;

  host.querySelectorAll("[data-sort-col]").forEach((th) => {
    th.addEventListener("click", () => {
      const k = th.dataset.sortCol;
      if (sortState.key === k) {
        sortState.dir = sortState.dir === "asc" ? "desc" : "asc";
      } else {
        sortState.key = k;
        // Text columns default to ascending; numeric to descending.
        sortState.dir = (k === "name" || k === "top_faucet") ? "asc" : "desc";
      }
      renderMembers(container, list, members);
    });
  });
}
