// Bank Manager — quest library, claim sign-off, community goals, grants, and
// the ledger audit stream. Gated by the economy manager role (or admin).
import { api, apiPost, apiPut, apiDelete, esc, fmtAge, fmtTs } from "../api.js";
import { showStatus, loadMembers } from "../config-helpers.js";
import { toast, confirmDialog, promptDialog } from "../ui.js";

// Advisory reward bands (client-side hint only — the server saves any value).
const REWARD_BANDS = { daily: [10, 20], weekly: [25, 75] };

function nowSec() { return Date.now() / 1000; }

function bandHint(qtype, reward) {
  const band = REWARD_BANDS[qtype];
  if (!band || reward === "" || reward == null) return "";
  const n = Number(reward);
  if (!Number.isFinite(n) || (n >= band[0] && n <= band[1])) return "";
  return `Outside the suggested ${qtype} band (${band[0]}–${band[1]}). Saves fine.`;
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading Bank Manager…</div></div>`;
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
        <h2>Bank Manager</h2>
        <div class="subtitle">Quests, claim sign-off, community goals, grants, and the ledger</div>
      </header>

      <section class="card" data-sec="library">
        <div class="section-label">Quest library</div>
        <div data-quests><div class="empty">Loading…</div></div>
      </section>

      <section class="card" data-sec="author">
        <div class="section-label">New quest</div>
        <div class="ai-gen" data-quest-ai>
          <div class="field-row" style="align-items:flex-end;">
            <div class="field"><label>AI idea theme (optional)</label>
              <input type="text" data-ai-theme maxlength="200" placeholder="e.g. summer event, voice chat, art"
                     style="max-width:260px;" /></div>
            <div class="field"><label>How many</label>
              <input type="number" data-ai-count min="1" max="10" step="1" value="5" style="max-width:90px;" /></div>
            <div class="field" style="align-self:flex-end;">
              <button type="button" class="btn" data-ai-generate>✨ Generate ideas</button></div>
          </div>
          <div class="field-hint" style="opacity:.75;">Ideas use the quest type selected below. Click one to load it into the form — nothing is saved until you create it.</div>
          <div data-ai-results></div>
        </div>
        <form data-form-quest class="form">
          <div class="field-row">
            <div class="field"><label>Title</label>
              <input type="text" name="title" maxlength="256" required /></div>
            <div class="field"><label>Type</label>
              <select name="qtype">
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="community">Community</option>
              </select></div>
          </div>
          <div class="field"><label>Description</label>
            <textarea name="description" maxlength="2000" rows="2"></textarea></div>
          <div class="field"><label>Criteria (shown on the claim card)</label>
            <textarea name="criteria" maxlength="2000" rows="2"></textarea></div>
          <div class="field-row">
            <div class="field"><label>Reward</label>
              <input type="number" name="reward" min="0" step="1" value="10" style="max-width:120px;" />
              <div class="field-hint" data-reward-hint style="color:#d9a441;"></div></div>
            <div class="field" data-community-target style="display:none;"><label>Community target</label>
              <input type="number" name="community_target" min="0" step="1" style="max-width:120px;" /></div>
            <div class="field"><label>Rotate tag</label>
              <input type="text" name="rotate_tag" maxlength="64" style="max-width:160px;" /></div>
          </div>
          <div class="field-row">
            <div class="field"><label>Starts (optional)</label>
              <input type="datetime-local" name="starts_at" /></div>
            <div class="field"><label>Ends (optional)</label>
              <input type="datetime-local" name="ends_at" /></div>
          </div>
          <label style="display:flex; gap:6px; align-items:center; margin:8px 0;">
            <input type="checkbox" name="signoff" /> Requires manager sign-off
          </label>
          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Create quest</button>
            <span data-status-quest></span>
          </div>
        </form>
      </section>

      <section class="card" data-sec="claims">
        <div class="section-label">Pending claims</div>
        <div data-claims><div class="empty">Loading…</div></div>
      </section>

      <section class="card" data-sec="rentals">
        <div class="section-label">Perk rentals</div>
        <div data-rentals><div class="empty">Loading…</div></div>
      </section>

      <section class="card" data-sec="grant">
        <div class="section-label">Grant currency</div>
        <form data-form-grant class="form">
          <div class="field-row">
            <div class="field"><label>Member ID</label>
              <input type="text" name="member_id" placeholder="Discord user id" style="max-width:220px;" required /></div>
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

      <section class="card" data-sec="ledger">
        <div class="section-label">Ledger audit</div>
        <div class="field-row">
          <div class="field"><label>Member ID filter</label>
            <input type="text" data-ledger-user placeholder="(all)" style="max-width:200px;" /></div>
          <div class="field"><label>Kind filter</label>
            <input type="text" data-ledger-kind placeholder="(all) e.g. quest" style="max-width:180px;" /></div>
          <div class="field" style="align-self:flex-end;">
            <button class="btn" data-ledger-refresh>Apply</button></div>
        </div>
        <div data-ledger><div class="empty">Loading…</div></div>
      </section>
    </div>`;

  wireAuthoring(container);
  wireGrant(container, members);
  wireLedger(container, members);
  refreshQuests(container, members);
  refreshClaims(container, members);
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

// ── quest library ────────────────────────────────────────────────────

async function refreshQuests(container, members) {
  const host = container.querySelector("[data-quests]");
  let quests;
  try {
    quests = (await api("/api/economy/quests")).quests;
  } catch (err) {
    host.innerHTML = `<div class="error">${esc(err.message)}</div>`;
    return;
  }
  if (!quests.length) {
    host.innerHTML = `<div class="empty">No quests yet.</div>`;
    renderCommunity(container, members, quests);
    return;
  }
  const rows = quests.map((q) => {
    const status = `dk-quest-status-${q.id}`;
    return `
      <tr data-quest-row="${q.id}">
        <td>${esc(q.title)}</td>
        <td>${esc(q.qtype)}</td>
        <td>${q.reward}</td>
        <td>${q.rotate_tag ? esc(q.rotate_tag) : "—"}</td>
        <td>${q.signoff ? "sign-off" : "instant"}</td>
        <td>
          <label style="display:inline-flex; gap:4px; align-items:center;">
            <input type="checkbox" data-active-toggle="${q.id}"${q.active ? " checked" : ""} /> active
          </label>
          <span id="${status}" class="save-status" style="margin-left:6px;"></span>
        </td>
        <td>
          <button class="btn btn-ghost btn-sm" data-del-quest="${q.id}">Delete</button>
        </td>
      </tr>`;
  }).join("");
  host.innerHTML = `
    <div style="overflow-x:auto;">
      <table class="data-table">
        <thead><tr><th>Title</th><th>Type</th><th>Reward</th><th>Rotate</th><th>Mode</th><th>Active</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;

  host.querySelectorAll("[data-active-toggle]").forEach((cb) => {
    cb.addEventListener("change", async () => {
      const id = cb.dataset.activeToggle;
      const status = host.querySelector(`#dk-quest-status-${id}`);
      try {
        await apiPost(`/api/economy/quests/${id}/active`, { active: cb.checked });
        showStatus(status, true);
        refreshQuests(container, members);
      } catch (err) {
        cb.checked = !cb.checked; // revert
        showStatus(status, false, err.message);
      }
    });
  });
  host.querySelectorAll("[data-del-quest]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.delQuest;
      if (!(await confirmDialog("Delete this quest? Denied/expired claim history goes with it.", { danger: true, confirmLabel: "Delete" }))) return;
      try {
        await apiDelete(`/api/economy/quests/${id}`);
        toast("Quest deleted", "success");
        refreshQuests(container, members);
      } catch (err) {
        toast(err.message, "error"); // 409 when paid claims exist
      }
    });
  });

  renderCommunity(container, members, quests);
}

// ── community goals ──────────────────────────────────────────────────

function renderCommunity(container, members, quests) {
  let host = container.querySelector("[data-community-sec]");
  const community = quests.filter((q) => q.qtype === "community");
  if (!host) {
    const sec = document.createElement("section");
    sec.className = "card";
    sec.dataset.communitySec = "1";
    container.querySelector("[data-sec='claims']").before(sec);
    host = sec;
  }
  if (!community.length) {
    host.innerHTML = `<div class="section-label">Community goals</div><div class="empty">No community quests.</div>`;
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
  host.innerHTML = `<div class="section-label">Community goals</div>${rows}`;

  host.querySelectorAll("[data-cprogress-save]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.cprogressSave;
      const val = parseInt(host.querySelector(`[data-cprogress="${id}"]`).value, 10) || 0;
      const status = host.querySelector(`[data-cstatus="${id}"]`);
      try {
        await apiPost(`/api/economy/quests/${id}/progress`, { current: val });
        showStatus(status, true);
        refreshQuests(container, members);
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
        refreshQuests(container, members);
        refreshLedger(container, members);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  });
}

// ── authoring form ───────────────────────────────────────────────────

function toEpoch(v) {
  if (!v) return null;
  const ms = Date.parse(v);
  return Number.isNaN(ms) ? null : ms / 1000;
}

function wireAuthoring(container) {
  const form = container.querySelector("[data-form-quest]");
  const status = form.querySelector("[data-status-quest]");
  const rewardInput = form.querySelector("[name=reward]");
  const qtypeSel = form.querySelector("[name=qtype]");
  const hint = form.querySelector("[data-reward-hint]");
  const communityField = form.querySelector("[data-community-target]");

  const updateHint = () => { hint.textContent = bandHint(qtypeSel.value, rewardInput.value); };
  const updateCommunity = () => {
    communityField.style.display = qtypeSel.value === "community" ? "" : "none";
  };
  rewardInput.addEventListener("input", updateHint);
  qtypeSel.addEventListener("change", () => { updateHint(); updateCommunity(); });
  updateHint();
  updateCommunity();

  wireQuestAi(container, form, { updateHint, updateCommunity });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      title: form.querySelector("[name=title]").value.trim(),
      description: form.querySelector("[name=description]").value,
      criteria: form.querySelector("[name=criteria]").value,
      qtype: qtypeSel.value,
      reward: parseInt(rewardInput.value, 10) || 0,
      signoff: form.querySelector("[name=signoff]").checked,
      rotate_tag: form.querySelector("[name=rotate_tag]").value.trim(),
      starts_at: toEpoch(form.querySelector("[name=starts_at]").value),
      ends_at: toEpoch(form.querySelector("[name=ends_at]").value),
    };
    if (qtypeSel.value === "community") {
      const t = form.querySelector("[name=community_target]").value;
      body.community_target = t === "" ? null : parseInt(t, 10);
    }
    try {
      await apiPost("/api/economy/quests", body);
      showStatus(status, true, "Created");
      form.reset();
      updateHint();
      updateCommunity();
      const members = await loadMembers().catch(() => []);
      refreshQuests(container, members);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}

// ── AI quest-idea generator ──────────────────────────────────────────
// Batches suggestions for the currently-selected quest type, renders them as
// clickable cards, and loads a picked idea into the New-quest form. Nothing is
// persisted here — the manager still reviews and submits.

function wireQuestAi(container, form, { updateHint, updateCommunity }) {
  const root = container.querySelector("[data-quest-ai]");
  if (!root) return;
  const btn = root.querySelector("[data-ai-generate]");
  const results = root.querySelector("[data-ai-results]");
  const qtypeSel = form.querySelector("[name=qtype]");

  const setField = (name, value) => {
    const el = form.querySelector(`[name=${name}]`);
    if (el != null) el.value = value == null ? "" : value;
  };

  const loadIdea = (idea) => {
    setField("title", idea.title || "");
    setField("description", idea.description || "");
    setField("criteria", idea.criteria || "");
    setField("reward", idea.reward ?? "");
    if (qtypeSel.value === "community" && idea.community_target != null) {
      setField("community_target", idea.community_target);
    }
    updateHint();
    updateCommunity();
    form.querySelector("[name=title]").focus();
    toast("Idea loaded — edit and create", "success");
    form.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  const renderIdeas = (ideas) => {
    if (!ideas.length) {
      results.innerHTML = `<div class="empty">No ideas came back — try again.</div>`;
      return;
    }
    results.innerHTML = "";
    ideas.forEach((idea) => {
      const card = document.createElement("div");
      card.className = "ai-idea";
      card.style.cssText =
        "border:1px solid var(--border,#3a3a3a);border-radius:8px;padding:8px 10px;margin:6px 0;cursor:pointer;";
      const target =
        idea.community_target != null
          ? ` · target ${idea.community_target}`
          : "";
      card.innerHTML =
        `<div style="display:flex;justify-content:space-between;gap:8px;">` +
        `<strong>${esc(idea.title || "(untitled)")}</strong>` +
        `<span class="badge">${idea.reward ?? 0}${esc(target)}</span></div>` +
        (idea.description ? `<div style="opacity:.85;margin-top:2px;">${esc(idea.description)}</div>` : "") +
        (idea.criteria ? `<div style="opacity:.65;font-size:.9em;margin-top:2px;">✓ ${esc(idea.criteria)}</div>` : "");
      card.addEventListener("click", () => loadIdea(idea));
      results.appendChild(card);
    });
  };

  btn.addEventListener("click", async () => {
    const theme = root.querySelector("[data-ai-theme]").value.trim();
    const count = Math.max(1, Math.min(10, parseInt(root.querySelector("[data-ai-count]").value, 10) || 5));
    const qtype = qtypeSel.value;
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "Generating…";
    results.innerHTML = `<div class="empty">Generating ${count} ${esc(qtype)} idea(s)…</div>`;
    try {
      const data = await apiPost("/api/economy/quests/generate", { qtype, count, theme });
      renderIdeas(data.ideas || []);
    } catch (err) {
      results.innerHTML = `<div class="error">${esc(err.message)}</div>`;
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  });
}

// ── claims ───────────────────────────────────────────────────────────

async function refreshClaims(container, members) {
  const host = container.querySelector("[data-claims]");
  let claims;
  try {
    claims = (await api("/api/economy/claims", { state: "pending" })).claims;
  } catch (err) {
    host.innerHTML = `<div class="error">${esc(err.message)}</div>`;
    return;
  }
  if (!claims.length) {
    host.innerHTML = `<div class="empty">No pending claims.</div>`;
    return;
  }
  const rows = claims.map((c) => `
    <tr data-claim-row="${c.id}">
      <td>${esc(memberName(members, c.user_id))}</td>
      <td>${esc(c.quest_title || "#" + c.quest_id)}</td>
      <td>${fmtAge(nowSec() - (c.created_at || 0))}</td>
      <td>${c.deny_count || 0}</td>
      <td>
        <button class="btn btn-primary btn-sm" data-approve="${c.id}">Approve</button>
        <button class="btn btn-ghost btn-sm" data-deny="${c.id}">Deny</button>
        <span class="save-status" data-claim-status="${c.id}"></span>
      </td>
    </tr>`).join("");
  host.innerHTML = `
    <div style="overflow-x:auto;">
      <table class="data-table">
        <thead><tr><th>Claimant</th><th>Quest</th><th>Age</th><th>Denies</th><th></th></tr></thead>
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
        refreshClaims(container, members);
        refreshLedger(container, members);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  });
  host.querySelectorAll("[data-deny]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.deny;
      const status = host.querySelector(`[data-claim-status="${id}"]`);
      const reason = await promptDialog("Reason for denial (shown to the claimant):", { confirmLabel: "Deny", required: true, danger: true });
      if (reason == null) return;
      try {
        await apiPost(`/api/economy/claims/${id}/deny`, { reason });
        showStatus(status, true, "Denied");
        refreshClaims(container, members);
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
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      member_id: parseInt(form.querySelector("[name=member_id]").value.trim(), 10),
      amount: parseInt(form.querySelector("[name=amount]").value, 10) || 0,
      reason: form.querySelector("[name=reason]").value,
    };
    if (!Number.isFinite(body.member_id)) {
      showStatus(status, false, "Enter a numeric member id");
      return;
    }
    try {
      const res = await apiPost("/api/economy/grant", body);
      showStatus(status, true, `Credited ${res.credited}`);
      form.reset();
      refreshLedger(container, members);
    } catch (err) {
      showStatus(status, false, err.message);
    }
  });
}

// ── ledger audit ─────────────────────────────────────────────────────

function wireLedger(container, members) {
  container.querySelector("[data-ledger-refresh]").addEventListener("click", () => {
    refreshLedger(container, members);
  });
}

async function refreshLedger(container, members) {
  const host = container.querySelector("[data-ledger]");
  const userId = container.querySelector("[data-ledger-user]").value.trim();
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
