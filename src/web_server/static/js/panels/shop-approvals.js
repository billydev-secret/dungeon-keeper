/**
 * The two work queues the perk shop generates: sponsored emoji submissions
 * waiting on a decision, and custom-item orders waiting on a human.
 *
 * These were the third and the sixteenth section of a 1,339-line settings page.
 * Two things were wrong with that, both structural:
 *
 *  - WRONG AUDIENCE. `/api/economy/emoji-submissions` (list, approve, deny) is
 *    gated `require_economy_manager` — admins OR the configured economy-manager
 *    role. The backend deliberately lets a manager work this queue. But the page
 *    hosting it was `adminOnly: true`, so a manager could never reach it. The
 *    comparable queues, Claims and QOTD, are not adminOnly. This page is not
 *    either, which is what makes the backend's grant mean something.
 *
 *  - WRONG PLACE FOR WORK. A queue fills up on its own and has to be worked
 *    through; a price dial sits still until someone changes it. Burying pending
 *    work at scroll positions 3 and 16 of a settings page meant nothing told you
 *    it was there. As its own page it can carry a count in the nav.
 *
 * Both queues are read-mostly and poll on mount only — no timers, so leaving the
 * page open costs nothing.
 */
import { api, apiPost, esc } from "../api.js";
import { mountAsync, showStatus, loadMembers } from "../config-helpers.js";
import { promptDialog, toast } from "../ui.js";
import { renderError } from "../states.js";
import { economyOffBanner } from "./economy-shop-shared.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading approvals…</div></div>`;

  return mountAsync(container, async () => {
    // The config fetch is only for the economy-off banner, and it is
    // admin-gated — an economy manager gets a 403. Swallow it: the queues are
    // the point of this page and they are manager-readable.
    const cfg = await api("/api/economy/config").catch(() => null);
    render(container, cfg);
    wireAllApprovals(container);
    wireEmojiQueue(container);
    wireThemeQueue(container);
    wireShopOrders(container);
  }, { errorMsg: "Couldn’t load the approval queues." });
}

function render(container, cfg) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Approvals</h2>
        <div class="subtitle">Purchases that need a person before they take effect.
          What members can buy is curated on
          <a href="#/economy-sinks">Shop &amp; Perks</a>, and priced on
          <a href="#/pricing">Pricing</a>.</div>
      </header>
      ${economyOffBanner(cfg)}

      <section class="form card">
        <div class="section-label">Everything Waiting</div>
        <div class="field-hint" style="margin-bottom:1rem;">
          Every economy queue in one list, longest wait first — so one look answers
          “is anyone waiting on us?”. Each row says where it gets handled.
        </div>
        <div data-all-approvals></div>
      </section>

      <section class="form card" style="margin-top:1.5rem;">
        <div class="section-label">Emoji Approval Queue</div>
        <div class="field-hint" style="margin-bottom:1rem;">
          Emojis members have sponsored and paid for, waiting on your decision.
          Approving uploads the emoji to the server and starts its weekly rent — the
          first week is already paid. Turning one down refunds the member in full. If a
          rental later lapses, the emoji comes back down on its own.
        </div>
        <div data-emoji-queue></div>
        <div data-emoji-empty class="field-hint" style="display:none;">Nothing is waiting for review.</div>
      </section>

      <section class="form card" style="margin-top:1.5rem;">
        <div class="section-label">Themed Days</div>
        <div class="field-hint" style="margin-bottom:1rem;">
          Days members have paid to theme, waiting on your decision. Approving puts a
          theme in the queue — it isn’t announced yet; the next one goes up on its own
          whenever the theme channel is free. Turning one down refunds the member in
          full, and so does pulling a queued one back out. Ending a theme that’s already
          running does <b>not</b> refund it: it was announced and people saw it.
        </div>
        <div data-theme-queue></div>
        <div data-theme-empty class="field-hint" style="display:none;">
          Nothing waiting, queued or running.
        </div>
      </section>

      <section class="form card" style="margin-top:1.5rem;">
        <div class="section-label">Orders Waiting on Staff</div>
        <div class="field-hint" style="margin-bottom:1rem;">
          Every custom item somebody bought that a human still has to do. These are the
          same jobs that appear on your <a href="#/mod-todo">todo board</a> — tick one off
          there (or in Discord) once it’s done and the buyer’s payment is kept. Turn one
          down here instead and the money goes straight back to them.
        </div>
        <div data-orders></div>
        <div data-orders-empty class="field-hint" style="display:none;">
          Nothing waiting. Orders show up here the moment somebody buys one.
        </div>
      </section>
    </div>
  `;
}

function emojiRow(sub, memberName) {
  const kind = sub.animated ? "animated" : "static";
  return `
    <div class="card" data-sub-id="${sub.id}"
         style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px;">
      <img src="/api/economy/emoji-submissions/${sub.id}/image" alt=""
           width="48" height="48"
           style="width:48px;height:48px;object-fit:contain;
                  background:repeating-conic-gradient(#808080 0% 25%, #a0a0a0 0% 50%) 50% / 12px 12px" />
      <div>
        <div><code>:${esc(sub.name)}:</code> <span class="field-hint">(${kind}, ${sub.price}/wk)</span></div>
        <div class="field-hint">from <span data-member-id="${esc(sub.user_id)}">${esc(memberName(sub.user_id))}</span></div>
      </div>
      <div style="display:flex;gap:8px;margin-left:auto;">
        <button type="button" class="btn btn-primary" data-approve>Approve and Upload</button>
        <button type="button" class="btn btn-danger" data-deny>Turn Down</button>
      </div>
      <span data-row-status></span>
    </div>`;
}

function wireEmojiQueue(container) {
  const listEl = container.querySelector("[data-emoji-queue]");
  const emptyEl = container.querySelector("[data-emoji-empty]");

  // Every other moderator queue on the dashboard resolves people through
  // loadMembers(); this one printed the raw snowflake, which tells a reviewer
  // nothing about who is asking. Falls back to the id when the member list is
  // unavailable, or the sponsor has left and isn't in it.
  let nameById = new Map();
  const memberName = (id) => nameById.get(String(id)) || String(id);

  async function refresh() {
    let subs = [];
    try {
      const [members, data] = await Promise.all([
        loadMembers().catch(() => []),
        api("/api/economy/emoji-submissions?state=pending"),
      ]);
      nameById = new Map(
        members.map((m) => [String(m.id), m.display_name || m.name || String(m.id)]),
      );
      subs = data.submissions;
    } catch (err) {
      listEl.innerHTML = `<div class="error">${esc(err.message)}</div>`;
      return;
    }
    listEl.innerHTML = subs.map((s) => emojiRow(s, memberName)).join("");
    emptyEl.style.display = subs.length ? "none" : "block";
  }
  refresh();

  listEl.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const row = btn.closest("[data-sub-id]");
    const id = row.getAttribute("data-sub-id");
    const rowStatus = row.querySelector("[data-row-status]");
    btn.disabled = true;
    try {
      if (btn.hasAttribute("data-approve")) {
        const out = await apiPost(`/api/economy/emoji-submissions/${id}/approve`, {});
        showStatus(rowStatus, out.ok, out.ok ? "Live" : out.error);
      } else if (btn.hasAttribute("data-deny")) {
        // Shared dialog rather than the browser's native prompt(), so it is
        // themed, focus-trapped, and keyboard-accessible like every other
        // confirmation on the dashboard.
        const reason = await promptDialog(
          "This member is refunded in full and sent your reason. What should they be told?",
          { title: "Turn down this emoji?", confirmLabel: "Turn Down", danger: true },
        );
        if (reason === null) { btn.disabled = false; return; }
        await apiPost(`/api/economy/emoji-submissions/${id}/deny`, {
          reason: reason.trim() || "not a fit for the server",
        });
        showStatus(rowStatus, true, "Turned down and refunded");
      }
      await refresh();
    } catch (err) {
      showStatus(rowStatus, false, err.message);
      btn.disabled = false;
    }
  });
}

const THEME_STATE_LABEL = {
  pending: "waiting on you",
  approved: "queued",
  live: "running now",
};

function themeRow(sub, memberName) {
  const state = String(sub.state);
  const blurb = sub.blurb
    ? `<div class="field-hint">“${esc(sub.blurb)}”</div>` : "";
  // Contextual, because the refund rule differs per state and a button that
  // silently means something else in another row is how a mod refunds a day
  // that already ran.
  let actions = "";
  if (state === "pending") {
    actions = `
      <button type="button" class="btn btn-primary" data-approve>Approve</button>
      <button type="button" class="btn btn-danger" data-deny>Turn Down</button>`;
  } else if (state === "approved") {
    actions = `<button type="button" class="btn btn-danger" data-withdraw>Remove from Queue</button>`;
  } else if (state === "live") {
    actions = `<button type="button" class="btn btn-danger" data-takedown>End Early</button>`;
  }
  return `
    <div class="card" data-sub-id="${sub.id}"
         style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px;">
      <div>
        <div><b>${esc(sub.title)}</b>
          <span class="field-hint">(${sub.price}, ${esc(THEME_STATE_LABEL[state] || state)})</span></div>
        <div class="field-hint">from <span data-member-id="${esc(sub.user_id)}">${esc(memberName(sub.user_id))}</span></div>
        ${blurb}
      </div>
      <div style="display:flex;gap:8px;margin-left:auto;flex-wrap:wrap;">${actions}</div>
      <span data-row-status></span>
    </div>`;
}

function wireThemeQueue(container) {
  const listEl = container.querySelector("[data-theme-queue]");
  const emptyEl = container.querySelector("[data-theme-empty]");

  let nameById = new Map();
  const memberName = (id) => nameById.get(String(id)) || String(id);

  async function refresh() {
    let subs = [];
    try {
      // Three states in one list, ordered the way a mod works through them:
      // what needs a decision, what is queued behind it, what is up right now.
      const [members, pending, queued, live] = await Promise.all([
        loadMembers().catch(() => []),
        api("/api/economy/theme-submissions?state=pending"),
        api("/api/economy/theme-submissions?state=approved"),
        api("/api/economy/theme-submissions?state=live"),
      ]);
      nameById = new Map(
        members.map((m) => [String(m.id), m.display_name || m.name || String(m.id)]),
      );
      subs = [...live.submissions, ...pending.submissions, ...queued.submissions];
    } catch {
      listEl.innerHTML = renderError("Couldn’t load the themed days.");
      emptyEl.style.display = "none";
      return;
    }
    listEl.innerHTML = subs.map((sub) => themeRow(sub, memberName)).join("");
    emptyEl.style.display = subs.length ? "none" : "block";
  }
  refresh();

  listEl.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const row = btn.closest("[data-sub-id]");
    const id = row.getAttribute("data-sub-id");
    const rowStatus = row.querySelector("[data-row-status]");
    btn.disabled = true;
    try {
      if (btn.hasAttribute("data-approve")) {
        await apiPost(`/api/economy/theme-submissions/${id}/approve`, {});
        showStatus(rowStatus, true, "Queued");
      } else if (btn.hasAttribute("data-deny")) {
        const reason = await promptDialog(
          "This member is refunded in full and sent your reason. What should they be told?",
          { title: "Turn down this theme?", confirmLabel: "Turn Down", danger: true },
        );
        if (reason === null) { btn.disabled = false; return; }
        await apiPost(`/api/economy/theme-submissions/${id}/deny`, {
          reason: reason.trim() || "not a fit for the server",
        });
        showStatus(rowStatus, true, "Turned down and refunded");
      } else if (btn.hasAttribute("data-withdraw")) {
        const reason = await promptDialog(
          "It never ran, so the member is refunded in full.",
          { title: "Remove this theme from the queue?", confirmLabel: "Remove", danger: true },
        );
        if (reason === null) { btn.disabled = false; return; }
        await apiPost(`/api/economy/theme-submissions/${id}/withdraw`, {
          reason: reason.trim(),
        });
        showStatus(rowStatus, true, "Removed and refunded");
      } else if (btn.hasAttribute("data-takedown")) {
        const ok = await promptDialog(
          "The announcement is unpinned and deleted. There is no refund — the theme "
          + "was announced and people saw it.",
          { title: "End this theme early?", confirmLabel: "End It", danger: true },
        );
        if (ok === null) { btn.disabled = false; return; }
        await apiPost(`/api/economy/theme-submissions/${id}/take-down`, {});
        showStatus(rowStatus, true, "Ended");
      }
      await refresh();
    } catch (err) {
      showStatus(rowStatus, false, err.message);
      btn.disabled = false;
    }
  });
}

function orderRow(order) {
  const note = order.note
    ? `<div class="field-hint">“${esc(order.note)}”</div>` : "";
  return `
    <div class="card" data-order-id="${order.id}"
         style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px;">
      <div>
        <div><b>${esc(order.item_name)}</b>
          <span class="field-hint">(${order.price})</span></div>
        <div class="field-hint">for ${esc(order.user_name || order.user_id)}</div>
        ${note}
      </div>
      <div style="display:flex;gap:8px;margin-left:auto;">
        <button type="button" class="btn btn-danger" data-refund>Turn Down &amp; Refund</button>
      </div>
      <span data-row-status></span>
    </div>`;
}

function wireShopOrders(container) {
  const listEl = container.querySelector("[data-orders]");
  const emptyEl = container.querySelector("[data-orders-empty]");

  async function refresh() {
    try {
      const data = await api("/api/economy/shop-orders");
      const rows = data.orders || [];
      listEl.innerHTML = rows.map(orderRow).join("");
      emptyEl.style.display = rows.length ? "none" : "block";
    } catch {
      // Into its own element: writing this into emptyEl replaced the real
      // empty-state copy for the rest of the session, so a later successful
      // fetch with nothing waiting still read "Couldn't load the orders."
      listEl.innerHTML = renderError("Couldn’t load the orders.");
      emptyEl.style.display = "none";
    }
  }

  listEl.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-refund]");
    if (!btn) return;
    const row = btn.closest("[data-order-id]");
    const id = row.getAttribute("data-order-id");
    const rowStatus = row.querySelector("[data-row-status]");
    const reason = await promptDialog(
      "The buyer gets their money back and the job comes off the todo board. "
      + "Anything you type here is for your own records.",
      { title: "Turn this order down?", confirmLabel: "Refund", danger: true },
    );
    if (reason === null) return;
    btn.disabled = true;
    try {
      await apiPost(`/api/economy/shop-orders/${id}/refund`, { reason: reason || "" });
      toast("Refunded.");
      await refresh();
    } catch (err) {
      showStatus(rowStatus, false, err.message);
      btn.disabled = false;
    }
  });

  refresh();
}

// ── The unified queue ─────────────────────────────────────────────────────
//
// Six economy queues used to live on three pages, and two of them (Pin of the
// Day, and orders on a page that 403'd for a manager) had no working web
// surface at all — so a moderator could not find out that nothing was waiting
// without opening three pages and still miss two queues.
//
// A flat list, not tabs. Tabs would preserve the exact complaint: you would
// still click three times to learn a queue was empty. Production runs these at
// zero to two pending rows each, so one list is never long.
//
// Resolving stays with the product. This section is a *finder* — every row
// says where the work is done, and nothing here approves anything, so no
// product's copy, permissions or side effects move.

const KIND_LABEL = {
  theme:   "Themed day",
  sponsor: "Sponsored question",
  pin:     "Pin of the day",
  emoji:   "Sponsored emoji",
  claim:   "Quest sign-off",
  order:   "Shop order",
};

// Where each kind is actually worked. A section on this page scrolls; another
// page is a link. Pin of the Day has no web queue and is resolved from the
// Discord approvals board — saying so is more honest than a dead link.
const KIND_ACTION = {
  theme:   { kind: "scroll", target: "[data-theme-queue]", label: "Below \u2193" },
  emoji:   { kind: "scroll", target: "[data-emoji-queue]", label: "Below \u2193" },
  order:   { kind: "scroll", target: "[data-orders]",      label: "Below \u2193" },
  sponsor: { kind: "link",   href: "#/economy-qotd-submissions", label: "QOTD \u2192" },
  claim:   { kind: "link",   href: "#/economy-claims",           label: "Claims \u2192" },
  pin:     { kind: "none",   label: "In Discord" },
};

function waitedFor(ts) {
  const secs = Math.max(0, Date.now() / 1000 - Number(ts || 0));
  const days = Math.floor(secs / 86400);
  if (days >= 1) return days === 1 ? "1 day" : `${days} days`;
  const hours = Math.floor(secs / 3600);
  if (hours >= 1) return hours === 1 ? "1 hour" : `${hours} hours`;
  const mins = Math.floor(secs / 60);
  return mins <= 1 ? "just now" : `${mins} mins`;
}

function approvalActionCell(kind) {
  const action = KIND_ACTION[kind];
  if (!action) return "";
  if (action.kind === "link") {
    return `<a href="${esc(action.href)}">${esc(action.label)}</a>`;
  }
  if (action.kind === "scroll") {
    return `<a href="#" data-scroll-to="${esc(action.target)}">${esc(action.label)}</a>`;
  }
  return `<span class="field-hint">${esc(action.label)}</span>`;
}

function approvalRow(row) {
  return `<tr>
    <td>${esc(KIND_LABEL[row.kind] || row.kind)}</td>
    <td>${esc(row.user_name || row.user_id)}</td>
    <td>${esc(row.summary || "\u2014")}</td>
    <td style="white-space:nowrap;">${esc(String(row.amount ?? 0))}</td>
    <td style="white-space:nowrap;">${esc(waitedFor(row.created_at))}</td>
    <td style="white-space:nowrap;">${approvalActionCell(row.kind)}</td>
  </tr>`;
}

async function wireAllApprovals(container) {
  const host = container.querySelector("[data-all-approvals]");
  if (!host) return;
  host.innerHTML = `<div class="field-hint">Loading\u2026</div>`;

  let rows;
  try {
    rows = (await api("/api/economy/approvals")).approvals || [];
  } catch (err) {
    // A failure here must not read as "nothing is waiting" — that is the one
    // wrong answer this section can give.
    host.innerHTML =
      `<div class="error">Couldn\u2019t load the queue: ${esc(err.message)}. `
      + `The sections below still work.</div>`;
    return;
  }

  if (!rows.length) {
    host.innerHTML = `<div class="field-hint">Nothing is waiting on you.</div>`;
    return;
  }

  host.innerHTML = `
    <div class="data-table-scroll"><table class="data-table">
      <thead><tr>
        <th>Type</th><th>Member</th><th>What</th><th>Coins</th>
        <th>Waiting</th><th>Handled</th>
      </tr></thead>
      <tbody>${rows.map(approvalRow).join("")}</tbody>
    </table></div>`;

  host.querySelectorAll("[data-scroll-to]").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      const el = container.querySelector(a.dataset.scrollTo);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  });
}
