// Economy — Quests. The quest library and the authoring form (plus the AI
// idea generator). Operational work — claim sign-off, community-goal
// settlement, grants, the ledger — lives on the Operations page. Gated by
// the economy manager role (or admin).
import { api, apiPost, apiPut, apiDelete, esc } from "../api.js";
import { showStatus, guardForm, loadChannels, mountChannelPicker } from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";
import { KIND_LABELS, CHANNEL_SCOPED_KINDS } from "./economy-sources-shared.js";

// Advisory reward bands (client-side hint only — the server saves any value).
const REWARD_BANDS = { daily: [10, 20], weekly: [25, 75], monthly: [50, 90] };

// Plain-language cadence per quest type (shown under the Type select).
const TYPE_HINTS = {
  daily: "Members can complete it once per day (guild-local midnight). Active dailies form a pool; each member is shown a few of them per day — set how many under Board size.",
  weekly: "A push across the ISO week. Active weeklies form a pool drawn from per member — see Board size. On a game trigger it must count progress (“How Many Times” above 1), not finish on the first action — only dailies are one-shot.",
  monthly: "A guild-wide goal for the calendar month — everyone contributes to one shared counter, like a community goal but monthly. Auto-tracked only (pick a game trigger); the scheduler runs one monthly goal at a time, auto-sizes it, and pays 40/70/100% tiers to everyone at month end. Not member-claimable, no personal board.",
  community: "One shared goal for the whole server. Manual completion: you track progress and settle from Operations. Game trigger: every member's action counts automatically, the target auto-sizes from recent activity, and the biweekly scheduler runs it with tiered payouts (40/70/100%).",
  event: "Pays by itself every time the trigger happens — no claims, no daily/weekly cap. One active event quest per trigger.",
};

// The per-member board dials. Each entry: [settings key, cadence, label].
// Personal-board size dials. Monthly left this set when it became a guild-wide
// community-measured goal (no personal board) — only daily/weekly draw one.
const BOARD_FIELDS = [
  ["quest_board_daily", "daily", "Daily"],
  ["quest_board_weekly", "weekly", "Weekly"],
];

// Econ config for the board dials, or null for manager-role holders (the
// config GET is admin-gated). Module-scoped because the library summary
// renders "pool → shown" on every refreshQuests(), and a board save has to
// move those numbers without a reload.
let boardCfg = null;

function bandHint(qtype, reward) {
  const band = REWARD_BANDS[qtype];
  if (!band || reward === "" || reward == null) return "";
  const n = Number(reward);
  if (!Number.isFinite(n) || (n >= band[0] && n <= band[1])) return "";
  return `Outside the suggested ${qtype} band (${band[0]}–${band[1]}). Saves fine.`;
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading quests…</div></div>`;
  (async () => {
    // All three loads are independent — fetch them together rather than one
    // after another (W-D11). The quest list is handed straight to the first
    // render so the library doesn't flash "Loading…" a second time.
    const [channelsResult, cfgResult, questsResult] = await Promise.allSettled([
      loadChannels(),
      // Admin probe, same as Income Sources: the config GET is admin-gated, so
      // a success means this user may edit the board sizes. Manager-role
      // holders get a 403 and the read-only view.
      api("/api/economy/config"),
      api("/api/economy/quests"),
    ]);
    const channels = channelsResult.status === "fulfilled" ? channelsResult.value : [];
    boardCfg = cfgResult.status === "fulfilled" ? cfgResult.value : null;
    const economyOff = boardCfg != null && !boardCfg.enabled;
    const prefetched = questsResult.status === "fulfilled" ? questsResult.value.quests : null;
    render(container, channels, boardCfg, economyOff, prefetched);
  })();
  return null;
}

function boardSection(cfg) {
  if (!cfg) {
    // Manager-role view: the config GET is admin-gated, so we have no values
    // to show — describe the dial rather than printing a row of em-dashes.
    return `
      <div class="field-hint">
        Members don't see every active quest — each is shown a few of each
        cadence, drawn from that cadence's pool. How many is admin-editable.
      </div>`;
  }
  const fields = BOARD_FIELDS.map(([key, , label]) => `
    <div class="field">
      <label for="q-${key}">${label} Quests Shown</label>
      <input type="number" name="${key}" id="q-${key}" required value="${cfg[key]}"
             min="0" max="25" step="1" style="max-width:90px;" />
    </div>`).join("");
  return `
    <div class="field-hint" style="margin-bottom:8px;">
      How many quests of each kind one member sees at a time, picked from all the
      active quests of that kind. Showing fewer keeps the board from feeling like a
      chore list without your having to deactivate anything, and a large pool paired
      with a small board means members see the same quest come round far less often.
      <strong>Setting one to 0 switches that kind off completely</strong> — nothing is
      shown and nothing pays. Like every other change here, a new size takes effect at
      the next daily, weekly, or monthly roll; nobody's current board is reshuffled.
    </div>
    <form data-form-board class="form">
      <div class="field-row">${fields}</div>
      <div style="display:flex; gap:8px; align-items:center; margin-top:10px;">
        <button type="submit" class="btn btn-primary">Save</button>
        <span data-status-board></span>
      </div>
    </form>`;
}

function render(container, channels, cfg, economyOff, prefetchedQuests) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Quests</h2>
        <div class="subtitle">Write and manage the quests members work through. Approving
          finished quests and settling community goals happen on Operations.</div>
      </header>
      ${economyOff ? `<div class="empty" role="status" style="margin-bottom:12px;">
        The economy is currently off, so nothing below takes effect until it is switched
        on under <a href="#/economy-config">Economy Settings</a>.</div>` : ""}

      <section class="card" data-sec="library">
        <div class="section-label">Quest Library</div>
        <div class="field-hint" style="margin-bottom:8px;">
          Quests are the tunable rewards. Members also earn automatically from
          faucets (daily logins &amp; streaks, XP conversion, game wins, QOTD…) —
          those rates live on <a href="#/economy-income-sources">Income Sources</a>.
          Sign-off claims and community-goal payouts are handled on
          <a href="#/economy-bank-manager">Operations</a>.
        </div>
        <div class="field-hint" style="margin-bottom:8px;">
          🔁 <strong>Edits apply at the next roll.</strong> Activating,
          deactivating, or editing a quest never reshuffles anyone's current
          board — the change lands at the next daily / weekly / monthly
          period boundary, so in-progress boards stay put.
        </div>
        <div data-quest-slots class="field-hint" style="margin-bottom:6px;"></div>
        <div data-quests><div class="empty">Loading…</div></div>
      </section>

      <section class="card" data-sec="board">
        <div class="section-label">Board Size</div>
        ${boardSection(cfg)}
      </section>

      <section class="card" data-sec="author">
        <div class="section-label" data-author-label>New Quest</div>
        <form data-form-quest class="form">
          <div class="field-row">
            <div class="field"><label for="q-title">Title</label>
              <input type="text" name="title" id="q-title" maxlength="256" required />
              <div class="field-hint">What members see at the top of the quest card.</div></div>
            <div class="field"><label for="q-type">Type</label>
              <select name="qtype" id="q-type">
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
                <option value="community">Community goal</option>
                <option value="event">Event (every time it happens)</option>
              </select>
              <div class="field-hint" data-type-hint style="max-width:420px;"></div></div>
          </div>
          <div class="field"><label for="q-description">Description</label>
            <textarea name="description" id="q-description" maxlength="2000" rows="2"></textarea>
            <div class="field-hint">A sentence or two of flavor, shown under the title.</div></div>
          <div class="field"><label for="q-criteria">What Counts as Done</label>
            <textarea name="criteria" id="q-criteria" maxlength="2000" rows="2"></textarea>
            <div class="field-hint">Spelled out on the card members claim from, so there
              is no argument later about whether they finished it.</div></div>
          <div class="field-row">
            <div class="field"><label for="q-reward">Reward (coins)</label>
              <input type="number" name="reward" id="q-reward" required min="0" max="1000000" step="1" value="10" style="max-width:120px;" />
              <div class="field-hint" data-reward-hint style="color:#d9a441;"></div></div>
            <div class="field"><label for="q-reward-xp">Bonus XP</label>
              <input type="number" name="reward_xp" id="q-reward-xp" required min="0" max="1000000" step="1" value="0" style="max-width:120px;" />
              <div class="field-hint">Leveling XP paid alongside the coins. The booster
                multiplier does not apply to XP.</div></div>
            <div class="field" data-community-target style="display:none;"><label for="q-community-target">Community Target</label>
              <input type="number" name="community_target" id="q-community-target" min="0" max="100000000" step="1" style="max-width:120px;" />
              <div class="field-hint">How far the whole server has to get before the goal
                pays out.</div></div>
            <div class="field" data-community-auto style="display:none;"><label>Community Target</label>
              <div class="field-hint">Set automatically when the run starts, based on how
                busy the server has been lately — a normal period (a week for community
                goals, a month for monthly ones) gets roughly three quarters of the way
                there, and a real push finishes it. There is nothing to enter here.</div></div>
            <div class="field" data-rotate-field><label for="q-rotate">Rotation Tag</label>
              <input type="text" name="rotate_tag" id="q-rotate" maxlength="64" style="max-width:160px;" />
              <div class="field-hint">Optional. Daily quests sharing a tag take turns
                rather than all appearing at once.</div></div>
            <div class="field"><label for="q-pair">Pair Tag</label>
              <input type="text" name="pair_tag" id="q-pair" maxlength="64" style="max-width:160px;" />
              <div class="field-hint">Optional. When exactly two quests of the same kind
                share a tag, they land on a member's board together — useful for a pair
                like "host a game" and "play a game".</div></div>
          </div>
          <div class="field-row">
            <div class="field"><label for="q-starts">Starts (optional)</label>
              <input type="datetime-local" name="starts_at" id="q-starts" />
              <div class="field-hint">The quest stays off boards until this moment. Leave
                empty to start straight away.</div></div>
            <div class="field"><label for="q-ends">Ends (optional)</label>
              <input type="datetime-local" name="ends_at" id="q-ends" />
              <div class="field-hint">The quest drops off boards after this moment. Leave
                empty to run indefinitely.</div></div>
          </div>

          <div class="field" data-completion-block>
            <label>How It Completes</label>
            <div style="display:flex; gap:14px; flex-wrap:wrap; margin:4px 0;">
              <label style="display:flex; gap:5px; align-items:center; cursor:pointer;">
                <input type="radio" name="completion" value="manual" checked /> Member claims it</label>
              <label style="display:flex; gap:5px; align-items:center; cursor:pointer;">
                <input type="radio" name="completion" value="phrase" /> Saying a phrase</label>
              <label style="display:flex; gap:5px; align-items:center; cursor:pointer;">
                <input type="radio" name="completion" value="game" /> Playing a game</label>
            </div>
            <div class="field-hint" data-completion-hint></div>
          </div>
          <div class="field" data-trigger-words style="display:none;"><label for="q-words">Phrases That Count</label>
            <textarea name="trigger_words" id="q-words" maxlength="1000" rows="2" placeholder="e.g. good morning, gm"></textarea>
            <div class="field-hint">Separate them with commas or new lines. Capital
              letters do not matter, but the whole phrase has to appear.</div></div>
          <div class="field" data-trigger-channel style="display:none;"><label>Only in This Channel</label>
            <span data-picker="trigger-channel"></span>
            <div class="field-hint">Leave it on "(any channel)" to count anywhere. Pick a
              channel and only messages there, or in its threads, count.</div></div>
          <div class="field" data-trigger-kind style="display:none;"><label for="q-kind">Which Event</label>
            <select name="trigger_kind" id="q-kind">${Object.entries(KIND_LABELS).map(([k, v]) =>
              `<option value="${k}">${esc(v)}</option>`).join("")}
            </select>
            <div class="field-hint" data-kind-hint></div></div>
          <div class="field" data-target-count style="display:none;"><label for="q-target">How Many Times</label>
            <input type="number" name="target_count" id="q-target" min="1" max="10000" step="1" value="1" style="max-width:110px;" />
            <div class="field-hint">On a <strong>daily</strong>, 1 means the first time it
              happens finishes the quest. Anything higher turns it into a counted quest —
              "do this many times this period" — with a progress bar on the member's quest
              card. <strong>Weekly and monthly quests must be counted</strong> (2 or more):
              they show progress across the period, only dailies are one-shot.</div>
            <div class="field-hint" data-target-hint style="color:var(--warn,#b9770e);"></div></div>

          <div class="field">
            <label style="display:flex; gap:6px; align-items:center;">
              <input type="checkbox" name="signoff" /> Have a manager approve it before paying
            </label>
            <div class="field-hint">When checked, finishing the quest files a claim for
              you to approve on the Claims page instead of paying immediately. Use it for
              anything you cannot verify automatically.</div>
          </div>
          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary" data-submit-quest>Create Quest</button>
            <button type="button" class="btn" data-cancel-edit style="display:none;">Cancel</button>
            <span data-status-quest></span>
          </div>
        </form>
        <div class="ai-gen" data-quest-ai style="margin-top:14px;">
          <div class="section-label" style="font-size:.85em;">✨ Need ideas?</div>
          <div class="field-row" style="align-items:flex-end;flex-wrap:wrap;">
            <div class="field"><label for="q-ai-theme">Theme (optional)</label>
              <input type="text" id="q-ai-theme" data-ai-theme maxlength="200" placeholder="e.g. summer event, voice chat, art"
                     style="max-width:260px;" /></div>
            <div class="field"><label for="q-ai-count">How Many Ideas</label>
              <input type="number" id="q-ai-count" data-ai-count min="1" max="10" step="1" value="5" style="max-width:90px;" /></div>
            <div class="field" style="align-self:flex-end;">
              <button type="button" class="btn" data-ai-generate>Generate Ideas</button></div>
          </div>
          <div class="field-hint" style="opacity:.75;">Ideas are written for whichever quest
            type is selected above. Click one to drop it into the form. Nothing is saved
            until you press Create Quest, so you can edit freely first.</div>
          <div data-ai-results></div>
        </div>
      </section>
    </div>`;

  wireAuthoring(container, channels);
  wireBoard(container);
  if (prefetchedQuests) {
    renderQuestList(container, prefetchedQuests);
  } else {
    refreshQuests(container);
  }
}

// ── board size ───────────────────────────────────────────────────────

function wireBoard(container) {
  const form = container.querySelector("[data-form-board]");
  if (!form) return; // manager-role view: read-only, nothing to wire
  guardForm(form);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const status = form.querySelector("[data-status-board]");
    const payload = {};
    for (const [key, , label] of BOARD_FIELDS) {
      const input = form.querySelector(`[name="${key}"]`);
      const n = parseInt(input.value, 10);
      if (!Number.isFinite(n) || n < 0 || n > 25) {
        showStatus(status, false, `${label} Quests Shown must be a number from 0 to 25`);
        input.focus();
        return;
      }
      payload[key] = n;
    }
    try {
      await apiPut("/api/economy/config", payload);
    } catch (err) {
      // showStatus takes (el, ok, msg) — the arguments used to be reversed
      // here, so a failed save rendered a blank success line.
      showStatus(status, false, err.message);
      return;
    }
    // Keep the cached config in step so the library summary's "→ N shown"
    // reflects the save without a reload.
    boardCfg = { ...boardCfg, ...payload };
    showStatus(status, true);
    refreshQuests(container);
  });
}

// ── quest library ────────────────────────────────────────────────────

function questVerification(q) {
  const pay = q.signoff ? "sign-off" : "instant";
  if (q.trigger_kind && KIND_LABELS[q.trigger_kind]) {
    const times = (q.target_count || 1) > 1 ? ` ×${q.target_count}` : "";
    return `<span title="${esc(KIND_LABELS[q.trigger_kind])}">${esc(KIND_LABELS[q.trigger_kind].split(" ")[0])} game trigger${times}</span> · ${pay}`;
  }
  if (q.trigger_words) {
    return `<span title="${esc(q.trigger_words)}">🗣️ phrase</span> · ${pay}`;
  }
  if (q.qtype === "community") return `manager settles · ${pay}`;
  return `member claims · ${pay}`;
}

// "pool → what one member actually sees" per cadence. The old version quoted
// slot caps of 1/5/5 that no longer exist (the cap is POOL_CAP for every
// cadence); pool-vs-board is the number that actually shapes the experience.
function renderSlotSummary(container, quests) {
  const host = container.querySelector("[data-quest-slots]");
  if (!host) return;
  const active = quests.filter((q) => q.active);
  const parts = BOARD_FIELDS.map(([key, qtype, label]) => {
    const pool = active.filter((q) => q.qtype === qtype).length;
    if (!boardCfg) return `${label.toLowerCase()} ${pool} active`;
    if (boardCfg[key] === 0) return `${label.toLowerCase()} ${pool} active (off)`;
    // A board bigger than the pool just means "the whole pool" — show what a
    // member actually sees, not the dial.
    return `${label.toLowerCase()} ${pool} active → ${Math.min(boardCfg[key], pool)} shown`;
  });
  const kinds = active
    .filter((q) => q.qtype === "event" && q.trigger_kind)
    .map((q) => (KIND_LABELS[q.trigger_kind] || q.trigger_kind).split(" ")[0]);
  parts.push(`event ${kinds.length ? kinds.join(" ") : "none"}`);
  host.textContent = `Pool: ${parts.join(" · ")}`;
}

async function refreshQuests(container) {
  const host = container.querySelector("[data-quests]");
  let quests;
  try {
    quests = (await api("/api/economy/quests")).quests;
  } catch (err) {
    host.innerHTML = `<div class="error">The quest library failed to load: ${esc(err.message)}</div>`;
    return;
  }
  renderQuestList(container, quests);
}

// Split out of refreshQuests so the first paint can use the list fetched in
// parallel at mount instead of firing a second request.
function renderQuestList(container, quests) {
  const host = container.querySelector("[data-quests]");
  renderSlotSummary(container, quests);
  if (!quests.length) {
    host.innerHTML = `<div class="empty">No quests yet. Write your first one in the form below.</div>`;
    return;
  }
  const rows = quests.map((q) => {
    const status = `dk-quest-status-${q.id}`;
    return `
      <tr data-quest-row="${q.id}">
        <td>${esc(q.title)}</td>
        <td>${esc(q.qtype)}</td>
        <td>${q.reward}${q.reward_xp > 0 ? ` <span class="field-hint" title="Bonus XP">+${q.reward_xp}xp</span>` : ""}</td>
        <td>${questVerification(q)}</td>
        <td>
          <label style="display:inline-flex; gap:4px; align-items:center;">
            <input type="checkbox" data-active-toggle="${q.id}"${q.active ? " checked" : ""} /> In rotation
          </label>
          <span id="${status}" class="save-status" style="margin-left:6px;"></span>
        </td>
        <td>
          <button class="btn btn-ghost btn-sm" data-edit-quest="${q.id}">Edit</button>
          <button class="btn btn-ghost btn-sm" data-del-quest="${q.id}">Delete</button>
        </td>
      </tr>`;
  }).join("");
  host.innerHTML = `
    <div style="overflow-x:auto;">
      <table class="data-table">
        <thead><tr><th>Title</th><th>Type</th><th>Reward</th><th>How It Completes</th><th>In Rotation</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;

  host.querySelectorAll("[data-edit-quest]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const quest = quests.find((q) => String(q.id) === String(btn.dataset.editQuest));
      if (quest) container.dispatchEvent(new CustomEvent("dk-edit-quest", { detail: quest }));
    });
  });

  host.querySelectorAll("[data-active-toggle]").forEach((cb) => {
    cb.addEventListener("change", async () => {
      const id = cb.dataset.activeToggle;
      const status = host.querySelector(`#dk-quest-status-${id}`);
      try {
        await apiPost(`/api/economy/quests/${id}/active`, { active: cb.checked });
        showStatus(status, true);
        refreshQuests(container);
      } catch (err) {
        cb.checked = !cb.checked; // revert
        showStatus(status, false, err.message);
      }
    });
  });
  host.querySelectorAll("[data-del-quest]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.delQuest;
      const ok = await confirmDialog(
        "The quest is removed for good, along with any turned-down or expired claims "
        + "against it. This cannot be undone. To retire a quest while keeping its "
        + "history, clear \"In rotation\" instead.",
        { title: "Delete this quest?", danger: true, confirmLabel: "Delete" },
      );
      if (!ok) return;
      try {
        await apiDelete(`/api/economy/quests/${id}`);
        toast("Quest deleted", "success");
        refreshQuests(container);
      } catch (err) {
        toast(err.message, "error"); // 409 when paid claims exist
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

function fromEpoch(sec) {
  if (!sec) return "";
  const d = new Date(sec * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const COMPLETION_HINTS = {
  manual: "Members claim it themselves from /bank quests.",
  phrase: "Saying one of the phrases in chat completes it — no manual claim.",
  game: "Completes on its own when the member does this in a game. Daily/weekly: once per period. Event: every single time.",
};

function wireAuthoring(container, channels) {
  const form = container.querySelector("[data-form-quest]");
  const status = form.querySelector("[data-status-quest]");
  const rewardInput = form.querySelector("[name=reward]");
  const qtypeSel = form.querySelector("[name=qtype]");
  const hint = form.querySelector("[data-reward-hint]");
  const typeHint = form.querySelector("[data-type-hint]");
  const communityField = form.querySelector("[data-community-target]");
  const rotateField = form.querySelector("[data-rotate-field]");
  const completionBlock = form.querySelector("[data-completion-block]");
  const completionHint = form.querySelector("[data-completion-hint]");
  const wordsField = form.querySelector("[data-trigger-words]");
  const channelField = form.querySelector("[data-trigger-channel]");
  const kindField = form.querySelector("[data-trigger-kind]");
  const kindHint = form.querySelector("[data-kind-hint]");
  const kindSel = form.querySelector("[name=trigger_kind]");
  const targetField = form.querySelector("[data-target-count]");
  const targetInput = form.querySelector("[name=target_count]");
  const targetHint = form.querySelector("[data-target-hint]");
  const submitBtn = form.querySelector("[data-submit-quest]");
  const cancelBtn = form.querySelector("[data-cancel-edit]");
  const authorLabel = container.querySelector("[data-author-label]");
  const triggerPicker = mountChannelPicker(
    form.querySelector('[data-picker="trigger-channel"]'), channels, "0",
    { emptyLabel: "(any channel)", label: "Only in This Channel" },
  );
  guardForm(form);
  let editingId = null;

  const completion = () =>
    form.querySelector("[name=completion]:checked")?.value || "manual";
  const setCompletion = (value) => {
    const radio = form.querySelector(`[name=completion][value="${value}"]`);
    if (radio) radio.checked = true;
  };

  const updateHint = () => { hint.textContent = bandHint(qtypeSel.value, rewardInput.value); };
  const communityAuto = form.querySelector("[data-community-auto]");
  const updateKindHint = () => {
    const qtype = qtypeSel.value;
    kindHint.textContent = qtype === "event"
      ? "Pays every time it happens — one payout per member per game/card/round."
      : qtype === "community"
        ? "Every member's action counts toward the shared goal — the weekly scheduler activates it, sizes the target, and pays the 40/70/100% tiers."
        : qtype === "monthly"
          ? "Every member's action counts toward one guild-wide goal for the month — the scheduler activates one monthly goal at a time, sizes it, and pays the 40/70/100% tiers at month end."
          : "Auto-completes the quest the first time it happens each period.";
  };
  const updateCommunity = () => {
    const qtype = qtypeSel.value;
    const isCommunity = qtype === "community";
    const isMonthly = qtype === "monthly";
    const isEvent = qtype === "event";
    typeHint.textContent = TYPE_HINTS[qtype] || "";
    rotateField.style.display = qtype === "daily" ? "" : "none";
    // An event quest IS its game trigger; community may be manual (the old
    // Operations-driven goal) or game-triggered (the auto-tracking weekly);
    // monthly is a guild-wide goal that is ALWAYS game-triggered (auto-tracked
    // only). None of these three are ever phrase-completed.
    if (isEvent || isMonthly) setCompletion("game");
    if (isCommunity && completion() === "phrase") setCompletion("manual");
    form.querySelectorAll("[name=completion]").forEach((r) => {
      r.disabled =
        (isEvent && r.value !== "game") ||
        (isMonthly && r.value !== "game") ||
        (isCommunity && r.value === "phrase");
    });
    const mode = completion();
    completionHint.textContent = COMPLETION_HINTS[mode] || "";
    // Manual community goals take a hand-set target; auto-tracking community
    // goals AND all monthly goals are sized by the scheduler at kickoff.
    communityField.style.display = isCommunity && mode !== "game" ? "" : "none";
    communityAuto.style.display =
      (isCommunity && mode === "game") || isMonthly ? "" : "none";
    const channelScoped =
      mode === "phrase" ||
      (mode === "game" && CHANNEL_SCOPED_KINDS.has(kindSel.value));
    wordsField.style.display = mode === "phrase" ? "" : "none";
    channelField.style.display = channelScoped ? "" : "none";
    kindField.style.display = mode === "game" ? "" : "none";
    // The per-member "How Many Times" count only applies to daily/weekly board
    // quests — events pay every occurrence, and community/monthly are measured
    // by the guild-wide auto-sized target instead.
    const countable = mode === "game" && ["daily", "weekly"].includes(qtype);
    targetField.style.display = countable ? "" : "none";
    updateKindHint();
    updateTargetHint();
  };
  // A weekly triggered quest can't be one-shot — the server rejects a target of
  // 1, so warn before the save round-trips (matches _check_target_count).
  const updateTargetHint = () => {
    const oneShot = completion() === "game"
      && qtypeSel.value === "weekly"
      && Number(targetInput.value) <= 1;
    targetHint.textContent = oneShot
      ? "A weekly game quest must count 2 or more — bump this up so it shows progress (only dailies can be one-shot)."
      : "";
  };
  rewardInput.addEventListener("input", updateHint);
  qtypeSel.addEventListener("change", () => { updateHint(); updateCommunity(); });
  kindSel.addEventListener("change", updateCommunity);
  targetInput.addEventListener("input", updateTargetHint);
  form.querySelectorAll("[name=completion]").forEach((r) =>
    r.addEventListener("change", updateCommunity));
  updateHint();
  updateCommunity();

  wireQuestAi(container, form, { updateHint, updateCommunity });

  const exitEditMode = () => {
    editingId = null;
    form.reset();
    triggerPicker.setValue("0");
    authorLabel.textContent = "New Quest";
    submitBtn.textContent = "Create Quest";
    cancelBtn.style.display = "none";
    updateHint();
    updateCommunity();
  };
  cancelBtn.addEventListener("click", exitEditMode);

  container.addEventListener("dk-edit-quest", (e) => {
    const q = e.detail;
    editingId = q.id;
    form.querySelector("[name=title]").value = q.title || "";
    form.querySelector("[name=description]").value = q.description || "";
    form.querySelector("[name=criteria]").value = q.criteria || "";
    qtypeSel.value = q.qtype;
    rewardInput.value = q.reward ?? 0;
    form.querySelector("[name=reward_xp]").value = q.reward_xp ?? 0;
    form.querySelector("[name=signoff]").checked = !!q.signoff;
    form.querySelector("[name=rotate_tag]").value = q.rotate_tag || "";
    form.querySelector("[name=pair_tag]").value = q.pair_tag || "";
    form.querySelector("[name=starts_at]").value = fromEpoch(q.starts_at);
    form.querySelector("[name=ends_at]").value = fromEpoch(q.ends_at);
    form.querySelector("[name=community_target]").value = q.community_target ?? "";
    form.querySelector("[name=trigger_words]").value = q.trigger_words || "";
    triggerPicker.setValue(q.trigger_channel_id || "0");
    if (q.trigger_kind) kindSel.value = q.trigger_kind;
    targetInput.value = q.target_count ?? 1;
    setCompletion(q.trigger_kind ? "game" : (q.trigger_words ? "phrase" : "manual"));
    authorLabel.textContent = `Editing: ${q.title}`;
    submitBtn.textContent = "Save Changes";
    cancelBtn.style.display = "";
    updateHint();
    updateCommunity();
    form.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const qtype = qtypeSel.value;
    const mode = completion();

    // Blank or silly numbers used to slip through as 0 or a 422 naming no
    // field — check them here and say which one is wrong (W-C5).
    const titleInput = form.querySelector("[name=title]");
    if (!titleInput.value.trim()) {
      showStatus(status, false, "Give the quest a Title");
      titleInput.focus();
      return;
    }
    const NUMS = [
      ["reward", "Reward", 0, 1000000],
      ["reward_xp", "Bonus XP", 0, 1000000],
    ];
    const nums = {};
    for (const [name, label, min, max] of NUMS) {
      const input = form.querySelector(`[name=${name}]`);
      const n = parseInt(input.value, 10);
      if (!Number.isFinite(n) || n < min || n > max) {
        showStatus(status, false, `${label} must be a whole number from ${min} to ${max}`);
        input.focus();
        return;
      }
      nums[name] = n;
    }

    const body = {
      title: titleInput.value.trim(),
      description: form.querySelector("[name=description]").value,
      criteria: form.querySelector("[name=criteria]").value,
      qtype,
      reward: nums.reward,
      reward_xp: nums.reward_xp,
      signoff: form.querySelector("[name=signoff]").checked,
      rotate_tag: form.querySelector("[name=rotate_tag]").value.trim(),
      pair_tag: form.querySelector("[name=pair_tag]").value.trim(),
      starts_at: toEpoch(form.querySelector("[name=starts_at]").value),
      ends_at: toEpoch(form.querySelector("[name=ends_at]").value),
      // Always sent so an edit that switches completion mode clears the
      // other mode's fields instead of leaving them behind.
      trigger_words: "",
      trigger_channel_id: null,
      trigger_kind: "",
      target_count: 1,
    };
    if (qtype === "monthly") {
      // Guild-wide monthly goal: always kind-tracked, the scheduler sizes the
      // target at kickoff — no manual target, no per-user count.
      body.trigger_kind = kindSel.value;
      body.community_target = null;
      if (CHANNEL_SCOPED_KINDS.has(kindSel.value)) {
        const trigCh = triggerPicker.getValue();
        body.trigger_channel_id = !trigCh || trigCh === "0" ? null : trigCh;
      }
    } else if (qtype === "community") {
      if (mode === "game") {
        // Auto-tracking weekly: the scheduler sizes the target at kickoff.
        body.trigger_kind = kindSel.value;
        body.community_target = null;
        if (CHANNEL_SCOPED_KINDS.has(kindSel.value)) {
          const trigCh = triggerPicker.getValue();
          body.trigger_channel_id = !trigCh || trigCh === "0" ? null : trigCh;
        }
      } else {
        const targetEl = form.querySelector("[name=community_target]");
        const t = targetEl.value;
        if (t !== "") {
          const n = parseInt(t, 10);
          if (!Number.isFinite(n) || n < 0 || n > 100000000) {
            showStatus(status, false, "Community Target must be a whole number from 0 to 100000000");
            targetEl.focus();
            return;
          }
          body.community_target = n;
        } else {
          body.community_target = null;
        }
      }
    } else if (mode === "game") {
      body.trigger_kind = kindSel.value;
      if (["daily", "weekly"].includes(qtype)) {
        body.target_count = Math.max(1, parseInt(targetInput.value, 10) || 1);
      }
      if (CHANNEL_SCOPED_KINDS.has(kindSel.value)) {
        const trigCh = triggerPicker.getValue();
        body.trigger_channel_id = !trigCh || trigCh === "0" ? null : trigCh;
      }
    } else if (mode === "phrase") {
      body.trigger_words = form.querySelector("[name=trigger_words]").value.trim();
      const trigCh = triggerPicker.getValue();
      body.trigger_channel_id = !trigCh || trigCh === "0" ? null : trigCh;
    }
    try {
      if (editingId != null) {
        await apiPut(`/api/economy/quests/${editingId}`, body);
        showStatus(status, true, "Saved");
      } else {
        await apiPost("/api/economy/quests", body);
        showStatus(status, true, "Created");
      }
      exitEditMode();
      refreshQuests(container);
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
      card.tabIndex = 0;
      card.setAttribute("role", "button");
      card.addEventListener("click", () => loadIdea(idea));
      // Cards are role="button" tabindex="0" — activate with Enter/Space too.
      card.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        e.preventDefault();
        loadIdea(idea);
      });
      results.appendChild(card);
    });
  };

  btn.addEventListener("click", async () => {
    const theme = root.querySelector("[data-ai-theme]").value.trim();
    const count = Math.max(1, Math.min(10, parseInt(root.querySelector("[data-ai-count]").value, 10) || 5));
    const qtype = qtypeSel.value;
    if (qtype === "event") {
      results.innerHTML = `<div class="empty">Event quests have a fixed trigger — no ideas to generate. Pick another type.</div>`;
      return;
    }
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
