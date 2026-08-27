import { api, apiPost, apiDelete, esc } from "../api.js";
import { renderLoading, renderEmpty, renderError } from "../states.js";
import { fmtTs } from "../audit-helpers.js";
import { toast, promptDialog, confirmDialog, bindRowActivation } from "../ui.js";

// Read straight off the session blob the way mod-tickets.js does, rather than
// importing config-helpers.js — this panel needs one boolean, not the config
// machinery. Enforcement is the server's: delete is behind require_perms
// ({"admin"}), so hiding the button only spares a moderator a guaranteed 403.
function viewerIsAdmin() {
  const perms = window.__dk_user?.perms;
  return !!(perms && typeof perms.has === "function" && perms.has("admin"));
}

function fmtAge(ts) {
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
  return Math.floor(s / 86400) + "d";
}

function renderList(warnings, activeId, activeOnly) {
  if (!warnings.length) {
    return renderEmpty(activeOnly
      ? "No active warnings. Every warning on record has been revoked — untick Active Only to see them."
      : "No warnings on record. Warnings a moderator issues from Discord or from a ticket land here.");
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
      <div class="ticket-item low${cls}" data-warn-id="${esc(w.id)}"
           tabindex="0" role="button" aria-current="${w.id === activeId ? "true" : "false"}">
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

function renderActions(w) {
  // A revoked warning has no Revoke button rather than a disabled one: the
  // endpoint 409s on a second revoke, and the Revocation block right above
  // already says it happened. Delete stays — a wrongly-issued warning is
  // still wrong after it's been revoked.
  const revokeBtn = w.revoked
    ? ""
    : `<button class="act-btn primary" data-action="revoke">Revoke</button>`;
  const deleteBtn = viewerIsAdmin()
    ? `<button class="act-btn danger" data-action="delete">Delete</button>`
    : "";
  if (!revokeBtn && !deleteBtn) return "";
  return `
    <div class="td-act-groups">
      <div class="td-act-group">
        <div class="section-label">This warning</div>
        <div class="td-actions">
          ${revokeBtn}
          ${deleteBtn}
        </div>
      </div>
    </div>
  `;
}

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
      ${renderActions(w)}
    </div>
  `;
}

export function mount(container) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Warnings</h2>
        <div class="subtitle">Active and revoked warnings for server members.</div>
      </header>

      <div class="mod-stats" data-stats></div>

      <div class="controls" style="padding:8px 16px 0">
        <label><input type="checkbox" data-control="active-only"> Active Only</label>
      </div>

      <section class="mod-split">
        <div class="ticket-list-wrap">
          <div class="ticket-list-head">
            <h3>Queue</h3>
          </div>
          <div class="ticket-list" data-list>
            ${renderLoading("Loading warnings…")}
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
    listEl.innerHTML = renderList(visible, state.activeId, activeOnlyEl.checked);
    const active = state.warnings.find((w) => w.id === state.activeId) || null;
    detailEl.innerHTML = renderDetail(active);
  }

  async function refresh() {
    try {
      const data = await api("/api/moderation/warnings");

      statsEl.innerHTML = `
        <div class="stat stat-warning"><div class="stat-value">${data.active_count}</div><div class="stat-label">Active</div></div>
        <div class="stat"><div class="stat-value">${data.total_count}</div><div class="stat-label">Total</div></div>
      `;

      state.warnings = data.warnings || [];
      render();
    } catch (err) {
      listEl.innerHTML = renderError(`Couldn't load warnings — ${err.message}. Reload the page to try again.`);
      detailEl.innerHTML = "";
    }
  }

  activeOnlyEl.addEventListener("change", () => {
    state.activeId = null;
    render();
  });

  bindRowActivation(listEl, ".ticket-item", (row) => {
    state.activeId = Number(row.dataset.warnId);
    render();
  });

  // Runs one warning action. Returns a success message, or undefined if the
  // moderator cancelled (promptDialog/confirmDialog resolve null/false).
  async function runAction(action, w) {
    if (action === "revoke") {
      const reason = await promptDialog("Reason for revoking? (optional)", {
        title: `Revoke warning #W-${w.id}`, confirmLabel: "Revoke",
      });
      if (reason === null) return;
      const res = await apiPost(`/api/moderation/warnings/${encodeURIComponent(w.id)}/revoke`, { reason });
      return res.message || "Warning revoked";
    }
    if (action === "delete") {
      const who = w.user_name || w.user_id || "this member";
      const ok = await confirmDialog(
        `Delete warning #W-${w.id} for ${who}? This erases it permanently — revoking keeps the record instead.`,
        { title: "Delete Warning", confirmLabel: "Delete", danger: true },
      );
      if (!ok) return;
      const res = await apiDelete(`/api/moderation/warnings/${encodeURIComponent(w.id)}`);
      return res.message || "Warning deleted";
    }
    throw new Error(`Unknown action: ${action}`);
  }

  detailEl.addEventListener("click", async (e) => {
    const btn = e.target.closest(".act-btn");
    if (!btn || btn.disabled) return;
    const action = btn.dataset.action;
    if (!action) return;
    const w = state.warnings.find((x) => x.id === state.activeId);
    if (!w) return;

    btn.disabled = true;
    try {
      const msg = await runAction(action, w);
      if (msg) {
        toast(msg);
        // A deleted warning is gone from the list, so let render() pick the
        // next one rather than hunting for an id that no longer exists.
        if (action === "delete") state.activeId = null;
        await refresh();
      }
    } catch (err) {
      console.error(`Warning action "${action}" failed:`, err);
      toast(err.message, "error");
    } finally {
      btn.disabled = false;
    }
  });

  refresh();

  return { unmount() {} };
}
