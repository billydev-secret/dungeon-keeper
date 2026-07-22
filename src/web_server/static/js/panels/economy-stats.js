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
  price_role_holographic: "Role holographic",
  price_streak_shield: "Streak shield",
  price_voice_style: "Voice style",
  price_text_room: "Text room",
  price_voice_room: "Voice room",
  price_quest_reroll: "Quest reroll",
};

const FAUCET_LABELS = {
  logins: "Logins",
  activity: "Activity",
  quests: "Quests",
  games: "Games",
  grants: "Grants",
};

// Per-source colors for the income-mix stacked bars. All defined dashboard
// palette vars (distinct hues); grants sits in a muted grey to read as
// staff-injected rather than player-earned.
const FAUCET_COLORS = {
  logins: "var(--blurple, #5865f2)",
  activity: "var(--plum, #c07aa1)",
  quests: "var(--gold-solid, #e6b84c)",
  games: "var(--green, #23a55a)",
  grants: "var(--ink-mute, #979ba3)",
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

// "Happening now" auto-refresh cadence. The endpoint is a handful of cheap
// aggregates; 45s keeps the pulse feeling live without hammering it.
const LIVE_REFRESH_MS = 45000;

function fmtDur(secs) {
  const s = Math.max(0, Math.round(Number(secs) || 0));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
    m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading Statistics…</div></div>`;
  let liveTimer = null;
  (async () => {
    const members = await loadMembers().catch(() => []);
    render(container, members);
    refreshLive(container);
    liveTimer = setInterval(() => refreshLive(container), LIVE_REFRESH_MS);
  })();
  return { unmount() { if (liveTimer) clearInterval(liveTimer); } };
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
        <div class="section-label">Balance Distribution</div>
        <div data-distribution><div class="empty">Loading…</div></div>
      </section>

      <section class="card">
        <div class="section-label">Income Sources</div>
        <div class="field-hint">Coins minted per week by source (grants, quests,
          logins, activity, games) over the last 8 weeks. Transfers move currency
          sideways, so they aren't income and don't appear here.</div>
        <div data-income-sources><div class="empty">Loading…</div></div>
      </section>

      <div class="card-grid" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr));">
        <section class="card"><div class="section-label">Engagement</div>
          <div data-engagement><div class="empty">Loading…</div></div></section>
        <section class="card"><div class="section-label">Affordability</div>
          <div class="field-hint">Solid color ≈ how many days of median daily income each perk costs.</div>
          <div data-affordability><div class="empty">Loading…</div></div></section>
      </div>

      <section class="card">
        <div class="section-label">Biggest Spenders (all time)</div>
        <div class="field-hint">Lifetime currency burned — rentals, consumables and other sinks. Transfers and staff clawbacks don't count: a transfer moves currency sideways rather than removing it.</div>
        <div data-burn><div class="empty">Loading…</div></div>
      </section>

      <section class="card">
        <div class="section-label">Top Transfers (30d)</div>
        <div class="field-hint">One-way volume over 500 is flagged — a possible farming/laundering signal worth an audit.</div>
        <div data-transfers><div class="empty">Loading…</div></div>
      </section>

      <section class="card" data-live-card>
        <div class="section-label">Happening Now</div>
        <div class="field-hint">The quest pulse — anonymous counts only, auto-refreshes every 45s.</div>
        <div data-live><div class="empty">Loading…</div></div>
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

async function refreshLive(container) {
  const host = container.querySelector("[data-live]");
  if (!host) return;
  let live;
  try {
    live = await api("/api/economy/quests/live");
  } catch (err) {
    host.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
    return;
  }
  const bits = [];

  // Community hero (or the gap-week note).
  if (live.community.length) {
    for (const c of live.community) {
      const pace = c.completed
        ? "🎉 target cleared"
        : c.on_track ? "✅ on track" : "🔥 needs a push";
      const tiers = [40, 70, 100].map((pct, i) =>
        `<span style="opacity:${c.tiers_crossed > i ? 1 : 0.35};">🏁${pct}%</span>`,
      ).join(" ");
      bits.push(`
        <div style="margin:8px 0;">
          <strong>${esc(c.title)}</strong>
          <span class="field-hint" style="margin-left:8px;">${esc(c.kind_label)}</span>
          <div style="background:var(--border); border-radius:6px; height:14px; margin:6px 0; overflow:hidden; max-width:480px;">
            <div style="width:${Math.min(100, c.pct)}%; height:100%; background:var(--accent, #7aa2f7);"></div>
          </div>
          <div class="field-hint">
            ${fmtNum(c.current)} / ${fmtNum(c.target)} (${c.pct}%) · ${tiers} ·
            ${pace} · ${fmtNum(c.contributors)} contributor(s) ·
            ends in ${fmtDur(live.seconds_to_week_roll)}
          </div>
        </div>`);
    }
  } else {
    bits.push(`<div class="field-hint" style="margin:8px 0;">No community
      weekly running — gap week. The next one kicks off at the week roll
      (${fmtDur(live.seconds_to_week_roll)}) if the library has one.</div>`);
  }

  if (live.spotlight_label) {
    bits.push(`<div class="field-hint" style="margin:4px 0;">⚡ <strong>Spotlight:</strong>
      ${esc(live.spotlight_label)} pays double this week.</div>`);
  }

  // Ticker aggregates + countdowns.
  bits.push(`
    <div class="card-grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); margin:8px 0;">
      <div class="stat-card"><div class="stat-value">${fmtNum(live.completions_today)}</div><div class="stat-label">quests done today</div></div>
      <div class="stat-card"><div class="stat-value">${fmtNum(live.completions_week)}</div><div class="stat-label">this week</div></div>
      <div class="stat-card"><div class="stat-value">${fmtDur(live.seconds_to_day_roll)}</div><div class="stat-label">to daily reset</div></div>
      <div class="stat-card"><div class="stat-value">${fmtDur(live.seconds_to_week_roll)}</div><div class="stat-label">to weekly reset</div></div>
    </div>`);

  // Per-cadence pulse tables.
  const cadCols = ["daily", "weekly", "monthly"].map((cad) => {
    const rows = (live.cadences[cad] || []).map((q) => {
      const flight = q.in_progress ? ` · ${fmtNum(q.in_progress)} in progress` : "";
      return `<div class="field-hint" style="margin:2px 0;">${esc(q.title)} —
        <strong>${fmtNum(q.completed)}</strong> done${flight}</div>`;
    }).join("") || `<div class="empty">none active</div>`;
    return `<div><div class="section-label" style="text-transform:capitalize;">${cad} (this period)</div>${rows}</div>`;
  }).join("");
  const eventRows = (live.events || []).map((q) =>
    `<div class="field-hint" style="margin:2px 0;">${esc(q.title)} —
      <strong>${fmtNum(q.paid_7d)}</strong> this week · ${fmtNum(q.paid_total)} ever</div>`,
  ).join("") || `<div class="empty">none active</div>`;
  bits.push(`
    <div class="card-grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));">
      ${cadCols}
      <div><div class="section-label">Event Quests</div>${eventRows}</div>
    </div>`);

  host.innerHTML = bits.join("");
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
  renderIncomeSources(container, data.income_sources);
  renderEngagement(container, data.engagement);
  renderAffordability(container, data.affordability);
  renderBurn(container, data.burn_top, members);
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

// ── income sources (stacked bars, pure DOM/CSS) ──────────────────────

function weekLabel(startSec) {
  // Short "Jul 14" tick from the bucket start epoch (seconds).
  return new Date(startSec * 1000).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

function renderIncomeSources(container, income) {
  const host = container.querySelector("[data-income-sources]");
  if (!host) return;
  const groups = income?.groups || [];
  const buckets = income?.buckets || [];
  const max = Math.max(1, ...buckets.map((b) => b.total || 0));
  if (!buckets.some((b) => (b.total || 0) > 0)) {
    host.innerHTML = `<div class="empty">No income minted in the last 8 weeks.</div>`;
    return;
  }

  // Legend: one swatch per source, only for sources that actually paid.
  const active = groups.filter((g) => buckets.some((b) => (b.totals[g] || 0) > 0));
  const legend = active.map((g) => `
    <span style="display:inline-flex; align-items:center; gap:5px; margin-right:12px;">
      <span style="width:11px; height:11px; border-radius:2px; background:${FAUCET_COLORS[g] || "var(--ink-dim)"};"></span>
      <span class="field-hint">${esc(FAUCET_LABELS[g] || g)}</span>
    </span>`).join("");

  // Each bar is a fixed-height column; segments are height-proportional to the
  // week's total, and the column's overall height is scaled to the busiest week.
  const bars = buckets.map((b) => {
    const total = b.total || 0;
    const colH = Math.round((total / max) * 100); // % of the 120px track
    const segs = groups.map((g) => {
      const v = b.totals[g] || 0;
      if (v <= 0) return "";
      const h = (v / total) * 100;
      return `<div title="${esc(FAUCET_LABELS[g] || g)}: ${fmtNum(v)}"
        style="height:${h}%; background:${FAUCET_COLORS[g] || "var(--ink-dim)"};"></div>`;
    }).join("");
    return `
      <div style="flex:1; min-width:22px; display:flex; flex-direction:column; align-items:center; gap:4px;">
        <div style="width:100%; height:120px; display:flex; align-items:flex-end;">
          <div title="${esc(weekLabel(b.start))}: ${fmtNum(total)} total"
               style="width:100%; height:${colH}%; display:flex; flex-direction:column-reverse;
                      border-radius:3px 3px 0 0; overflow:hidden; background:var(--rule-soft);">
            ${segs}
          </div>
        </div>
        <div class="field-hint" style="font-variant-numeric:tabular-nums; white-space:nowrap;">${esc(weekLabel(b.start))}</div>
      </div>`;
  }).join("");

  host.innerHTML = `
    <div style="margin-bottom:10px;">${legend}</div>
    <div style="display:flex; align-items:flex-end; gap:6px; overflow-x:auto; padding-bottom:2px;">
      ${bars}
    </div>`;
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

// Ledger kinds that can show up as a member's top sink. Unknown kinds fall
// back to the raw kind rather than being hidden — a new sink should look
// unpolished here, not invisible.
const SINK_LABELS = {
  rental: "Perk rentals",
  quest_reroll: "Quest rerolls",
};

function sinkLabel(kind) {
  if (!kind) return "—";
  return SINK_LABELS[kind] || kind;
}

function renderBurn(container, burn, members) {
  const host = container.querySelector("[data-burn]");
  const list = burn || [];
  if (!list.length) {
    host.innerHTML = `<div class="empty">Nobody has spent anything yet.</div>`;
    return;
  }
  const rows = list.map((b, i) => `
      <tr>
        <td class="num">${i + 1}</td>
        <td>${esc(memberName(members, b.user_id))}</td>
        <td class="num">${fmtNum(b.burned)}</td>
        <td class="num">${fmtPct(b.share)}</td>
        <td>${esc(sinkLabel(b.top_sink))}</td>
      </tr>`).join("");
  host.innerHTML = `
    <div style="overflow-x:auto;">
      <table class="data-table">
        <thead><tr>
          <th class="num">#</th><th>Member</th><th class="num">Burned</th>
          <th class="num">Share</th><th>Mostly on</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

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
