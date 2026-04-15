import { api, apiPost, esc } from "../api.js";
import { showTranscript } from "../transcript-modal.js";

const AVATAR_COLORS = ["#c07aa1", "#5865f2", "#23a55a", "#e6b84c", "#f23f43", "#7F8F3A"];

function avatarColor(key) {
  const s = String(key || "");
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

function initial(name) {
  const s = String(name || "?").trim();
  return (s[0] || "?").toUpperCase();
}

function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
         d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function fmtAge(ts) {
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
  return Math.floor(s / 86400) + "d";
}

function fmtJoinDate(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" });
}

function memberForLabel(ts) {
  if (!ts) return "";
  const now = Date.now() / 1000;
  const months = Math.max(1, Math.round((now - ts) / (30 * 86400)));
  if (months < 12) return `${months} month${months === 1 ? "" : "s"}`;
  const years = Math.floor(months / 12);
  const rem = months % 12;
  if (!rem) return `${years} year${years === 1 ? "" : "s"}`;
  return `${years}y ${rem}m`;
}

function fmtShortDate(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { day: "2-digit", month: "short" });
}

function priorityClass(t) {
  if (t.escalated) return "high";
  if (t.status === "open" && !t.claimer_id) return "med";
  return "low";
}

function statusChip(t) {
  if (t.status === "closed" || t.status === "deleted") return '<span class="t-chip closed">Closed</span>';
  if (t.claimer_id) return '<span class="t-chip claimed">Claimed</span>';
  return '<span class="t-chip open">Open</span>';
}

function ticketSubject(t) {
  const desc = (t.description || "").trim();
  if (desc) return desc.length > 80 ? desc.slice(0, 77) + "…" : desc;
  return `Ticket #${t.id}`;
}

function renderList(tickets, activeId) {
  if (!tickets.length) {
    return '<div class="empty">No tickets match this filter.</div>';
  }
  return tickets.map((t) => {
    const cls = priorityClass(t) + (t.id === activeId ? " active" : "");
    const reporter = t.user_name || t.user_id || "unknown";
    const who = t.claimer_name
      ? `claimed by <b style="color:var(--ink-dim)">${esc(t.claimer_name)}</b>`
      : `by <b style="color:var(--ink-dim)">${esc(reporter)}</b>`;
    const age = t.status === "open" ? fmtAge(t.created_at) + " ago" : fmtAge(t.closed_at || t.created_at);
    const channelChip = t.channel_name
      ? `<span class="ch">#${esc(t.channel_name)}</span><span class="dot">·</span>`
      : "";
    const escalatedChip = t.escalated
      ? '<span class="t-chip" style="background:var(--gold-soft);color:var(--gold-solid)">Escalated</span>'
      : "";
    return `
      <div class="ticket-item ${cls}" data-ticket-id="${esc(t.id)}">
        <div class="pri"></div>
        <div class="body">
          <div class="subj">${esc(ticketSubject(t))}</div>
          <div class="row">
            ${channelChip}
            ${escalatedChip}
            <span>${who}</span>
            ${statusChip(t)}
          </div>
        </div>
        <div class="right">
          <span class="id">#${esc(t.id)}</span>
          <span class="age">${esc(age)}</span>
        </div>
      </div>
    `;
  }).join("");
}

const ICON_WARN = `<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 1L13 12H1L7 1Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M7 6V8.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="7" cy="10.5" r="0.75" fill="currentColor"/></svg>`;
const ICON_JAIL = `<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><rect x="2" y="3" width="10" height="9" stroke="currentColor" stroke-width="1.5" rx="1"/><path d="M5 3V12M9 3V12" stroke="currentColor" stroke-width="1.5"/><path d="M4.5 3V1.5a2.5 2.5 0 015 0V3" stroke="currentColor" stroke-width="1.5"/></svg>`;
const ICON_CHEV = `<svg class="chev" width="10" height="10" viewBox="0 0 10 10" fill="none"><path d="M2 4L5 7L8 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
const ICON_NOTE = `<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M1 11V3a1 1 0 011-1h6l4 4v5a1 1 0 01-1 1H2a1 1 0 01-1-1z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M8 2v4h4" stroke="currentColor" stroke-width="1.5"/></svg>`;
const ICON_X = `<svg width="10" height="10" viewBox="0 0 10 10" fill="none"><path d="M2 2L8 8M8 2L2 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
const ICON_DOC = `<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M1 11V3a1 1 0 011-1h6l4 4v5a1 1 0 01-1 1H2a1 1 0 01-1-1z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M8 2v4h4" stroke="currentColor" stroke-width="1.5"/></svg>`;

function renderActions(t) {
  if (t && (t.status === "closed" || t.status === "deleted")) {
    return `
      <div class="td-actions">
        <button class="act-btn" data-action="note">${ICON_NOTE}Add note</button>
        <span class="act-spacer"></span>
        <button class="act-btn" data-action="transcript" title="View transcript">${ICON_DOC}Transcript</button>
        <button class="act-btn warn" data-action="reopen">Reopen ticket</button>
      </div>
    `;
  }
  const me = window.__dk_user;
  const claimedByMe = t && t.claimer_id && me && String(t.claimer_id) === String(me.user_id);
  const claimLabel = claimedByMe ? "Claimed by you" : (t && t.claimer_id ? "Reassign to me" : "Claim");
  return `
    <div class="td-actions">
      <button class="act-btn warn" data-action="warn">${ICON_WARN}Warn</button>
      <div class="split-btn">
        <button class="act-btn jail" data-action="jail">${ICON_JAIL}Jail · 24h</button>
        <button class="act-btn jail" data-action="jail-custom" aria-label="Change duration">${ICON_CHEV}</button>
      </div>
      <button class="act-btn" data-action="note">${ICON_NOTE}Add note</button>
      <button class="act-btn ghost" data-action="dismiss">Dismiss</button>
      <span class="act-spacer"></span>
      <button class="act-btn ghost" data-action="claim"${claimedByMe ? " disabled" : ""}>${esc(claimLabel)}</button>
      <button class="act-btn" data-action="transcript" title="View transcript">${ICON_DOC}Transcript</button>
      <button class="act-btn" data-action="close">Close ticket${ICON_X}</button>
    </div>
  `;
}

function renderHistory(history) {
  if (!history.length) {
    return `<div class="empty" style="padding:8px 0;color:var(--ink-mute);font-size:12px">No prior actions.</div>`;
  }
  return `<div class="history">${history.map((h) => {
    const dLabel = fmtShortDate(h.date);
    const kindLabel = h.kind ? h.kind[0].toUpperCase() + h.kind.slice(1) : "";
    const actorSuffix = h.actor_name ? ` — ${esc(h.actor_name)}` : "";
    return `
      <div class="history-row">
        <span class="h-kind ${esc(h.kind)}">${esc(kindLabel)}</span>
        <span class="h-body">${esc(h.body || "")}${actorSuffix}</span>
        <span class="h-time">${esc(dLabel)}</span>
      </div>
    `;
  }).join("")}</div>`;
}

function renderSubjectCard(t, detail) {
  const reporter = t.user_name || t.user_id || "unknown";
  const color = avatarColor(t.user_id || t.user_name);
  const init = initial(t.user_name || t.user_id);

  let metaLine = "—";
  let warnCount = "—";
  let jailCount = "—";
  if (detail) {
    warnCount = detail.subject.warn_count_active;
    jailCount = detail.subject.jail_count_total;
    if (detail.subject.joined_at) {
      const parts = [`Joined ${fmtJoinDate(detail.subject.joined_at)}`];
      const mf = memberForLabel(detail.subject.joined_at);
      if (mf) parts.push(`Member for ${mf}`);
      metaLine = parts.join(" · ");
    }
  }

  const idSuffix = t.user_id
    ? `<span style="color:var(--ink-mute);font-weight:500;font-family:var(--mono);font-size:11px">· ${esc(t.user_id)}</span>`
    : "";

  return `
    <div class="user-card">
      <div class="av" style="background:${color}">${esc(init)}</div>
      <div class="info">
        <div class="n">${esc(reporter)} ${idSuffix}</div>
        <div class="m">${esc(metaLine)}</div>
      </div>
      <div class="counts">
        <div><b>${esc(warnCount)}</b>warns</div>
        <div><b>${esc(jailCount)}</b>jails</div>
      </div>
    </div>
  `;
}

function renderDetail(t, detail) {
  if (!t) {
    return '<div class="empty">Select a ticket from the queue to view details.</div>';
  }

  const reporter = t.user_name || t.user_id || "unknown";
  const when = t.status === "open"
    ? `opened ${fmtAge(t.created_at)} ago`
    : `closed ${fmtAge(t.closed_at || t.created_at)} ago`;
  const crumb = `#T-${esc(t.id)} &nbsp;·&nbsp; ${esc(when)}${t.escalated ? " &nbsp;·&nbsp; escalated" : ""}`;

  const channelName = t.channel_name || (detail && detail.channel_name) || "";
  const channelPair = channelName
    ? `<span class="pair"><span class="k">Channel</span><b style="color:var(--gold-solid)">#${esc(channelName)}</b></span>`
    : "";
  const claimedPair = t.claimer_name
    ? `<span class="pair"><span class="k">Claimed by</span><b>${esc(t.claimer_name)}</b></span>`
    : `<span class="pair"><span class="k">Claimed</span><b style="color:var(--ink-mute)">Unclaimed</b></span>`;

  const desc = (t.description || "").trim() || "(no description)";

  const historyHtml = detail
    ? renderHistory(detail.history || [])
    : `<div class="empty" style="padding:8px 0;color:var(--ink-mute);font-size:12px">Loading…</div>`;

  const closeSection = (t.status === "closed" || t.status === "deleted") ? `
    <div class="td-section">Resolution</div>
    <div class="user-card" style="background:var(--bg-floor)">
      <div class="av" style="background:${avatarColor(t.closed_by || "system")}">${esc(initial(t.closer_name || "·"))}</div>
      <div class="info">
        <div class="n">Closed by ${esc(t.closer_name || "system")}</div>
        <div class="m">${esc(fmtTs(t.closed_at))} · ${esc(t.close_reason || "no reason given")}</div>
      </div>
    </div>
  ` : "";

  return `
    <div class="td-head">
      <div class="td-crumb">${crumb}</div>
      <h3 class="td-title">${esc(ticketSubject(t))}</h3>
      <div class="td-meta">
        ${channelPair}
        <span class="pair"><span class="k">Reporter</span><b>${esc(reporter)}</b></span>
        ${claimedPair}
        <span class="pair"><span class="k">Status</span>${statusChip(t)}</span>
      </div>
    </div>

    <div class="td-body">
      <div class="td-section">Report</div>
      <div style="font-size:14px;color:var(--ink);line-height:1.5;white-space:pre-wrap;word-break:break-word;padding:4px 8px 8px">${esc(desc)}</div>

      <div class="td-section">Subject</div>
      ${renderSubjectCard(t, detail)}

      <div class="td-section">Prior actions</div>
      ${historyHtml}

      ${closeSection}
    </div>

    ${renderActions(t)}
  `;
}

const FILTERS = {
  open:   (t) => t.status === "open",
  mine:   (t) => {
    if (t.status !== "open" || !t.claimer_id) return false;
    const me = window.__dk_user;
    if (!me || !me.user_id) return true;
    return String(t.claimer_id) === String(me.user_id);
  },
  closed: (t) => t.status === "closed" || t.status === "deleted",
  all:    () => true,
};

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <div class="panel-head">
        <div>
          <h1 class="panel-title">Active <em>tickets</em></h1>
          <div class="sub" style="font-size:13px;color:var(--ink-dim);margin-top:6px">
            Flagged messages, auto-mod hits, and member reports awaiting review.
          </div>
        </div>
      </div>

      <section class="mod-split">
        <div class="ticket-list-wrap">
          <div class="ticket-list-head">
            <h3>Queue</h3>
            <div class="ctrl-group" role="tablist" data-filter-group>
              <button class="active" data-filter="open">Open</button>
              <button data-filter="mine">Mine</button>
              <button data-filter="closed">Closed</button>
              <button data-filter="all">All</button>
            </div>
          </div>
          <div class="ticket-list" data-list>
            <div class="empty">Loading…</div>
          </div>
        </div>

        <div class="ticket-detail" data-detail>
          <div class="empty">Loading…</div>
        </div>
      </section>
    </div>
  `;

  const listEl = container.querySelector("[data-list]");
  const detailEl = container.querySelector("[data-detail]");
  const filterGroup = container.querySelector("[data-filter-group]");

  const state = {
    tickets: [],
    closedTickets: null,
    filter: "open",
    activeId: null,
    detailCache: new Map(),
    fetchToken: 0,
  };

  function currentTicketSource() {
    return state.filter === "closed" ? (state.closedTickets || []) : state.tickets;
  }

  function setFilterBadge(filter, label, count) {
    const btn = filterGroup.querySelector(`[data-filter="${filter}"]`);
    if (!btn) return;
    btn.textContent = label;
    if (count) {
      const badge = document.createElement("span");
      badge.className = "filter-badge";
      badge.textContent = String(count);
      btn.appendChild(badge);
    }
  }

  function updateFilterBadges() {
    const openCount = state.tickets.filter((t) => t.status === "open").length;
    const mineCount = state.tickets.filter(FILTERS.mine).length;
    setFilterBadge("open", "Open", openCount);
    setFilterBadge("mine", "Mine", mineCount);
  }

  function render() {
    const source = currentTicketSource();
    const filtered = source.filter(FILTERS[state.filter]);
    if (!filtered.find((t) => t.id === state.activeId)) {
      state.activeId = filtered[0]?.id ?? null;
    }
    listEl.innerHTML = renderList(filtered, state.activeId);
    const active = source.find((t) => t.id === state.activeId) || null;
    const detail = active ? state.detailCache.get(active.id) : null;
    detailEl.innerHTML = renderDetail(active, detail);
    if (active && !detail) loadDetail(active.id);
    updateFilterBadges();
  }

  async function loadDetail(id) {
    const token = ++state.fetchToken;
    try {
      const detail = await api(`/api/moderation/tickets/${encodeURIComponent(id)}`);
      if (token !== state.fetchToken) return;
      state.detailCache.set(id, detail);
      if (state.activeId === id) render();
    } catch (err) {
      console.error("Failed to load ticket detail:", err);
    }
  }

  async function refresh() {
    try {
      const data = await api("/api/moderation/tickets");
      state.tickets = data.tickets || [];
      state.closedTickets = null;
      if (state.filter === "closed") {
        await refreshClosed();
      } else {
        render();
      }
    } catch (err) {
      listEl.innerHTML = `<div class="error" style="padding:20px">${esc(err.message)}</div>`;
      detailEl.innerHTML = "";
    }
  }

  async function refreshClosed() {
    listEl.innerHTML = '<div class="empty">Loading…</div>';
    try {
      const data = await api("/api/moderation/tickets?status=closed");
      state.closedTickets = data.tickets || [];
      render();
    } catch (err) {
      listEl.innerHTML = `<div class="error" style="padding:20px">${esc(err.message)}</div>`;
    }
  }

  filterGroup.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-filter]");
    if (!btn) return;
    filterGroup.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
    state.filter = btn.dataset.filter;
    state.activeId = null;
    if (state.filter === "closed" && state.closedTickets === null) {
      refreshClosed();
    } else {
      render();
    }
  });

  listEl.addEventListener("click", (e) => {
    const row = e.target.closest(".ticket-item");
    if (!row) return;
    state.activeId = Number(row.dataset.ticketId);
    render();
  });

  async function runAction(action, t) {
    const id = t.id;
    const base = `/api/moderation/tickets/${encodeURIComponent(id)}`;
    if (action === "claim") {
      await apiPost(`${base}/claim`);
      return;
    }
    if (action === "reopen") {
      await apiPost(`${base}/reopen`);
      return;
    }
    if (action === "warn") {
      const reason = window.prompt("Reason for warning?");
      if (reason === null) return;
      const trimmed = reason.trim();
      if (!trimmed) { window.alert("A reason is required."); return; }
      await apiPost(`${base}/warn`, { reason: trimmed });
      return;
    }
    if (action === "jail") {
      const reason = window.prompt("Reason for 24h jail? (blank = no reason)");
      if (reason === null) return;
      await apiPost(`${base}/jail`, { duration: "24h", reason: reason.trim() });
      return;
    }
    if (action === "jail-custom") {
      const duration = window.prompt("Jail duration? (e.g. 30m, 2h, 7d)", "24h");
      if (duration === null || !duration.trim()) return;
      const reason = window.prompt("Reason?");
      if (reason === null) return;
      await apiPost(`${base}/jail`, { duration: duration.trim(), reason: reason.trim() });
      return;
    }
    if (action === "note") {
      const body = window.prompt("Note body?");
      if (body === null) return;
      const trimmed = body.trim();
      if (!trimmed) { window.alert("Note body is required."); return; }
      await apiPost(`${base}/note`, { body: trimmed });
      return;
    }
    if (action === "dismiss") {
      const reason = window.prompt("Dismissal reason? (optional)", "");
      if (reason === null) return;
      await apiPost(`${base}/dismiss`, { reason: reason.trim() });
      return;
    }
    if (action === "close") {
      const reason = window.prompt("Reason for closing?");
      if (reason === null) return;
      await apiPost(`${base}/close`, { reason: reason.trim() });
      return;
    }
    throw new Error(`Unknown action: ${action}`);
  }

  detailEl.addEventListener("click", async (e) => {
    const btn = e.target.closest(".act-btn");
    if (!btn || btn.disabled) return;
    const action = btn.dataset.action;
    if (!action) return;

    if (action === "transcript") {
      if (state.activeId) showTranscript("ticket", state.activeId);
      return;
    }

    const t = state.tickets.find((x) => x.id === state.activeId);
    if (!t) return;

    btn.disabled = true;
    try {
      await runAction(action, t);
      state.detailCache.delete(t.id);
      await refresh();
    } catch (err) {
      console.error(`Ticket action "${action}" failed:`, err);
      window.alert(`Action failed: ${err.message}`);
    } finally {
      btn.disabled = false;
    }
  });

  refresh();

  return { unmount() {} };
}
