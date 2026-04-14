import { api, esc } from "../api.js";
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

function priorityClass(t) {
  if (t.escalated) return "high";
  if (t.status === "open" && !t.claimer_id) return "med";
  return "low";
}

function statusChip(t) {
  if (t.status === "closed") return '<span class="t-chip closed">Closed</span>';
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
    const extra = t.claimer_name ? `claimed by <b style="color:var(--ink-dim)">${esc(t.claimer_name)}</b>` : `by <b style="color:var(--ink-dim)">${esc(reporter)}</b>`;
    const age = t.status === "open" ? fmtAge(t.created_at) + " ago" : fmtAge(t.closed_at || t.created_at);
    return `
      <div class="ticket-item ${cls}" data-ticket-id="${esc(t.id)}">
        <div class="pri"></div>
        <div class="body">
          <div class="subj">${esc(ticketSubject(t))}</div>
          <div class="row">
            ${t.escalated ? '<span class="t-chip open" style="background:var(--gold-soft);color:var(--gold-solid)">Escalated</span>' : ""}
            <span>${extra}</span>
            ${statusChip(t)}
          </div>
        </div>
        <div class="right">
          <span class="id">#${esc(t.id)}</span>
          <span class="age">${age}</span>
        </div>
      </div>
    `;
  }).join("");
}

function renderDetail(t) {
  if (!t) {
    return '<div class="empty">Select a ticket from the queue to view details.</div>';
  }

  const desc = (t.description || "").trim() || "(no description)";
  const reporter = t.user_name || t.user_id || "unknown";
  const when = t.status === "open"
    ? `opened ${fmtAge(t.created_at)} ago`
    : `closed ${fmtAge(t.closed_at || t.created_at)} ago`;
  const userColor = avatarColor(t.user_id || t.user_name);
  const userInit = initial(t.user_name || t.user_id);

  const crumb = `#T-${esc(t.id)} &nbsp;·&nbsp; ${when}${t.escalated ? " &nbsp;·&nbsp; escalated" : ""}`;

  const channelPair = t.channel_id
    ? `<span class="pair"><span class="k">Channel</span><b style="color:var(--gold-solid)">#${esc(t.channel_id)}</b></span>`
    : "";

  const claimedPair = t.claimer_name
    ? `<span class="pair"><span class="k">Claimed by</span><b>${esc(t.claimer_name)}</b></span>`
    : `<span class="pair"><span class="k">Claimed</span><b style="color:var(--ink-mute)">Unclaimed</b></span>`;

  const closeSection = t.status === "closed" ? `
    <div class="td-section">Resolution</div>
    <div class="user-card" style="background:var(--bg-floor)">
      <div class="av" style="background:${avatarColor(t.closed_by || "system")}">${initial(t.closer_name || "·")}</div>
      <div class="info">
        <div class="n">Closed by ${esc(t.closer_name || "system")}</div>
        <div class="m">${fmtTs(t.closed_at)} · ${esc(t.close_reason || "no reason given")}</div>
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
      <div class="msg reported">
        <div class="msg-avatar" style="background:${userColor}">${userInit}</div>
        <div>
          <div class="msg-head">
            <span class="msg-name">${esc(reporter)}</span>
            <span class="msg-time">${fmtTs(t.created_at)}</span>
          </div>
          <div class="msg-text">${esc(desc)}</div>
        </div>
      </div>

      ${closeSection}
    </div>

    <div class="td-actions">
      <button class="act-btn" data-action="transcript">
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M1 11V3a1 1 0 011-1h6l4 4v5a1 1 0 01-1 1H2a1 1 0 01-1-1z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M8 2v4h4" stroke="currentColor" stroke-width="1.5"/></svg>
        View transcript
      </button>
      <span class="act-spacer"></span>
    </div>
  `;
}

const FILTERS = {
  open:   (t) => t.status === "open",
  mine:   (t) => t.status === "open" && t.claimer_id,
  closed: (t) => t.status === "closed",
  all:    () => true,
};

export function mount(container) {
  container.innerHTML = `
    <div class="panel-head">
      <div>
        <div class="kicker">Moderation · Tickets</div>
        <h1 class="panel-title">Active <em>tickets</em></h1>
        <div class="sub" style="font-size:13px;color:var(--ink-dim);margin-top:6px">
          Flagged messages, auto-mod hits, and member reports awaiting review.
        </div>
      </div>
    </div>

    <div class="mod-stats" data-stats>
      <div class="mod-stat open"><div class="lbl">Open</div><div class="v">—</div><div class="sub">loading…</div></div>
      <div class="mod-stat claimed"><div class="lbl">Claimed</div><div class="v">—</div><div class="sub"></div></div>
      <div class="mod-stat resolved"><div class="lbl">Closed</div><div class="v">—</div><div class="sub"></div></div>
      <div class="mod-stat avg"><div class="lbl">Escalated</div><div class="v">—</div><div class="sub"></div></div>
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
  `;

  const statsEl = container.querySelector("[data-stats]");
  const listEl = container.querySelector("[data-list]");
  const detailEl = container.querySelector("[data-detail]");
  const filterGroup = container.querySelector("[data-filter-group]");

  const state = {
    tickets: [],
    filter: "open",
    activeId: null,
  };

  function applyFilterAndRender() {
    const filtered = state.tickets.filter(FILTERS[state.filter]);
    // preserve active selection if still visible, else pick first
    if (!filtered.find((t) => t.id === state.activeId)) {
      state.activeId = filtered[0]?.id ?? null;
    }
    listEl.innerHTML = renderList(filtered, state.activeId);
    const active = state.tickets.find((t) => t.id === state.activeId) || null;
    detailEl.innerHTML = renderDetail(active);
  }

  function renderStats() {
    const open = state.tickets.filter((t) => t.status === "open").length;
    const claimed = state.tickets.filter((t) => t.status === "open" && t.claimer_id).length;
    const closed = state.tickets.filter((t) => t.status === "closed").length;
    const escalated = state.tickets.filter((t) => t.escalated && t.status === "open").length;

    statsEl.innerHTML = `
      <div class="mod-stat open">
        <div class="lbl">Open</div>
        <div class="v">${open}</div>
        <div class="sub">${open ? `${open - claimed} unclaimed` : "all clear"}</div>
      </div>
      <div class="mod-stat claimed">
        <div class="lbl">Claimed</div>
        <div class="v">${claimed}</div>
        <div class="sub">${open ? Math.round((claimed / open) * 100) + "% of open" : "—"}</div>
      </div>
      <div class="mod-stat resolved">
        <div class="lbl">Closed</div>
        <div class="v">${closed}</div>
        <div class="sub">in current window</div>
      </div>
      <div class="mod-stat avg">
        <div class="lbl">Escalated</div>
        <div class="v">${escalated}</div>
        <div class="sub">${escalated ? "needs review" : "none"}</div>
      </div>
    `;
  }

  async function refresh() {
    try {
      const data = await api("/api/moderation/tickets");
      state.tickets = data.tickets || [];
      renderStats();
      applyFilterAndRender();
    } catch (err) {
      listEl.innerHTML = `<div class="error" style="padding:20px">${esc(err.message)}</div>`;
      detailEl.innerHTML = "";
    }
  }

  filterGroup.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-filter]");
    if (!btn) return;
    filterGroup.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
    state.filter = btn.dataset.filter;
    state.activeId = null;
    applyFilterAndRender();
  });

  listEl.addEventListener("click", (e) => {
    const row = e.target.closest(".ticket-item");
    if (!row) return;
    state.activeId = row.dataset.ticketId;
    applyFilterAndRender();
  });

  detailEl.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-action='transcript']");
    if (btn && state.activeId) {
      showTranscript("ticket", state.activeId);
    }
  });

  refresh();

  return { unmount() {} };
}
