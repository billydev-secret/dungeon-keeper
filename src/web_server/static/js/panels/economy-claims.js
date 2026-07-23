// Economy — Claims. The sign-off inbox: pending quest claims to approve or
// deny, plus a state filter for the resolved history (approved / denied /
// expired). Approve/Deny resolves the same claim as the bank-channel card
// buttons. Gated by the economy manager role (or admin).
import { api, apiPost, esc, fmtAge } from "../api.js";
import { showStatus, loadMembers } from "../config-helpers.js";
import { promptDialog } from "../ui.js";
import { makeFilterStrip } from "../tab-strip.js";

// Claim lifecycle states as stored (an approved claim is 'paid').
const STATES = [
  ["pending", "Pending"],
  ["paid", "Paid"],
  ["denied", "Denied"],
  ["expired", "Expired"],
  ["", "All"],
];

function nowSec() { return Date.now() / 1000; }

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading claims…</div></div>`;
  (async () => {
    const members = await loadMembers().catch(() => []);
    render(container, members);
  })();
  return null;
}

function memberName(members, id) {
  const m = members.find((x) => String(x.id) === String(id));
  return m ? (m.display_name || m.name) : String(id);
}

function render(container, members) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Claims</h2>
        <div class="subtitle">Quests members say they have finished, waiting on your
          approval before they are paid</div>
      </header>

      <section class="card">
        <div class="ctrl-group" role="group" aria-label="Filter claims" data-filter-group style="margin-bottom:10px;">
          ${STATES.map(([v, label], i) =>
            `<button${i === 0 ? ` class="active"` : ""} data-filter="${v}">${label}</button>`).join("")}
        </div>
        <div data-claims><div class="empty">Loading…</div></div>
      </section>
    </div>`;

  let state = "pending";
  makeFilterStrip(container.querySelector("[data-filter-group]"), (value) => {
    state = value;
    refreshClaims(container, members, state);
  });
  refreshClaims(container, members, state);
}

function statusCell(c, members) {
  if (c.state === "pending") {
    const denies = c.deny_count
      ? ` · turned down ${c.deny_count} time${c.deny_count === 1 ? "" : "s"} before`
      : "";
    return `Waiting${denies}`;
  }
  const ago = c.resolved_at ? ` · ${fmtAge(nowSec() - c.resolved_at)} ago` : "";
  if (c.state === "paid") {
    const by = c.resolver_id ? ` by ${esc(memberName(members, c.resolver_id))}` : "";
    return `<span class="badge">Paid</span>${by}${ago}`;
  }
  if (c.state === "denied") {
    const reason = c.deny_reason ? ` · <span title="${esc(c.deny_reason)}">${esc(c.deny_reason)}</span>` : "";
    return `<span class="badge badge-warning">Turned down</span>${ago}${reason}`;
  }
  return `<span class="badge badge-dim">${esc(c.state)}</span>${ago}`;
}

async function refreshClaims(container, members, state) {
  const host = container.querySelector("[data-claims]");
  let claims;
  try {
    const params = state ? { state } : {};
    claims = (await api("/api/economy/claims", params)).claims;
  } catch (err) {
    host.innerHTML = `<div class="error">The claims list failed to load: ${esc(err.message)}</div>`;
    return;
  }
  if (!claims.length) {
    host.innerHTML = `<div class="empty">${state === "pending"
      ? "Nothing is waiting for approval right now."
      : "No claims match this filter."}</div>`;
    return;
  }
  const rows = claims.map((c) => `
    <tr data-claim-row="${c.id}">
      <td>${esc(memberName(members, c.user_id))}</td>
      <td>${esc(c.quest_title || "#" + c.quest_id)}</td>
      <td>${esc(c.period || "")}</td>
      <td>${fmtAge(nowSec() - (c.created_at || 0))}</td>
      <td>${statusCell(c, members)}</td>
      <td>${c.state === "pending" ? `
        <button class="btn btn-primary btn-sm" data-approve="${c.id}">Approve</button>
        <button class="btn btn-ghost btn-sm" data-deny="${c.id}">Turn Down</button>
        <span class="save-status" data-claim-status="${c.id}"></span>` : ""}
      </td>
    </tr>`).join("");
  host.innerHTML = `
    <div style="overflow-x:auto;">
      <table class="data-table">
        <thead><tr><th>Claimant</th><th>Quest</th><th>Period</th><th>Age</th><th>Status</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;

  host.querySelectorAll("[data-approve]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.approve;
      const status = host.querySelector(`[data-claim-status="${id}"]`);
      try {
        const res = await apiPost(`/api/economy/claims/${id}/approve`, {});
        showStatus(status, true, `Paid ${res.paid}`);
        refreshClaims(container, members, state);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  });
  host.querySelectorAll("[data-deny]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.deny;
      const status = host.querySelector(`[data-claim-status="${id}"]`);
      const reason = await promptDialog(
        "The member is not paid for this quest and is shown your reason. What should they be told?",
        { title: "Turn down this claim?", confirmLabel: "Turn Down", required: true, danger: true },
      );
      if (reason == null) return;
      try {
        await apiPost(`/api/economy/claims/${id}/deny`, { reason });
        showStatus(status, true, "Turned down");
        refreshClaims(container, members, state);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  });
}
