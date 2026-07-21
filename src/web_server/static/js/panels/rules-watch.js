import { api, apiPost, esc, fmtTs } from "../api.js";
import { toast } from "../ui.js";
import { makeFilterStrip } from "../tab-strip.js";
import { renderLoading, renderEmpty } from "../states.js";

const TIER_BADGE = {
  immediate: '<span class="badge badge-danger">Immediate</span>',
  digest:    '<span class="badge badge-warning">Digest</span>',
  logged:    '<span class="badge badge-dim">Logged</span>',
};

const LABEL_BADGE = {
  true:  '<span class="badge badge-danger">Violation</span>',
  false: '<span class="badge badge-ok">False Positive</span>',
  null:  '<span class="badge badge-dim">Unlabeled</span>',
};

function chip(label, val, highlight = false) {
  const cls = highlight ? "sig-chip sig-chip--hi" : "sig-chip";
  return `<span class="${cls}">${esc(label)}: ${esc(String(val))}</span>`;
}

function sigChips(ev) {
  const parts = [];
  if (ev.slur_signal)              parts.push(`<span class="sig-chip sig-chip--hi">🔴 slur</span>`);
  if (ev.boundary_token_crossed)   parts.push(`<span class="sig-chip sig-chip--hi">🛑 boundary</span>`);
  if (ev.consent_pair_recently_revoked) parts.push(`<span class="sig-chip sig-chip--hi">⚠️ revoked</span>`);
  if (ev.target_withdrew)          parts.push(`<span class="sig-chip sig-chip--hi">😶 withdrew</span>`);
  if (ev.consent_pair_active)      parts.push(`<span class="sig-chip">consent ✓</span>`);
  if (ev.persistence_count > 0)    parts.push(chip("persist", ev.persistence_count, ev.persistence_count >= 3));
  if (ev.mutual_interaction_count != null) parts.push(chip("mutual", ev.mutual_interaction_count));
  if (ev.dm_tier_mismatch)         parts.push(`<span class="sig-chip">tier ⚡</span>`);
  return parts.join(" ");
}

function renderRow(ev, activeId) {
  const cls = ev.id === activeId ? "rw-row active" : "rw-row";
  const tier = TIER_BADGE[ev.priority_tier] || "";
  const label = ev.is_violation != null
    ? LABEL_BADGE[String(ev.is_violation)]
    : LABEL_BADGE["null"];
  const rule = ev.guard_rule ? `Rule ${esc(ev.guard_rule)}` : "?";
  const conf = ev.guard_confidence != null ? `${Math.round(ev.guard_confidence * 100)}%` : "?";
  return `
    <div class="${cls}" data-id="${Number(ev.id)}" tabindex="0" role="button">
      <div class="rw-row__tier">${tier}</div>
      <div class="rw-row__rule">${rule} <span class="dim">${esc(conf)}</span></div>
      <div class="rw-row__score">${ev.priority_score != null ? ev.priority_score.toFixed(1) : "?"}</div>
      <div class="rw-row__ts">${fmtTs(ev.detected_at)}</div>
      <div class="rw-row__label">${label}</div>
    </div>`;
}

function renderDetail(ev) {
  if (!ev || !ev.id) return '<div class="empty">Select an event to review.</div>';

  let windowHtml = "";
  let windowFlagged = false;
  if (ev.window_json) {
    try {
      const lines = JSON.parse(ev.window_json);
      // The triggering message is the newest line in the window (built oldest-first).
      const last = lines.length - 1;
      windowFlagged = last >= 0;
      windowHtml = `<div class="rw-window">${lines.map((l, i) =>
        `<div class="rw-window__line${i === last ? " rw-window__line--flag" : ""}">${esc(l)}</div>`
      ).join("")}</div>`;
    } catch {
      windowHtml = `<pre class="rw-window">${esc(ev.window_json)}</pre>`;
    }
  }

  const labeledBy = ev.labeled_by ? `Labeled by <code>${esc(String(ev.labeled_by))}</code> at ${fmtTs(ev.labeled_at)}` : "";
  const alreadyLabeled = ev.is_violation != null;

  return `
    <div class="rw-detail">
      <button class="btn btn-secondary rw-back" data-back>← Back to queue</button>
      <div class="rw-detail__header">
        <span>Event #${ev.id}</span>
        ${TIER_BADGE[ev.priority_tier] || ""}
        ${ev.is_violation != null ? LABEL_BADGE[String(ev.is_violation)] : LABEL_BADGE["null"]}
      </div>

      <dl class="rw-meta">
        <dt>Author</dt><dd><code>${esc(String(ev.author_id))}</code></dd>
        <dt>Target</dt><dd><code>${ev.target_id ? esc(String(ev.target_id)) : "unknown"}</code> <span class="dim">(${esc(ev.target_confidence || "?")})</span></dd>
        <dt>Channel</dt><dd><code>${esc(String(ev.channel_id))}</code></dd>
        <dt>Detected</dt><dd>${fmtTs(ev.detected_at)}</dd>
        <dt>Guard</dt><dd>Rule ${esc(ev.guard_rule || "?")} · ${ev.guard_confidence != null ? Math.round(ev.guard_confidence * 100) : "?"}% · ${esc(ev.guard_reason || "—")}</dd>
        <dt>Priority</dt><dd>${ev.priority_score != null ? ev.priority_score.toFixed(1) : "?"} — ${esc(ev.priority_reason || "—")}</dd>
      </dl>

      <div class="rw-signals">${sigChips(ev)}</div>

      ${windowHtml ? `<div class="rw-section"><div class="rw-section__title">Conversation window${windowFlagged ? ' · <span class="rw-flag-legend">flagged message</span>' : ""}</div>${windowHtml}</div>` : ""}

      ${!alreadyLabeled ? `
        <div class="rw-actions">
          <button class="btn btn-danger" data-label="true">✅ Confirmed violation</button>
          <button class="btn btn-secondary" data-label="false">❌ False positive</button>
        </div>` : `<div class="dim" style="margin-top:8px">${labeledBy}</div>`}
    </div>`;
}

function renderStatsTab(stats) {
  if (!stats) return renderEmpty("No stats yet.");
  const fpPct = stats.fp_rate != null ? `${(stats.fp_rate * 100).toFixed(0)}%` : "—";
  const tierRows = Object.entries(stats.by_tier || {})
    .map(([t, n]) => `<tr><td>${esc(t)}</td><td>${n}</td></tr>`).join("");
  const ruleRows = Object.entries(stats.by_rule || {})
    .map(([r, n]) => `<tr><td>Rule ${esc(r)}</td><td>${n}</td></tr>`).join("");
  return `
    <div class="rw-stats">
      <div class="mod-stats">
        <div class="mod-stat"><div class="lbl">Total</div><div class="v">${stats.total}</div></div>
        <div class="mod-stat"><div class="lbl">Labeled</div><div class="v">${stats.labeled}</div></div>
        <div class="mod-stat"><div class="lbl">Confirmed</div><div class="v">${stats.confirmed}</div></div>
        <div class="mod-stat"><div class="lbl">FP rate</div><div class="v">${fpPct}</div></div>
      </div>
      <div class="rw-stats__tables">
        ${tierRows ? `<table class="rw-table"><thead><tr><th>Tier</th><th>Count</th></tr></thead><tbody>${tierRows}</tbody></table>` : ""}
        ${ruleRows ? `<table class="rw-table"><thead><tr><th>Rule</th><th>Events</th></tr></thead><tbody>${ruleRows}</tbody></table>` : ""}
      </div>
    </div>`;
}

const LEDGER_KIND = {
  dm_consent:     '<span class="badge badge-dim">DM consent</span>',
  cross_platform: '<span class="badge badge-dim">Cross-platform</span>',
};

function renderLedgerTab(rows) {
  if (!rows.length) {
    return renderEmpty("Nothing on the ledger yet. These fire rarely by design — roughly once a month between them.");
  }
  const body = rows.map(r => `
    <tr>
      <td class="rw-row__ts">${esc(fmtTs(r.detected_at))}</td>
      <td>${LEDGER_KIND[r.kind] || esc(r.kind)}</td>
      <td>${r.platform ? esc(r.platform) : "—"}</td>
      <td>
        <div class="rw-ledger__excerpt">${esc(r.excerpt || "")}</div>
        ${r.matched_phrase ? `<span class="rw-ledger__match">matched: ${esc(r.matched_phrase)}</span>` : ""}
      </td>
    </tr>`).join("");
  return `<div class="rw-ledger-scroll"><table class="rw-ledger-tbl">
    <thead><tr><th>When</th><th>Kind</th><th>Platform</th><th>What was said</th></tr></thead>
    <tbody>${body}</tbody></table></div>`;
}

function renderLedgerRepeats(rows) {
  if (!rows.length) return "";
  const items = rows.map(r =>
    `<li>User <code>${esc(r.author_id)}</code> — ${esc(String(r.distinct_targets))} different people, ${esc(String(r.hits))} entries</li>`
  ).join("");
  return `<div class="rw-ledger-repeats">
    <h4>Same entry against more than one person</h4>
    <ul style="margin:0; padding-left:18px">${items}</ul>
  </div>`;
}

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Rules Watch</h2>
        <div class="subtitle">Passive AI moderation monitor — alert queue and label capture</div>
      </header>

      <div class="ctrl-group" role="group" aria-label="Views" data-tabs style="margin-bottom:16px">
        <button class="active" data-tab="queue">Alert Queue</button>
        <button data-tab="ledger">Ledger</button>
        <button data-tab="stats">Label Stats</button>
      </div>

      <div data-tab-content="queue">
        <div class="ctrl-group" role="group" aria-label="Filter events" data-filter-group style="margin-bottom:12px">
          <button class="active" data-tier="">All</button>
          <button data-tier="immediate">Immediate</button>
          <button data-tier="digest">Digest</button>
          <button data-tier="logged">Logged</button>
        </div>
        <label class="rw-pending-toggle">
          <input type="checkbox" data-pending-only checked> Unlabeled only
        </label>

        <div class="rw-layout">
          <div class="rw-list" data-list>
            ${renderLoading("Loading…")}
          </div>
          <div class="rw-detail-pane" data-detail>
            <div class="empty">Select an event to review.</div>
          </div>
        </div>
      </div>

      <div data-tab-content="ledger" style="display:none">
        <div class="rw-ledger-note">
          A record of specific things that were said — not an assessment of anyone.
          Nothing here has been flagged, scored, or judged, and no alert was sent.
          It exists so that if someone is being reviewed, the dates are already on hand.
        </div>
        <div class="ctrl-group" role="group" aria-label="Filter ledger" data-ledger-filter style="margin-bottom:12px">
          <button class="active" data-kind="">All</button>
          <button data-kind="dm_consent">DM consent</button>
          <button data-kind="cross_platform">Cross-platform</button>
        </div>
        <div data-ledger-repeats></div>
        <div data-ledger-content>${renderLoading("Loading…")}</div>
      </div>

      <div data-tab-content="stats" style="display:none">
        <div data-stats-content>${renderLoading("Loading…")}</div>
      </div>
    </div>

    <style>
      .rw-layout { display:flex; gap:16px; min-height:400px; }
      .rw-list { flex:0 0 340px; overflow-y:auto; border:1px solid var(--border); border-radius:6px; }
      .rw-detail-pane { flex:1; overflow-y:auto; }
      .rw-row { display:grid; grid-template-columns:90px 80px 50px 100px 1fr; gap:6px;
                align-items:center; padding:8px 10px; cursor:pointer; border-bottom:1px solid var(--border); font-size:13px; }
      .rw-row:hover, .rw-row.active { background:var(--hover-bg, rgba(255,255,255,.05)); }
      .rw-row__score { font-weight:600; }
      .rw-row__ts { color:var(--dim, #888); font-size:12px; }
      .rw-detail { padding:12px; }
      .rw-detail__header { display:flex; gap:8px; align-items:center; margin-bottom:12px; font-weight:600; }
      .rw-meta { display:grid; grid-template-columns:80px 1fr; gap:4px 8px; font-size:13px; margin-bottom:12px; }
      .rw-meta dt { color:var(--dim, #888); }
      .rw-signals { display:flex; flex-wrap:wrap; gap:4px; margin-bottom:12px; }
      .sig-chip { font-size:11px; padding:2px 6px; border-radius:10px; background:var(--surface2, #3a3d42); }
      .sig-chip--hi { background:var(--danger-dim, #6b2b2b); color:#f99; }
      .rw-section__title { font-size:12px; color:var(--dim, #888); margin-bottom:4px; }
      .rw-window { font-size:12px; background:var(--surface2, #2a2d32); padding:10px; border-radius:4px;
                   white-space:pre-wrap; word-break:break-word; max-height:300px; overflow-y:auto; }
      .rw-window__line { padding:1px 4px; border-radius:3px; }
      .rw-window__line--flag { background:var(--red-soft, #4a1f1f); box-shadow:inset 3px 0 0 var(--red, #e06666);
                               padding-left:8px; font-weight:600; }
      .rw-flag-legend { color:var(--red, #e06666); }
      .rw-actions { display:flex; gap:8px; margin-top:12px; }
      .rw-stats__tables { display:flex; gap:16px; margin-top:16px; }
      .rw-table { border-collapse:collapse; font-size:13px; }
      .rw-table th, .rw-table td { padding:4px 12px; border:1px solid var(--border); }
      .rw-pending-toggle { font-size:13px; display:flex; align-items:center; gap:6px; margin-bottom:8px; }
      .rw-ledger-note { font-size:13px; color:var(--dim, #888); border-left:3px solid var(--border);
                        padding:8px 12px; margin-bottom:14px; line-height:1.5; }
      .rw-ledger-tbl { border-collapse:collapse; font-size:13px; width:100%; }
      .rw-ledger-tbl th { text-align:left; font-weight:600; color:var(--dim, #888); font-size:12px;
                          padding:6px 10px; border-bottom:1px solid var(--border); }
      .rw-ledger-tbl td { padding:8px 10px; border-bottom:1px solid var(--border); vertical-align:top; }
      .rw-ledger-tbl tr:last-child td { border-bottom:none; }
      .rw-ledger__excerpt { color:var(--fg, #ddd); word-break:break-word; }
      .rw-ledger__match { display:inline-block; font-size:11px; padding:1px 6px; border-radius:3px;
                          background:var(--surface2, #3a3d42); margin-top:4px; }
      .rw-ledger-repeats { border:1px solid var(--border); border-radius:6px; padding:10px 12px;
                           margin-bottom:14px; font-size:13px; }
      .rw-ledger-repeats h4 { margin:0 0 6px; font-size:13px; }
      .rw-ledger-scroll { overflow-x:auto; }
      @media (max-width: 700px) {
        .rw-ledger-tbl { font-size:12px; }
        .rw-ledger-tbl th:nth-child(3), .rw-ledger-tbl td:nth-child(3) { display:none; }
      }
      .badge-ok { background:#1e4620; color:#7ecb7f; }
      .rw-back { display:none; }

      @media (max-width: 700px) {
        .rw-layout { flex-direction:column; min-height:0; }
        .rw-list { flex:1 1 auto; max-height:none; }
        /* Master/detail: show one pane at a time */
        .rw-detail-pane { display:none; }
        .rw-layout--detail .rw-list { display:none; }
        .rw-layout--detail .rw-detail-pane { display:block; }
        .rw-back { display:inline-flex; margin-bottom:10px; }
        .rw-row { grid-template-columns:72px 1fr auto; column-gap:8px; row-gap:2px; padding:12px 10px;
                  grid-template-areas:"tier rule score" "tier ts label"; font-size:14px; }
        .rw-row__tier { grid-area:tier; }
        .rw-row__rule { grid-area:rule; }
        .rw-row__score { grid-area:score; text-align:right; }
        .rw-row__ts { grid-area:ts; }
        .rw-row__label { grid-area:label; text-align:right; }
        .rw-meta { grid-template-columns:80px 1fr; }
        .rw-actions { position:sticky; bottom:0; gap:10px; padding:10px 0;
                      background:var(--bg-card, var(--bg, #1e1e1e)); }
        .rw-actions .btn { flex:1; padding:14px 8px; font-size:15px; }
      }
    </style>
  `;

  const queuePane = container.querySelector('[data-tab-content="queue"]');
  const ledgerPane = container.querySelector('[data-tab-content="ledger"]');
  const statsPane = container.querySelector('[data-tab-content="stats"]');
  const ledgerContent = container.querySelector("[data-ledger-content]");
  const ledgerRepeatsEl = container.querySelector("[data-ledger-repeats]");
  const ledgerFilter = container.querySelector("[data-ledger-filter]");
  const filterGroup = container.querySelector("[data-filter-group]");
  const pendingOnlyEl = container.querySelector("[data-pending-only]");
  const layoutEl = container.querySelector(".rw-layout");
  const listEl = container.querySelector("[data-list]");
  const detailEl = container.querySelector("[data-detail]");
  const statsContent = container.querySelector("[data-stats-content]");

  let events = [];
  let activeId = null;
  let currentTier = "";
  let pendingOnly = true;
  let activeTab = "queue";
  let ledgerKind = "";

  // --- Tab switching ---
  makeFilterStrip(container.querySelector("[data-tabs]"), (tab) => {
    activeTab = tab;
    queuePane.style.display = activeTab === "queue" ? "" : "none";
    ledgerPane.style.display = activeTab === "ledger" ? "" : "none";
    statsPane.style.display = activeTab === "stats" ? "" : "none";
    if (activeTab === "stats") loadStats();
    if (activeTab === "ledger") loadLedger();
  }, { attr: "data-tab" });

  // --- Ledger kind filter ---
  makeFilterStrip(ledgerFilter, (kind) => {
    ledgerKind = kind;
    loadLedger();
  }, { attr: "data-kind" });

  // --- Tier filter ---
  makeFilterStrip(filterGroup, (tier) => {
    currentTier = tier;
    loadQueue();
  }, { attr: "data-tier" });

  pendingOnlyEl.addEventListener("change", () => {
    pendingOnly = pendingOnlyEl.checked;
    loadQueue();
  });

  // --- Select an event (renders detail, syncs list, handles mobile master/detail) ---
  function selectEvent(id) {
    activeId = id;
    renderList();
    const ev = id != null ? events.find(x => x.id === id) || null : null;
    detailEl.innerHTML = renderDetail(ev);
    bindDetailActions(ev);
    const mobile = window.matchMedia("(max-width: 700px)").matches;
    layoutEl.classList.toggle("rw-layout--detail", id != null);
    if (id != null) {
      if (mobile) layoutEl.scrollIntoView({ block: "start" });
      else {
        const activeRow = listEl.querySelector(".rw-row.active");
        if (activeRow) activeRow.scrollIntoView({ block: "nearest" });
      }
    }
  }

  // Next still-unlabeled event after the given one (wraps to earlier ones, else null).
  function nextUnlabeledId(afterId) {
    const idx = events.findIndex(x => x.id === afterId);
    for (let i = idx + 1; i < events.length; i++) {
      if (events[i].is_violation == null) return events[i].id;
    }
    for (let i = 0; i < idx; i++) {
      if (events[i].is_violation == null) return events[i].id;
    }
    return null;
  }

  // --- List click ---
  listEl.addEventListener("click", e => {
    const row = e.target.closest("[data-id]");
    if (!row) return;
    selectEvent(Number(row.dataset.id));
  });
  // Rows are role="button" tabindex="0" — activate with Enter/Space too.
  listEl.addEventListener("keydown", e => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const row = e.target.closest("[data-id]");
    if (!row) return;
    e.preventDefault();
    selectEvent(Number(row.dataset.id));
  });

  // --- Back to queue (mobile) ---
  detailEl.addEventListener("click", e => {
    if (e.target.closest("[data-back]")) selectEvent(null);
  });

  // --- Label buttons in detail pane ---
  function bindDetailActions(ev) {
    if (!ev) return;
    detailEl.querySelectorAll("[data-label]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const isViolation = btn.dataset.label === "true";
        try {
          await apiPost(`/api/rules-watch/events/${ev.id}/label`, { is_violation: isViolation });
          ev.is_violation = isViolation;
          const nextId = nextUnlabeledId(ev.id);
          if (nextId != null) {
            selectEvent(nextId);
          } else {
            activeId = null;
            renderList();
            detailEl.innerHTML = '<div class="empty">✅ Nothing left to review in this queue.</div>';
            layoutEl.classList.remove("rw-layout--detail");
          }
        } catch (err) {
          toast(err.message, "error");
          btn.textContent = "Error — try again";
        }
      });
    });
  }

  function renderList() {
    if (!events.length) {
      listEl.innerHTML = '<div class="empty" style="padding:16px">No events match this filter.</div>';
      return;
    }
    listEl.innerHTML = events.map(ev => renderRow(ev, activeId)).join("");
  }

  async function loadQueue() {
    listEl.innerHTML = '<div class="empty" style="padding:16px">Loading…</div>';
    const params = { limit: 100, pending_only: pendingOnly };
    if (currentTier) params.tier = currentTier;
    try {
      events = await api("/api/rules-watch/events", params);
      renderList();
      if (activeId) {
        const ev = events.find(x => x.id === activeId);
        if (ev) { detailEl.innerHTML = renderDetail(ev); bindDetailActions(ev); }
      }
    } catch {
      listEl.innerHTML = '<div class="empty" style="padding:16px">Failed to load events.</div>';
    }
  }

  async function loadLedger() {
    ledgerContent.innerHTML = renderLoading("Loading…");
    const params = { limit: 200 };
    if (ledgerKind) params.kind = ledgerKind;
    try {
      const rows = await api("/api/rules-watch/ledger", params);
      ledgerContent.innerHTML = renderLedgerTab(rows);
    } catch {
      ledgerContent.innerHTML = '<div class="empty">Failed to load ledger.</div>';
      return;
    }
    try {
      const repeats = await api("/api/rules-watch/ledger/repeats");
      ledgerRepeatsEl.innerHTML = renderLedgerRepeats(repeats);
    } catch {
      ledgerRepeatsEl.innerHTML = "";
    }
  }

  async function loadStats() {
    statsContent.innerHTML = renderLoading("Loading…");
    try {
      const stats = await api("/api/rules-watch/stats");
      statsContent.innerHTML = renderStatsTab(stats);
    } catch {
      statsContent.innerHTML = '<div class="empty">Failed to load stats.</div>';
    }
  }

  loadQueue();
}
