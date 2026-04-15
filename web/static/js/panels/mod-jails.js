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

function fmtDuration(seconds) {
  const s = Math.round(seconds);
  if (s <= 0) return "<1m";
  if (s < 60) return "<1m";
  const parts = [];
  if (s >= 86400) parts.push(Math.floor(s / 86400) + "d");
  if (s % 86400 >= 3600) parts.push(Math.floor((s % 86400) / 3600) + "h");
  if (s % 3600 >= 60 && parts.length < 2) parts.push(Math.floor((s % 3600) / 60) + "m");
  return parts.join(" ") || "<1m";
}

function fmtRemaining(expiresAt) {
  if (!expiresAt) return "Indefinite";
  const remaining = expiresAt - Date.now() / 1000;
  if (remaining <= 0) return "Expiring…";
  return fmtDuration(remaining);
}

function fmtShortDate(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { day: "2-digit", month: "short" });
}

function jailSubject(j) {
  const r = (j.reason || "").trim();
  if (r) return r.length > 80 ? r.slice(0, 77) + "…" : r;
  return `Jail #${j.id}`;
}

function priorityClass(j) {
  if (j.status !== "active") return "low";
  if (j.expires_at) {
    const remaining = j.expires_at - Date.now() / 1000;
    if (remaining <= 3600) return "high";
  }
  return "med";
}

function statusChip(j) {
  if (j.status === "released") return '<span class="t-chip closed">Released</span>';
  return '<span class="t-chip open">Active</span>';
}

function renderList(jails, activeId) {
  if (!jails.length) {
    return '<div class="empty">No jails match this filter.</div>';
  }
  return jails.map((j) => {
    const cls = priorityClass(j) + (j.id === activeId ? " active" : "");
    const subj = j.user_name || j.user_id || "unknown";
    const byMod = j.moderator_name
      ? `by <b style="color:var(--ink-dim)">${esc(j.moderator_name)}</b>`
      : "";
    const ageOrRemaining = j.status === "active"
      ? fmtRemaining(j.expires_at) + " left"
      : fmtAge(j.released_at || j.created_at) + " ago";
    return `
      <div class="ticket-item ${cls}" data-jail-id="${esc(j.id)}">
        <div class="pri"></div>
        <div class="body">
          <div class="subj">${esc(jailSubject(j))}</div>
          <div class="row">
            <span>${esc(subj)}</span>
            <span class="dot">·</span>
            ${byMod}
            ${statusChip(j)}
          </div>
        </div>
        <div class="right">
          <span class="id">#J-${esc(j.id)}</span>
          <span class="age">${esc(ageOrRemaining)}</span>
        </div>
      </div>
    `;
  }).join("");
}

const ICON_DOC = `<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M1 11V3a1 1 0 011-1h6l4 4v5a1 1 0 01-1 1H2a1 1 0 01-1-1z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M8 2v4h4" stroke="currentColor" stroke-width="1.5"/></svg>`;

function renderActions(j) {
  if (!j) return "";
  return `
    <div class="td-actions">
      <span class="act-spacer"></span>
      <button class="act-btn" data-action="transcript" title="View transcript">${ICON_DOC}Transcript</button>
    </div>
  `;
}

function renderSubjectCard(j) {
  const name = j.user_name || j.user_id || "unknown";
  const color = avatarColor(j.user_id || j.user_name);
  const init = initial(j.user_name || j.user_id);
  const idSuffix = j.user_id
    ? `<span style="color:var(--ink-mute);font-weight:500;font-family:var(--mono);font-size:11px">· ${esc(j.user_id)}</span>`
    : "";
  const jailedLine = `Jailed ${fmtTs(j.created_at)}`;
  return `
    <div class="user-card">
      <div class="av" style="background:${color}">${esc(init)}</div>
      <div class="info">
        <div class="n">${esc(name)} ${idSuffix}</div>
        <div class="m">${esc(jailedLine)}</div>
      </div>
    </div>
  `;
}

function renderReleaseCard(j) {
  if (j.status !== "released") return "";
  const modName = j.moderator_name || "system";
  const color = avatarColor(modName);
  const when = fmtTs(j.released_at);
  const reason = (j.release_reason || "").trim() || "timer expired";
  return `
    <div class="td-section">Release</div>
    <div class="user-card" style="background:var(--bg-floor)">
      <div class="av" style="background:${color}">${esc(initial(modName))}</div>
      <div class="info">
        <div class="n">Released</div>
        <div class="m">${esc(when)} · ${esc(reason)}</div>
      </div>
    </div>
  `;
}

function renderDetail(j) {
  if (!j) {
    return '<div class="empty">Select a jail from the queue to view details.</div>';
  }

  const when = j.status === "active"
    ? `opened ${fmtAge(j.created_at)} ago`
    : `released ${fmtAge(j.released_at || j.created_at)} ago`;
  const crumb = `#J-${esc(j.id)} &nbsp;·&nbsp; ${esc(when)}`;

  const durationLabel = j.expires_at
    ? fmtDuration(j.expires_at - j.created_at)
    : "Indefinite";
  const remainingPair = j.status === "active"
    ? `<span class="pair"><span class="k">Remaining</span><b>${esc(fmtRemaining(j.expires_at))}</b></span>`
    : "";

  const subjectName = j.user_name || j.user_id || "unknown";
  const modName = j.moderator_name || j.moderator_id || "system";

  const reasonText = (j.reason || "").trim() || "(no reason given)";

  return `
    <div class="td-head">
      <div class="td-crumb">${crumb}</div>
      <h3 class="td-title">${esc(jailSubject(j))}</h3>
      <div class="td-meta">
        <span class="pair"><span class="k">Subject</span><b>${esc(subjectName)}</b></span>
        <span class="pair"><span class="k">Moderator</span><b>${esc(modName)}</b></span>
        <span class="pair"><span class="k">Duration</span><b>${esc(durationLabel)}</b></span>
        ${remainingPair}
        <span class="pair"><span class="k">Status</span>${statusChip(j)}</span>
      </div>
    </div>

    <div class="td-body">
      <div class="td-section">Reason</div>
      <div style="font-size:14px;color:var(--ink);line-height:1.5;white-space:pre-wrap;word-break:break-word;padding:4px 8px 8px">${esc(reasonText)}</div>

      <div class="td-section">Subject</div>
      ${renderSubjectCard(j)}

      ${renderReleaseCard(j)}
    </div>

    ${renderActions(j)}
  `;
}

const FILTERS = {
  active:   (j) => j.status === "active",
  released: (j) => j.status === "released",
  all:      () => true,
};

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <div class="panel-head">
        <div>
          <h1 class="panel-title">Jails <em>&amp; holds</em></h1>
          <div class="sub" style="font-size:13px;color:var(--ink-dim);margin-top:6px">
            Members currently restricted from posting, plus recent releases.
          </div>
        </div>
      </div>

      <div class="mod-stats" data-stats>
        <div class="mod-stat open"><div class="lbl">Currently jailed</div><div class="v">—</div><div class="sub">loading…</div></div>
        <div class="mod-stat claimed"><div class="lbl">Ending soon</div><div class="v">—</div><div class="sub"></div></div>
        <div class="mod-stat resolved"><div class="lbl">Released</div><div class="v">—</div><div class="sub"></div></div>
        <div class="mod-stat avg"><div class="lbl">Total</div><div class="v">—</div><div class="sub"></div></div>
      </div>

      <section class="mod-split">
        <div class="ticket-list-wrap">
          <div class="ticket-list-head">
            <h3>Queue</h3>
            <div class="ctrl-group" role="tablist" data-filter-group>
              <button class="active" data-filter="active">Active</button>
              <button data-filter="released">Released</button>
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

  const statsEl = container.querySelector("[data-stats]");
  const listEl = container.querySelector("[data-list]");
  const detailEl = container.querySelector("[data-detail]");
  const filterGroup = container.querySelector("[data-filter-group]");

  const state = {
    jails: [],
    filter: "active",
    activeId: null,
  };

  function render() {
    const filtered = state.jails.filter(FILTERS[state.filter]);
    if (!filtered.find((j) => j.id === state.activeId)) {
      state.activeId = filtered[0]?.id ?? null;
    }
    listEl.innerHTML = renderList(filtered, state.activeId);
    const active = state.jails.find((j) => j.id === state.activeId) || null;
    detailEl.innerHTML = renderDetail(active);
  }

  function renderStats() {
    const now = Date.now() / 1000;
    const active = state.jails.filter((j) => j.status === "active");
    const endingSoon = active.filter((j) => j.expires_at && j.expires_at - now <= 3600).length;
    const released = state.jails.filter((j) => j.status === "released").length;
    const total = state.jails.length;

    statsEl.innerHTML = `
      <div class="mod-stat open">
        <div class="lbl">Currently jailed</div>
        <div class="v">${active.length}</div>
        <div class="sub">${active.length ? "active holds" : "all clear"}</div>
      </div>
      <div class="mod-stat claimed">
        <div class="lbl">Ending soon</div>
        <div class="v">${endingSoon}</div>
        <div class="sub">${endingSoon ? "within 1h" : "none"}</div>
      </div>
      <div class="mod-stat resolved">
        <div class="lbl">Released</div>
        <div class="v">${released}</div>
        <div class="sub">in current window</div>
      </div>
      <div class="mod-stat avg">
        <div class="lbl">Total</div>
        <div class="v">${total}</div>
        <div class="sub">last 200 records</div>
      </div>
    `;
  }

  async function refresh() {
    try {
      const data = await api("/api/moderation/jails");
      state.jails = data.jails || [];
      renderStats();
      render();
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
    render();
  });

  listEl.addEventListener("click", (e) => {
    const row = e.target.closest(".ticket-item");
    if (!row) return;
    state.activeId = Number(row.dataset.jailId);
    render();
  });

  detailEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".act-btn");
    if (!btn || btn.disabled) return;
    if (btn.dataset.action === "transcript" && state.activeId) {
      showTranscript("jail", state.activeId);
    }
  });

  refresh();

  return { unmount() {} };
}
