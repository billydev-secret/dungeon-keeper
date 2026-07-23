// Economy — Statistics. Tuning-grade visibility into who holds what and
// how fast currency flows. Read-only; gated by the economy manager role (or
// admin), same as the Operations page. Everything is a single GET; the
// Refresh button re-fetches.
import { api, esc, fmtAge } from "../api.js";
import { loadMembers } from "../config-helpers.js";
import { renderEmpty, renderError, renderLoading } from "../states.js";
import { ROLE_COLORS, CHART_ACCENT, CHART_BAR } from "../charts.js";

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

// Per-source colors for the income-mix stacked bars, taken from the shared
// chart palette (charts.js) so this page matches every other report; grants
// sits in the muted slot to read as staff-injected rather than player-earned.
const FAUCET_COLORS = {
  logins: ROLE_COLORS[2],
  activity: ROLE_COLORS[1],
  quests: ROLE_COLORS[0],
  games: ROLE_COLORS[4],
  grants: ROLE_COLORS[5],
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
  container.innerHTML = `<div class="panel">${renderLoading("Loading statistics…")}</div>`;
  let liveTimer = null;
  (async () => {
    // The member list, the stats blob, and the live pulse don't depend on each
    // other — fetch them together rather than in a waterfall (W-D11).
    const [members, stats] = await Promise.all([
      loadMembers().catch(() => []),
      api("/api/economy/stats", { limit: 100 }).then(
        (d) => ({ ok: true, data: d }),
        (err) => ({ ok: false, err }),
      ),
    ]);
    render(container, members);
    applyStats(container, members, stats);
    refreshLive(container);
    liveTimer = setInterval(() => refreshLive(container), LIVE_REFRESH_MS);
  })();
  return { unmount() { if (liveTimer) clearInterval(liveTimer); } };
}

// Every section that the stats fetch fills. Kept in one place so a failure can
// clear all of them instead of leaving six panels stuck on "Loading…" (W-D7).
const STATS_SECTIONS = [
  "[data-distribution]", "[data-income-sources]", "[data-engagement]",
  "[data-affordability]", "[data-burn]", "[data-transfers]", "[data-members]",
];

function applyStats(container, members, stats) {
  if (!stats.ok) {
    container.querySelector("[data-summary]").innerHTML = renderError(
      `Couldn't load economy statistics — ${stats.err.message}. Press Refresh to try again.`
    );
    for (const sel of STATS_SECTIONS) {
      const el = container.querySelector(sel);
      if (el) el.innerHTML = renderEmpty("Not loaded — the statistics request failed.");
    }
    return;
  }
  const data = stats.data;
  renderSummary(container, data);
  renderDistribution(container, data.distribution);
  renderIncomeSources(container, data.income_sources);
  renderEngagement(container, data.engagement);
  renderAffordability(container, data.affordability);
  renderBurn(container, data.burn_top, members);
  renderTransfers(container, data.transfers_top, members);
  renderMembers(container, data.members, members);
}

function render(container, members) {
  container.innerHTML = `
    <div class="panel">
      <header style="display:flex; align-items:flex-start; justify-content:space-between; gap:12px;">
        <div>
          <h2>Statistics</h2>
          <div class="subtitle">Who holds what, and how fast currency flows &middot; members active in the last 30 days</div>
        </div>
        <button class="btn" data-refresh>Refresh</button>
      </header>

      <div class="card-grid" data-summary style="margin-bottom:4px;"></div>

      <section class="card">
        <div class="section-label">Balance Distribution</div>
        <div data-distribution>${renderLoading("Loading…")}</div>
      </section>

      <section class="card">
        <div class="section-label">Income Sources</div>
        <div class="field-hint">Coins minted per week by source (grants, quests,
          logins, activity, games) over the last 8 weeks. Transfers move currency
          sideways, so they aren't income and don't appear here.</div>
        <div data-income-sources>${renderLoading("Loading…")}</div>
      </section>

      <div class="card-grid" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr));">
        <section class="card"><div class="section-label">Engagement</div>
          <div data-engagement>${renderLoading("Loading…")}</div></section>
        <section class="card"><div class="section-label">Affordability</div>
          <div class="field-hint">Solid color ≈ how many days of median daily income each perk costs.</div>
          <div data-affordability>${renderLoading("Loading…")}</div></section>
      </div>

      <section class="card">
        <div class="section-label">Biggest Spenders (all time)</div>
        <div class="field-hint">Lifetime currency burned — rentals, consumables and other sinks. Transfers and staff clawbacks don't count: a transfer moves currency sideways rather than removing it.</div>
        <div data-burn>${renderLoading("Loading…")}</div>
      </section>

      <section class="card">
        <div class="section-label">Top Transfers (30d)</div>
        <div class="field-hint">One-way volume over 500 is flagged — a possible farming/laundering signal worth an audit.</div>
        <div data-transfers>${renderLoading("Loading…")}</div>
      </section>

      <section class="card" data-live-card>
        <div class="section-label">Happening Now</div>
        <div class="field-hint">The quest pulse — anonymous counts only, auto-refreshes every 45s.</div>
        <div data-live>${renderLoading("Loading…")}</div>
      </section>

      <section class="card">
        <div class="section-label">Members</div>
        <div data-members>${renderLoading("Loading…")}</div>
      </section>
    </div>`;

  container.querySelector("[data-refresh]").addEventListener("click", () => {
    refresh(container, members);
  });
}

async function refreshLive(container) {
  const host = container.querySelector("[data-live]");
  if (!host) return;
  let live;
  try {
    live = await api("/api/economy/quests/live");
  } catch (err) {
    host.innerHTML = renderError(`Couldn't load the quest pulse — ${err.message}. It retries automatically every 45 seconds.`);
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
            <div style="width:${Math.min(100, c.pct)}%; height:100%; background:${CHART_BAR};"></div>
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
    }).join("") || renderEmpty("None active");
    return `<div><div class="section-label" style="text-transform:capitalize;">${cad} (this period)</div>${rows}</div>`;
  }).join("");
  const eventRows = (live.events || []).map((q) =>
    `<div class="field-hint" style="margin:2px 0;">${esc(q.title)} —
      <strong>${fmtNum(q.paid_7d)}</strong> this week · ${fmtNum(q.paid_total)} ever</div>`,
  ).join("") || renderEmpty("None active");
  bits.push(`
    <div class="card-grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));">
      ${cadCols}
      <div><div class="section-label">Event Quests</div>${eventRows}</div>
    </div>`);

  host.innerHTML = bits.join("");
}

async function refresh(container, members) {
  container.querySelector("[data-summary]").innerHTML = renderLoading("Refreshing…");
  const stats = await api("/api/economy/stats", { limit: 100 }).then(
    (d) => ({ ok: true, data: d }),
    (err) => ({ ok: false, err }),
  );
  applyStats(container, members, stats);
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
    host.innerHTML = renderEmpty("Nobody holds a balance yet. This fills in once members start earning coins.");
    return;
  }
  host.innerHTML = buckets.map((b) => {
    const pct = Math.round((b.count / max) * 100);
    return `
      <div style="display:flex; align-items:center; gap:10px; margin:4px 0;">
        <div style="width:72px; text-align:right; font-variant-numeric:tabular-nums; color:var(--ink-dim);">${esc(bucketLabel(b))}</div>
        <div style="flex:1; background:var(--rule-soft); border-radius:4px; height:16px; overflow:hidden;">
          <div style="width:${pct}%; height:100%; background:${CHART_ACCENT};"></div>
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
    host.innerHTML = renderEmpty("No coins minted in the last 8 weeks. Income appears here as members log in, chat, finish quests, and play games.");
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
    host.innerHTML = renderEmpty("No earners yet, so there is nothing to price perks against. This fills in once members start earning daily income.");
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
          <div style="width:${pct}%; height:100%; background:${CHART_BAR};"></div>
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
    host.innerHTML = renderEmpty("Nobody has spent anything yet. Rentals, consumables, and other sinks show up here.");
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
    host.innerHTML = renderEmpty("No transfers in the last 30 days. Member-to-member payments show up here.");
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
    host.innerHTML = renderEmpty("Nobody holds a balance yet. This fills in once members start earning coins.");
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
