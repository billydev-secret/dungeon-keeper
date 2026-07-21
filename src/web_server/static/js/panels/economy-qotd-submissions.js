// Economy — Sponsored QOTD. The paid question queue: pending submissions to
// approve or decline, the approved ones waiting on `/qotd post` (withdrawable),
// and a state filter for the history. Mirrors the bank-channel review card's
// buttons. Gated by the economy manager role (or admin).
import { api, apiPost, esc, fmtAge } from "../api.js";
import { showStatus, loadMembers } from "../config-helpers.js";
import { promptDialog } from "../ui.js";
import { makeFilterStrip } from "../tab-strip.js";

// History states only — pending and approved get their own tables above.
const STATES = [
  ["posted", "Posted"],
  ["denied", "Declined"],
  ["expired", "Expired"],
  ["", "All"],
];

function nowSec() { return Date.now() / 1000; }

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading Sponsored QOTD…</div></div>`;
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
        <h2>Sponsored QOTD</h2>
        <div class="subtitle">Paid questions — approve what runs, decline to refund</div>
      </header>

      <section class="card">
        <div class="section-label">Awaiting Review</div>
        <div class="field-hint" style="margin-bottom:8px;">Declining refunds the sponsor automatically.</div>
        <div data-pending><div class="empty">Loading…</div></div>
      </section>

      <section class="card">
        <div class="section-label">Ready to Post</div>
        <div class="field-hint" style="margin-bottom:8px;">Queued oldest-first; the next <code>/qotd post</code> takes the top one. Withdrawing refunds it.</div>
        <div data-approved><div class="empty">Loading…</div></div>
      </section>

      <section class="card">
        <div class="section-label">History</div>
        <div class="ctrl-group" role="group" aria-label="Filter submissions" data-filter-group style="margin-bottom:10px;">
          ${STATES.map(([v, label], i) =>
            `<button${i === 0 ? ` class="active"` : ""} data-filter="${v}">${label}</button>`).join("")}
        </div>
        <div data-history><div class="empty">Loading…</div></div>
      </section>
    </div>`;

  let history = "posted";
  makeFilterStrip(container.querySelector("[data-filter-group]"), (value) => {
    history = value;
    refreshHistory(container, members, history);
  });
  const refreshAll = () => {
    refreshQueues(container, members, refreshAll);
    refreshHistory(container, members, history);
  };
  refreshAll();
}

function questionCell(s) {
  return `<span title="${esc(s.question)}">${esc(s.question)}</span>`;
}

async function fetchSubmissions(state) {
  return (await api("/api/economy/qotd-submissions", state ? { state } : {})).submissions;
}

function errorBox(host, err) {
  host.innerHTML = `<div class="error">${esc(err.message)}</div>`;
}

async function refreshQueues(container, members, refreshAll) {
  const pendingHost = container.querySelector("[data-pending]");
  const approvedHost = container.querySelector("[data-approved]");
  let pending, approved;
  try {
    [pending, approved] = await Promise.all([
      fetchSubmissions("pending"),
      fetchSubmissions("approved"),
    ]);
  } catch (err) {
    errorBox(pendingHost, err);
    errorBox(approvedHost, err);
    return;
  }
  renderPending(pendingHost, members, pending, refreshAll);
  renderApproved(approvedHost, members, approved, refreshAll);
}

function renderPending(host, members, rows, refreshAll) {
  if (!rows.length) {
    host.innerHTML = `<div class="empty">Nothing waiting on you.</div>`;
    return;
  }
  const body = rows.map((s) => `
    <tr>
      <td>${esc(memberName(members, s.user_id))}</td>
      <td>${questionCell(s)}</td>
      <td>${s.price}</td>
      <td>${fmtAge(nowSec() - (s.created_at || 0))}</td>
      <td>
        <button class="btn btn-primary btn-sm" data-approve="${s.id}">Approve</button>
        <button class="btn btn-ghost btn-sm" data-deny="${s.id}">Decline</button>
        <span class="save-status" data-sub-status="${s.id}"></span>
      </td>
    </tr>`).join("");
  host.innerHTML = `
    <div style="overflow-x:auto;">
      <table class="data-table">
        <thead><tr><th>Sponsor</th><th>Question</th><th>Paid</th><th>Age</th><th></th></tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;

  host.querySelectorAll("[data-approve]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.approve;
      const status = host.querySelector(`[data-sub-status="${id}"]`);
      try {
        await apiPost(`/api/economy/qotd-submissions/${id}/approve`, {});
        showStatus(status, true, "Queued");
        refreshAll();
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  });
  host.querySelectorAll("[data-deny]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.deny;
      const status = host.querySelector(`[data-sub-status="${id}"]`);
      const reason = await promptDialog("Why? (shown to the sponsor, who is refunded):", { confirmLabel: "Decline", required: true, danger: true });
      if (reason == null) return;
      try {
        await apiPost(`/api/economy/qotd-submissions/${id}/deny`, { reason });
        showStatus(status, true, "Declined + refunded");
        refreshAll();
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  });
}

function renderApproved(host, members, rows, refreshAll) {
  if (!rows.length) {
    host.innerHTML = `<div class="empty">No sponsored questions queued.</div>`;
    return;
  }
  const body = rows.map((s, i) => `
    <tr>
      <td>${i + 1}</td>
      <td>${esc(memberName(members, s.user_id))}</td>
      <td>${questionCell(s)}</td>
      <td>${s.price}</td>
      <td>
        <button class="btn btn-ghost btn-sm" data-withdraw="${s.id}">Withdraw</button>
        <span class="save-status" data-sub-status="${s.id}"></span>
      </td>
    </tr>`).join("");
  host.innerHTML = `
    <div style="overflow-x:auto;">
      <table class="data-table">
        <thead><tr><th>#</th><th>Sponsor</th><th>Question</th><th>Paid</th><th></th></tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;

  host.querySelectorAll("[data-withdraw]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.withdraw;
      const status = host.querySelector(`[data-sub-status="${id}"]`);
      // Cancel returns null; an empty string is a deliberate no-reason withdraw.
      const reason = await promptDialog("Pull this back out of the queue and refund the sponsor. Reason (optional, shown to them):", { confirmLabel: "Withdraw", danger: true });
      if (reason == null) return;
      try {
        await apiPost(`/api/economy/qotd-submissions/${id}/withdraw`, { reason });
        showStatus(status, true, "Withdrawn + refunded");
        refreshAll();
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  });
}

function statusCell(s, members) {
  const ago = s.resolved_at ? ` · ${fmtAge(nowSec() - s.resolved_at)} ago` : "";
  const by = s.resolver_id ? ` by ${esc(memberName(members, s.resolver_id))}` : "";
  if (s.state === "posted") return `<span class="badge">posted</span>${ago}`;
  if (s.state === "denied") {
    const reason = s.deny_reason ? ` · ${esc(s.deny_reason)}` : "";
    return `<span class="badge badge-warning">declined</span>${by}${ago}${reason}`;
  }
  return `<span class="badge badge-dim">${esc(s.state)}</span>${by}${ago}`;
}

async function refreshHistory(container, members, state) {
  const host = container.querySelector("[data-history]");
  let rows;
  try {
    rows = await fetchSubmissions(state);
  } catch (err) {
    errorBox(host, err);
    return;
  }
  if (!rows.length) {
    host.innerHTML = `<div class="empty">Nothing here yet.</div>`;
    return;
  }
  const body = rows.map((s) => `
    <tr>
      <td>${esc(memberName(members, s.user_id))}</td>
      <td>${questionCell(s)}</td>
      <td>${s.price}</td>
      <td>${statusCell(s, members)}</td>
    </tr>`).join("");
  host.innerHTML = `
    <div style="overflow-x:auto;">
      <table class="data-table">
        <thead><tr><th>Sponsor</th><th>Question</th><th>Paid</th><th>Status</th></tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}
