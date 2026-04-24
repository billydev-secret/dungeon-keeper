import { api, esc } from "../api.js";

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

function renderList(warnings, activeId) {
  if (!warnings.length) {
    return '<div class="empty">No warnings found.</div>';
  }
  return warnings.map((w) => {
    const badge = w.revoked
      ? '<span class="badge badge-dim">Revoked</span>'
      : '<span class="badge badge-warning">Active</span>';
    const cls = w.id === activeId ? " active" : "";
    const name = esc(w.user_name || w.user_id || "unknown");
    const age = fmtAge(w.created_at) + " ago";
    const reason = (w.reason || "").trim();
    const preview = reason ? (reason.length > 60 ? reason.slice(0, 57) + "…" : reason) : "(no reason)";
    return `
      <div class="ticket-item low${cls}" data-warn-id="${esc(w.id)}">
        <div class="pri"></div>
        <div class="body">
          <div class="subj">${esc(preview)}</div>
          <div class="row">
            <span>${name}</span>
            ${badge}
          </div>
        </div>
        <div class="right">
          <span class="id">#W-${esc(w.id)}</span>
          <span class="age">${esc(age)}</span>
        </div>
      </div>
    `;
  }).join("");
}

function renderDetail(w) {
  if (!w) {
    return '<div class="empty">Select a warning from the list to view details.</div>';
  }

  const badge = w.revoked
    ? '<span class="badge badge-dim">Revoked</span>'
    : '<span class="badge badge-warning">Active</span>';
  const reasonText = (w.reason || "").trim() || "(no reason given)";
  const userName = w.user_name || w.user_id || "unknown";
  const modName = w.moderator_name || w.moderator_id || "unknown";

  const revokeSection = w.revoked ? `
    <div class="td-section">Revocation</div>
    <div style="font-size:14px;color:var(--ink);line-height:1.5;padding:4px 8px 8px">
      <div><b>Revoked by:</b> ${esc(w.revoker_name || w.revoked_by || "unknown")}</div>
      <div><b>At:</b> ${esc(fmtTs(w.revoked_at))}</div>
      ${w.revoke_reason ? `<div><b>Reason:</b> ${esc(w.revoke_reason)}</div>` : ""}
    </div>
  ` : "";

  return `
    <div class="td-head">
      <div class="td-crumb">#W-${esc(w.id)} &nbsp;·&nbsp; issued ${esc(fmtAge(w.created_at))} ago</div>
      <h3 class="td-title">Warning for <em>${esc(userName)}</em></h3>
      <div class="td-meta">
        <span class="pair"><span class="k">User</span><b>${esc(userName)}</b></span>
        <span class="pair"><span class="k">Issued by</span><b>${esc(modName)}</b></span>
        <span class="pair"><span class="k">Date</span><b>${esc(fmtTs(w.created_at))}</b></span>
        <span class="pair"><span class="k">Status</span>${badge}</span>
      </div>
    </div>

    <div class="td-body">
      <div class="td-section">Warning</div>
      <div style="font-size:14px;color:var(--ink);line-height:1.5;white-space:pre-wrap;word-break:break-word;padding:4px 8px 8px">${esc(reasonText)}</div>
      ${revokeSection}
    </div>
  `;
}

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <div class="panel-head">
        <div>
          <h1 class="panel-title">Warnings</h1>
          <div class="sub" style="font-size:13px;color:var(--ink-dim);margin-top:6px">
            Active and expired warnings for server members.
          </div>
        </div>
      </div>

      <div class="mod-stats" data-stats></div>

      <div class="controls" style="padding:8px 16px 0">
        <label><input type="checkbox" data-control="active-only"> Active only</label>
      </div>

      <section class="mod-split">
        <div class="ticket-list-wrap">
          <div class="ticket-list-head">
            <h3>Queue</h3>
          </div>
          <div class="ticket-list" data-list>
            <div class="empty">Loading…</div>
          </div>
        </div>

        <div class="ticket-detail" data-detail>
          <div class="empty">Select a warning from the list to view details.</div>
        </div>
      </section>
    </div>
  `;

  const activeOnlyEl = container.querySelector('[data-control="active-only"]');
  const statsEl = container.querySelector("[data-stats]");
  const listEl = container.querySelector("[data-list]");
  const detailEl = container.querySelector("[data-detail]");

  const state = {
    warnings: [],
    activeId: null,
  };

  function visibleWarnings() {
    if (activeOnlyEl.checked) return state.warnings.filter((w) => !w.revoked);
    return state.warnings;
  }

  function render() {
    const visible = visibleWarnings();
    if (!visible.find((w) => w.id === state.activeId)) {
      state.activeId = visible[0]?.id ?? null;
    }
    listEl.innerHTML = renderList(visible, state.activeId);
    const active = state.warnings.find((w) => w.id === state.activeId) || null;
    detailEl.innerHTML = renderDetail(active);
  }

  async function refresh() {
    try {
      const data = await api("/api/moderation/warnings");

      statsEl.innerHTML = `
        <div class="stat-card stat-warning"><div class="stat-value">${data.active_count}</div><div class="stat-label">Active</div></div>
        <div class="stat-card"><div class="stat-value">${data.total_count}</div><div class="stat-label">Total</div></div>
      `;

      state.warnings = data.warnings || [];
      render();
    } catch (err) {
      listEl.innerHTML = `<div class="error" style="padding:20px">${esc(err.message)}</div>`;
      detailEl.innerHTML = "";
    }
  }

  activeOnlyEl.addEventListener("change", () => {
    state.activeId = null;
    render();
  });

  listEl.addEventListener("click", (e) => {
    const row = e.target.closest(".ticket-item");
    if (!row) return;
    state.activeId = Number(row.dataset.warnId);
    render();
  });

  refresh();

  return { unmount() {} };
}
