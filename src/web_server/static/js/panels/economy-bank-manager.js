// Economy — Operations. The day-to-day manager work: grants, perk rentals,
// and the ledger audit stream. Claim sign-off lives on the Claims page;
// quest authoring and community-goal progress/settlement live on the Quests
// page, where community goals are just quests with qtype = 'community'.
// Gated by the economy manager role (or admin).
import { api, apiPost, esc, fmtAge, fmtTs } from "../api.js";
import {
  showStatus, loadMembers,
  mountMemberPicker,
  mountAsync,
} from "../config-helpers.js";
import { confirmDialog } from "../ui.js";
import { mountTabs } from "../tabs.js";

// Common ledger kinds for the audit filter (free text still allowed).
const LEDGER_KINDS = [
  "quest", "quest_community", "qotd", "game_participation", "game_win",
  "conversion", "grant", "admin_remove", "transfer_in", "transfer_out", "rental",
];

// How many ledger rows the audit stream pulls. Named so the fetch and the
// "showing the N most recent" note below it can't drift apart.
const LEDGER_LIMIT = 100;

function nowSec() { return Date.now() / 1000; }

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading Operations…</div></div>`;
  return mountAsync(container, async () => {
    const members = await loadMembers().catch(() => []);
    return render(container, members);
  }, { errorMsg: "Couldn’t load the economy operations page." });
}

function memberName(members, id) {
  const m = members.find((x) => String(x.id) === String(id));
  return m ? (m.display_name || m.name) : String(id);
}

// Three tabs per Billy's call: Grant/Remove together, Perk Rentals on its
// own, Ledger Audit on its own — same lazy-per-tab pattern as the Bios panel
// (tabs.js), which this is grouped the same way as. Visibility of the whole
// page is still gated at the route level (econManagerRole, see app.js) —
// every tab here is open to the same audience, tabbing just re-groups the
// controls that were already all on one page.
function render(container, members) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Operations</h2>
        <div class="subtitle">Grants, rentals, and the ledger — sign-off lives on
          <a href="#/economy-claims">Claims</a>, and quest authoring plus
          community goals on <a href="#/economy-quests">Quests</a></div>
      </header>
      <div data-tabs></div>
    </div>`;

  // The ledger tab builds its own refresh function the first time it's
  // opened; a grant/remove landing before that needs no nudge, since that
  // first open always fetches fresh. Once built, grant/remove calls it
  // directly so the audit trail is current even while the ledger pane is
  // hidden — matching the old single-page behavior where a grant/remove
  // refreshed the always-visible ledger below it.
  let ledgerRefresh = null;

  return mountTabs(container.querySelector("[data-tabs]"), [
    {
      key: "grant-remove", label: "Grant / Remove",
      render: (pane) => renderGrantRemoveTab(pane, members, () => ledgerRefresh?.()),
      errorMsg: "Couldn’t load grant / remove.",
    },
    {
      key: "rentals", label: "Perk Rentals",
      render: (pane) => renderRentalsTab(pane, members),
      errorMsg: "Couldn’t load perk rentals.",
    },
    {
      key: "ledger", label: "Ledger Audit",
      render: (pane) => { ledgerRefresh = renderLedgerTab(pane, members); },
      errorMsg: "Couldn’t load the ledger.",
    },
  ], { ariaLabel: "Economy operations sections" });
}

// ── grant / remove ──────────────────────────────────────────────────

function renderGrantRemoveTab(pane, members, onChanged) {
  pane.innerHTML = `
    <section class="card" data-sec="grant">
      <div class="section-label">Grant Currency</div>
      <form data-form-grant class="form">
        <div class="field-row">
          <div class="field"><label>Member</label>
            <span data-picker="grant-member"></span></div>
          <div class="field"><label for="economybankm-amount">Amount</label>
            <input type="number" name="amount" min="1" step="1" value="1" style="max-width:120px;" / id="economybankm-amount"></div>
        </div>
        <div class="field"><label for="economybankm-reason">Reason</label>
          <input type="text" name="reason" maxlength="300" / id="economybankm-reason"></div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary">Grant</button>
          <span data-status-grant></span>
        </div>
      </form>
    </section>

    <section class="card" data-sec="remove">
      <div class="section-label">Remove Currency</div>
      <form data-form-remove class="form">
        <div class="field-row">
          <div class="field"><label>Member</label>
            <span data-picker="remove-member"></span></div>
          <div class="field"><label for="economybankm-amount">Amount</label>
            <input type="number" name="amount" min="1" step="1" value="1" style="max-width:120px;" / id="economybankm-amount"></div>
        </div>
        <div class="field"><label for="economybankm-reason">Reason</label>
          <input type="text" name="reason" maxlength="300" / id="economybankm-reason"></div>
        <div class="field-hint">Takes exactly the amount typed — no booster bonus.
          Removing more than they hold empties the wallet; balances never go negative.</div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-danger">Remove</button>
          <span data-status-remove></span>
        </div>
      </form>
    </section>
  `;
  wireGrant(pane, members, onChanged);
  wireRemove(pane, members, onChanged);
}

function wireGrant(pane, members, onChanged) {
  const form = pane.querySelector("[data-form-grant]");
  const status = form.querySelector("[data-status-grant]");
  // The member list is a bounded page; mountMemberPicker adds the server-side
  // lookup so a grant can still be addressed to someone outside it.
  const memberPicker = mountMemberPicker(
    form.querySelector('[data-picker="grant-member"]'),
    members, "0",
    { emptyLabel: "(pick a member)" },
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const picked = memberPicker.getValue();
    // `|| 0` used to swallow a blank or non-numeric field and post a grant of
    // zero — a no-op that still wrote a ledger row and reported "Credited 0".
    // Name the field and refuse instead.
    const rawAmount = String(form.querySelector("[name=amount]").value ?? "").trim();
    const amount = parseInt(rawAmount, 10);
    const body = {
      // Sent as a string: parseInt corrupts snowflakes past 2^53 (doubles are
      // spaced 256 apart there). The server coerces it to an int losslessly.
      member_id: picked || "0",
      amount,
      reason: form.querySelector("[name=reason]").value,
    };
    if (body.member_id === "0" || !/^[1-9]\d*$/.test(body.member_id)) {
      showStatus(status, false, "Pick a member first");
      return;
    }
    if (rawAmount === "" || !Number.isFinite(amount) || amount === 0) {
      showStatus(status, false, "Amount must be a whole number other than zero.");
      form.querySelector("[name=amount]").focus();
      return;
    }
    try {
      const res = await apiPost("/api/economy/grant", body);
      showStatus(status, true, `Credited ${res.credited}`);
      form.reset();
      memberPicker.setValue("0");
      onChanged();
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}

// ── remove ───────────────────────────────────────────────────────────

function wireRemove(pane, members, onChanged) {
  const form = pane.querySelector("[data-form-remove]");
  const status = form.querySelector("[data-status-remove]");
  const memberPicker = mountMemberPicker(
    form.querySelector('[data-picker="remove-member"]'),
    members, "0",
    { emptyLabel: "(pick a member)" },
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const picked = memberPicker.getValue();
    const rawAmount = String(form.querySelector("[name=amount]").value ?? "").trim();
    const amount = parseInt(rawAmount, 10);
    const body = {
      // String, not parseInt: snowflakes past 2^53 round to a wrong id.
      member_id: picked || "0",
      amount,
      reason: form.querySelector("[name=reason]").value,
    };
    if (body.member_id === "0" || !/^[1-9]\d*$/.test(body.member_id)) {
      showStatus(status, false, "Pick a member first");
      return;
    }
    if (rawAmount === "" || !Number.isFinite(amount) || amount < 1) {
      showStatus(status, false, "Amount must be a whole number of 1 or more.");
      form.querySelector("[name=amount]").focus();
      return;
    }
    // The picker searches server-side, so the target can be someone the
    // bounded member page never held — memberName() would then render a raw
    // snowflake on the one dialog whose job is to catch a wrong target. The
    // picker's own input carries the label it displayed.
    const who = memberPicker.getInput().value.trim()
      || memberName(members, body.member_id);
    if (!(await confirmDialog(
      `Remove ${amount} from ${who}? They keep nothing back — if they hold less, the wallet is emptied.`,
      { confirmLabel: "Remove", danger: true },
    ))) return;
    try {
      const res = await apiPost("/api/economy/remove", body);
      // The server clamps at a zero balance, so a short removal is a normal
      // outcome, not an error — say what actually went, not what was asked.
      showStatus(status, true, res.removed < res.requested
        ? `Removed ${res.removed} (all they had) — balance ${res.balance}`
        : `Removed ${res.removed} — balance ${res.balance}`);
      form.reset();
      memberPicker.setValue("0");
      onChanged();
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}

// ── perk rentals ─────────────────────────────────────────────────────

const PERK_LABELS = {
  role_color: "Role color",
  role_name: "Role name",
  role_icon: "Role icon",
  role_gradient: "Role gradient",
  role_holographic: "Role holographic",
  role_preset: "Palette color",
};

async function renderRentalsTab(pane, members) {
  pane.innerHTML = `<div class="empty">Loading…</div>`;
  await refreshRentals(pane, members);
}

async function refreshRentals(host, members) {
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
    <div class="field-hint">Force-canceling an active rental runs it to the end of the paid week (no refund); a grace-period rental is canceled immediately.</div>
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
      if (!(await confirmDialog(`Cancel this rental? ${note}`, { danger: true, confirmLabel: "Cancel Rental" }))) return;
      try {
        await apiPost(`/api/economy/rentals/${id}/cancel`, {});
        showStatus(status, true, "Cancelled");
        refreshRentals(host, members);
      } catch (err) {
        showStatus(status, false, err.message); // 409 when not live
      }
    });
  });
}

// ── ledger audit ─────────────────────────────────────────────────────

// Pull the note out of a ledger row's meta JSON. `meta` arrives as a raw
// string and is absent on most kinds, so anything unparseable is just "no memo".
// Transfers store it as `memo`; manual grants and removals as `reason` — both
// are the human explanation this column exists to show.
function memoOf(meta) {
  if (!meta) return null;
  try {
    const parsed = JSON.parse(meta);
    const memo = parsed.memo ?? parsed.reason;
    return typeof memo === "string" && memo ? memo : null;
  } catch {
    return null;
  }
}

// Builds the filter row + does the first fetch. Returns a refresh function
// that grant/remove calls after a successful save, so re-opening this tab
// (or seeing it live-update, if already open) always reflects the change
// even though tabs.js's normal lazy-load only fetches a tab's first open.
function renderLedgerTab(pane, members) {
  pane.innerHTML = `
    <div class="field-row">
      <div class="field"><label>Member filter</label>
        <span data-picker="ledger-member"></span></div>
      <div class="field"><label>Kind filter</label>
        <input type="text" data-ledger-kind list="dk-ledger-kinds" placeholder="(all)" style="max-width:180px;" aria-label="Kind filter" />
        <datalist id="dk-ledger-kinds">${LEDGER_KINDS.map((k) => `<option value="${k}"></option>`).join("")}</datalist></div>
      <div class="field" style="align-self:flex-end;">
        <button class="btn" data-ledger-refresh>Apply</button></div>
    </div>
    <div data-ledger><div class="empty">Loading…</div></div>
  `;
  const memberPicker = mountMemberPicker(
    pane.querySelector('[data-picker="ledger-member"]'),
    members, "0",
    { emptyLabel: "(all members)" },
  );
  const refresh = () => refreshLedger(pane, members, memberPicker);
  pane.querySelector("[data-ledger-refresh]").addEventListener("click", refresh);
  refresh();
  return refresh;
}

async function refreshLedger(pane, members, memberPicker) {
  const host = pane.querySelector("[data-ledger]");
  const picked = memberPicker.getValue();
  const userId = picked && picked !== "0" ? picked : "";
  const kind = pane.querySelector("[data-ledger-kind]").value.trim();
  let entries;
  try {
    entries = (await api("/api/economy/ledger", {
      user_id: userId || undefined,
      kind: kind || undefined,
      limit: LEDGER_LIMIT,
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
    const memo = memoOf(e.meta);
    return `
      <tr>
        <td>${fmtTs(e.created_at)}</td>
        <td>${esc(memberName(members, e.user_id))}</td>
        <td>${esc(e.kind)}</td>
        <td style="text-align:right;">${sign}${e.amount}</td>
        <td>${e.actor_id ? esc(memberName(members, e.actor_id)) : "—"}</td>
        <td>${memo ? esc(memo) : "—"}</td>
      </tr>`;
  }).join("");
  // The fetch is capped, and a full page of rows means older entries exist that
  // this view is silently hiding. Say so rather than reading as "all there is".
  const capNote = entries.length >= LEDGER_LIMIT
    ? `<div class="field-hint" style="padding:6px 2px;">Showing the ${LEDGER_LIMIT} most recent entries. Filter by member or kind to look further back.</div>`
    : "";
  host.innerHTML = `
    <div style="overflow-x:auto;">
      <table class="data-table">
        <thead><tr><th>When</th><th>Member</th><th>Kind</th><th>Amount</th><th>By</th><th>Memo</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>${capNote}`;
}
