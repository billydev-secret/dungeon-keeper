// Economy — QOTD. One page for the whole feature: the ping role that opens a
// question, and the paid queue of sponsored ones (pending submissions to
// approve or decline, the approved ones waiting on `/qotd post`, withdrawable,
// and a state filter for the history). Mirrors the bank-channel review card's
// buttons. Gated by the economy manager role (or admin).
//
// The settings half absorbed the retired `economy-qotd` page in 2026-08 (IA2):
// it was an 88-line panel owning one role id, which never earned a nav slot of
// its own. That page was adminOnly and this one is manager-visible, so the
// settings card renders only for admins — probed the way Income Sources does
// it, by whether the admin-gated config GET came back. Visibility is therefore
// unchanged for both audiences. `MOVED_PAGES` redirects the old deep link.
import { api, apiPost, apiPut, esc, fmtAge } from "../api.js";
import { showStatus, loadMembers, loadRoles, mountRolePicker, mountAsync } from "../config-helpers.js";
import { promptDialog } from "../ui.js";
import { mountRoleDialStates } from "../role-dial-state.js";
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
  container.innerHTML = `<div class="panel"><div class="empty">Loading QOTD…</div></div>`;
  return mountAsync(container, async () => {
    // Admin probe: the config GET is admin-gated, so a rejection means a
    // manager-role holder, who gets the queue alone. Roles ride the same
    // branch — only the settings card uses them.
    const [membersR, cfgR, rolesR] = await Promise.allSettled([
      loadMembers(), api("/api/economy/config"), loadRoles(),
    ]);
    const members = membersR.status === "fulfilled" ? membersR.value : [];
    const cfg = cfgR.status === "fulfilled" ? cfgR.value : null;
    const roles = rolesR.status === "fulfilled" ? rolesR.value : [];
    render(container, members, cfg, roles);
  }, { errorMsg: "Couldn’t load QOTD." });
}

function memberName(members, id) {
  const m = members.find((x) => String(x.id) === String(id));
  return m ? (m.display_name || m.name) : String(id);
}

function settingsMarkup(cfg) {
  if (!cfg) return "";
  const unit = cfg.reward_qotd === 1 ? cfg.currency_name : cfg.currency_plural;
  return `
      <form class="form card" data-form>
        <div class="section-label">The QOTD role</div>
        <div class="field">
          <label>QOTD role</label>
          <span data-picker="qotd_ping_role_id"></span>
          <div class="field-hint">Does two jobs. The bot mentions it when a mod runs
            <code>/qotd post</code>, <strong>and</strong> any message from a mod that
            tags it becomes that day's question — so a mod can just ask in their own
            words. Leave as <em>(none)</em> to post silently and turn tag-to-ask off.</div>
          <div class="field-hint">Restrict who may mention it in Discord's role
            settings — a member tagging it does nothing here either way, since only
            admins and the manager role can open a question.</div>
          <div class="field-hint">The role must be <strong>mentionable</strong> in
            Discord's role settings — otherwise the mention posts as plain text and
            nobody is notified. (Granting the bot “Mention @everyone, @here, and All
            Roles” also works.)</div>
          <div data-role-state="econ_qotd_ping_role_id"></div>
        </div>
        <div style="display:flex; gap:8px; align-items:center; margin-top:16px;">
          <button type="submit" class="btn btn-primary">Save</button>
          <span data-status></span>
        </div>
      </form>

      <section class="card">
        <div class="section-label">How It Works</div>
        <div class="field-hint">
          A mod asks the question two ways: type it normally and <strong>tag the QOTD
          role</strong> in the message, or run <code>/qotd post &lt;question&gt;</code>
          to have the bot render it as a card (that path also posts the queued
          sponsored questions). Either way, every member who <strong>replies to that
          message</strong> earns <strong>${cfg.reward_qotd}</strong> ${unit}, once per
          question. Replies stop paying once the guild-local day rolls over, so
          yesterday's question can't be farmed. Change that award on
          <a href="#/economy-income-sources">Income Sources</a>. Who may open a question
          is the manager role on <a href="#/economy-config">Settings</a>.
        </div>
      </section>`;
}

function wireSettings(container, cfg, roles) {
  const form = container.querySelector("[data-form]");
  if (!form) return;
  const status = form.querySelector("[data-status]");
  const pingRolePicker = mountRolePicker(
    form.querySelector('[data-picker="qotd_ping_role_id"]'),
    roles,
    String(cfg.qotd_ping_role_id),
  );
  // Whether that "(none)" is a decision, a blank, or a role that's been
  // deleted — same source as the Bot-Managed Roles page.
  mountRoleDialStates(container);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await apiPut("/api/economy/config", {
        // String, not parseInt: a 19-digit snowflake loses its low digits as a
        // JS number. Pydantic coerces it back to int losslessly.
        qotd_ping_role_id: pingRolePicker.getValue() || "0",
      });
      showStatus(status, true);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}

function render(container, members, cfg, roles) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>QOTD</h2>
        <div class="subtitle">Question of the Day — the role that opens one, and the paid queue</div>
      </header>
${settingsMarkup(cfg)}
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

  wireSettings(container, cfg, roles);

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
