// Economy — Operations. The day-to-day manager work: community-goal progress
// & settlement, grants, perk rentals, and the ledger audit stream. Claim
// sign-off lives on the Claims page, quest authoring on the Quests page.
// Gated by the economy manager role (or admin).
import { api, apiPost, esc, fmtAge, fmtTs } from "../api.js";
import {
  showStatus, loadMembers,
  mountPicker, toMemberOptions,
} from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";

// Common ledger kinds for the audit filter (free text still allowed).
const LEDGER_KINDS = [
  "quest", "quest_community", "qotd", "game_participation", "game_win",
  "conversion", "grant", "transfer_in", "transfer_out", "rental",
];

function nowSec() { return Date.now() / 1000; }

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading Operations…</div></div>`;
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
        <h2>Operations</h2>
        <div class="subtitle">Community goals, grants, rentals, and the ledger —
          sign-off lives on <a href="#/economy-claims">Claims</a>, authoring on
          <a href="#/economy-quests">Quests</a></div>
      </header>

      <section class="card" data-sec="community" style="display:none;">
        <div class="section-label">Community goals</div>
        <div data-community></div>
      </section>

      <section class="card" data-sec="grant">
        <div class="section-label">Grant currency</div>
        <form data-form-grant class="form">
          <div class="field-row">
            <div class="field"><label>Member</label>
              <span data-picker="grant-member"></span></div>
            <div class="field"><label>Amount</label>
              <input type="number" name="amount" min="1" step="1" value="1" style="max-width:120px;" /></div>
          </div>
          <div class="field"><label>Reason</label>
            <input type="text" name="reason" maxlength="300" /></div>
          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Grant</button>
            <span data-status-grant></span>
          </div>
        </form>
      </section>

      <section class="card" data-sec="rentals">
        <div class="section-label">Perk rentals</div>
        <div data-rentals><div class="empty">Loading…</div></div>
      </section>

      <section class="card" data-sec="ledger">
        <div class="section-label">Ledger audit</div>
        <div class="field-row">
          <div class="field"><label>Member filter</label>
            <span data-picker="ledger-member"></span></div>
          <div class="field"><label>Kind filter</label>
            <input type="text" data-ledger-kind list="dk-ledger-kinds" placeholder="(all)" style="max-width:180px;" />
            <datalist id="dk-ledger-kinds">${LEDGER_KINDS.map((k) => `<option value="${k}"></option>`).join("")}</datalist></div>
          <div class="field" style="align-self:flex-end;">
            <button class="btn" data-ledger-refresh>Apply</button></div>
        </div>
        <div data-ledger><div class="empty">Loading…</div></div>
      </section>
    </div>`;

  wireGrant(container, members);
  wireLedger(container, members);
  refreshCommunity(container, members);
  refreshRentals(container, members);
  refreshLedger(container, members);
}

// ── perk rentals ─────────────────────────────────────────────────────

const PERK_LABELS = {
  role_color: "Role color",
  role_name: "Role name",
  role_icon: "Role icon",
  role_gradient: "Role gradient",
  gift_color: "Gift color",
};

async function refreshRentals(container, members) {
  const host = container.querySelector("[data-rentals]");
  let rentals;
  try {
    rentals = (await api("/api/economy/rentals")).rentals;
  } catch (err) {
    host.innerHTML = `<div class="error">${esc(err.message)}</div>`;
    return;
  }
  if (!rentals.length) {
    host.innerHTML = `<div class="empty">No active rentals.</div>`;
    return;
  }
  const rows = rentals.map((r) => {
    const perk = PERK_LABELS[r.perk] || r.perk;
    const stateBadge = r.suspended
      ? `${esc(r.state)} <span class="badge badge-warning" title="Required server feature missing — billing paused">suspended</span>`
      : (r.cancel_at_period_end
        ? `${esc(r.state)} <span class="badge badge-dim" title="Cancels at the end of the paid week">cancelling</span>`
        : esc(r.state));
    // beneficiary shown only when it differs from the owner (a gifted color).
    const gift = String(r.beneficiary_id) !== String(r.user_id)
      ? esc(memberName(members, r.beneficiary_id))
      : "—";
    const nextBill = fmtAge((r.next_bill_at || 0) - nowSec());
    const disabled = r.cancel_at_period_end ? " disabled" : "";
    return `
      <tr data-rental-row="${r.id}">
        <td>${esc(memberName(members, r.user_id))}</td>
        <td>${esc(perk)}</td>
        <td>${stateBadge}</td>
        <td style="text-align:right;">${r.price}</td>
        <td>${nextBill}</td>
        <td>${gift}</td>
        <td>
          <button class="btn btn-ghost btn-sm" data-cancel-rental="${r.id}"${disabled}>Cancel</button>
          <span class="save-status" data-rental-status="${r.id}"></span>
        </td>
      </tr>`;
  }).join("");
  host.innerHTML = `
    <div class="field-hint">Force-cancelling an active rental runs it to the end of the paid week (no refund); a grace-period rental is cancelled immediately.</div>
    <div style="overflow-x:auto;">
      <table class="data-table">
        <thead><tr><th>Member</th><th>Perk</th><th>State</th><th>price/wk (current)</th><th>Next bill</th><th>Gift to</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;

  host.querySelectorAll("[data-cancel-rental]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.cancelRental;
      const rental = rentals.find((r) => String(r.id) === String(id));
      const status = host.querySelector(`[data-rental-status="${id}"]`);
      const note = rental && rental.state === "grace"
        ? "This grace-period rental cancels immediately."
        : "This active rental runs to the end of the paid week (no refund), then finalizes.";
      if (!(await confirmDialog(`Cancel this rental? ${note}`, { danger: true, confirmLabel: "Cancel rental" }))) return;
      try {
        await apiPost(`/api/economy/rentals/${id}/cancel`, {});
        showStatus(status, true, "Cancelled");
        refreshRentals(container, members);
      } catch (err) {
        showStatus(status, false, err.message); // 409 when not live
      }
    });
  });
}

// ── community goals ──────────────────────────────────────────────────

async function refreshCommunity(container, members) {
  const sec = container.querySelector("[data-sec='community']");
  const host = sec.querySelector("[data-community]");
  let quests;
  try {
    quests = (await api("/api/economy/quests")).quests;
  } catch (err) {
    sec.style.display = "";
    host.innerHTML = `<div class="error">${esc(err.message)}</div>`;
    return;
  }
  const community = quests.filter((q) => q.qtype === "community");
  // The whole card hides when there are no community goals — an empty
  // placeholder card was just noise.
  sec.style.display = community.length ? "" : "none";
  if (!community.length) {
    host.innerHTML = "";
    return;
  }
  const rows = community.map((q) => `
    <div class="community-goal" data-cgoal="${q.id}" style="margin:10px 0; padding:8px 0; border-top:1px solid var(--border);">
      <strong>${esc(q.title)}</strong>
      <div class="field-hint">${q.community_current || 0} / ${q.community_target ?? "—"} ${q.community_completed_at ? "· completed" : ""} ${q.community_settled_at ? "· settled" : ""}</div>
      <div class="field-row" style="align-items:flex-end;">
        <div class="field"><label>Set progress</label>
          <input type="number" min="0" step="1" data-cprogress="${q.id}" value="${q.community_current || 0}" style="max-width:120px;" /></div>
        <div class="field"><button class="btn" data-cprogress-save="${q.id}">Save</button></div>
        <div class="field"><button class="btn btn-primary" data-csettle="${q.id}">Settle payout</button></div>
        <span class="save-status" data-cstatus="${q.id}"></span>
      </div>
    </div>`).join("");
  host.innerHTML = rows;

  host.querySelectorAll("[data-cprogress-save]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.cprogressSave;
      const val = parseInt(host.querySelector(`[data-cprogress="${id}"]`).value, 10) || 0;
      const status = host.querySelector(`[data-cstatus="${id}"]`);
      try {
        await apiPost(`/api/economy/quests/${id}/progress`, { current: val });
        showStatus(status, true);
        refreshCommunity(container, members);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  });
  host.querySelectorAll("[data-csettle]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.csettle;
      const status = host.querySelector(`[data-cstatus="${id}"]`);
      if (!(await confirmDialog("Settle this community quest now? Every active member is paid once; re-settling only pays members missed earlier.", { confirmLabel: "Settle" }))) return;
      try {
        const res = await apiPost(`/api/economy/quests/${id}/settle`, {});
        showStatus(status, true, `Paid ${res.paid_count}`);
        toast(`Paid ${res.paid_count} member(s)`, "success");
        refreshCommunity(container, members);
        refreshLedger(container, members);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  });
}

// ── grant ────────────────────────────────────────────────────────────

function wireGrant(container, members) {
  const form = container.querySelector("[data-form-grant]");
  const status = form.querySelector("[data-status-grant]");
  const memberPicker = mountPicker(
    form.querySelector('[data-picker="grant-member"]'),
    toMemberOptions(members), "0",
    { emptyValue: "0", emptyLabel: "(pick a member)" },
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const picked = memberPicker.getValue();
    const body = {
      member_id: parseInt(picked || "0", 10),
      amount: parseInt(form.querySelector("[name=amount]").value, 10) || 0,
      reason: form.querySelector("[name=reason]").value,
    };
    if (!Number.isFinite(body.member_id) || body.member_id <= 0) {
      showStatus(status, false, "Pick a member first");
      return;
    }
    try {
      const res = await apiPost("/api/economy/grant", body);
      showStatus(status, true, `Credited ${res.credited}`);
      form.reset();
      memberPicker.setValue("0");
      refreshLedger(container, members);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}

// ── ledger audit ─────────────────────────────────────────────────────

let _ledgerMemberPicker = null;

function wireLedger(container, members) {
  _ledgerMemberPicker = mountPicker(
    container.querySelector('[data-picker="ledger-member"]'),
    toMemberOptions(members), "0",
    { emptyValue: "0", emptyLabel: "(all members)" },
  );
  container.querySelector("[data-ledger-refresh]").addEventListener("click", () => {
    refreshLedger(container, members);
  });
}

async function refreshLedger(container, members) {
  const host = container.querySelector("[data-ledger]");
  const picked = _ledgerMemberPicker ? _ledgerMemberPicker.getValue() : "0";
  const userId = picked && picked !== "0" ? picked : "";
  const kind = container.querySelector("[data-ledger-kind]").value.trim();
  let entries;
  try {
    entries = (await api("/api/economy/ledger", {
      user_id: userId || undefined,
      kind: kind || undefined,
      limit: 100,
    })).entries;
  } catch (err) {
    host.innerHTML = `<div class="error">${esc(err.message)}</div>`;
    return;
  }
  if (!entries.length) {
    host.innerHTML = `<div class="empty">No ledger entries.</div>`;
    return;
  }
  const rows = entries.map((e) => {
    const sign = e.amount >= 0 ? "+" : "";
    return `
      <tr>
        <td>${fmtTs(e.created_at)}</td>
        <td>${esc(memberName(members, e.user_id))}</td>
        <td>${esc(e.kind)}</td>
        <td style="text-align:right;">${sign}${e.amount}</td>
        <td>${e.actor_id ? esc(memberName(members, e.actor_id)) : "—"}</td>
      </tr>`;
  }).join("");
  host.innerHTML = `
    <div style="overflow-x:auto;">
      <table class="data-table">
        <thead><tr><th>When</th><th>Member</th><th>Kind</th><th>Amount</th><th>By</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}
