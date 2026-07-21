// Economy — Quests. The quest library and the authoring form (plus the AI
// idea generator). Operational work — claim sign-off, community-goal
// settlement, grants, the ledger — lives on the Operations page. Gated by
// the economy manager role (or admin).
import { api, apiPost, apiPut, apiDelete, esc } from "../api.js";
import { showStatus, loadChannels, mountChannelPicker } from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";
import { KIND_LABELS, CHANNEL_SCOPED_KINDS } from "./economy-sources-shared.js";

// Advisory reward bands (client-side hint only — the server saves any value).
const REWARD_BANDS = { daily: [10, 20], weekly: [25, 75], monthly: [75, 200] };

// Plain-language cadence per quest type (shown under the Type select).
const TYPE_HINTS = {
  daily: "Members can complete it once per day (guild-local midnight). Active dailies form a pool; each member is shown a few of them per day — set how many under Board size.",
  weekly: "Members can complete it once per ISO week. Active weeklies form a pool drawn from per member — see Board size.",
  monthly: "Members can complete it once per calendar month (starts on the 1st, guild-local). Active monthlies form a pool drawn from per member — see Board size.",
  community: "One shared goal for the whole server. Manual completion: you track progress and settle from Operations. Game trigger: every member's action counts automatically, the target auto-sizes from recent activity, and the biweekly scheduler runs it with tiered payouts (40/70/100%).",
  event: "Pays by itself every time the trigger happens — no claims, no daily/weekly cap. One active event quest per trigger.",
};

// The per-member board dials. Each entry: [settings key, cadence, label].
const BOARD_FIELDS = [
  ["quest_board_daily", "daily", "Daily"],
  ["quest_board_weekly", "weekly", "Weekly"],
  ["quest_board_monthly", "monthly", "Monthly"],
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
  container.innerHTML = `<div class="panel"><div class="empty">Loading Quests…</div></div>`;
  (async () => {
    const channels = await loadChannels().catch(() => []);
    // Admin probe, same as Income Sources: the config GET is admin-gated, so
    // a success means this user may edit the board sizes. Manager-role
    // holders get a 403 and the read-only view.
    boardCfg = await api("/api/economy/config").catch(() => null);
    render(container, channels, boardCfg);
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
      <label>${label}</label>
      <input type="number" name="${key}" value="${cfg[key]}" min="0" max="25"
             step="1" style="max-width:90px;" />
    </div>`).join("");
  return `
    <div class="field-hint" style="margin-bottom:8px;">
      How many quests of each cadence a member sees at once, drawn from that
      cadence's active pool. Lower this to make the board less busy without
      deactivating quests — a bigger pool with a small board also spaces
      repeats further apart. <strong>0 turns the cadence off entirely</strong>
      (nothing shows, nothing pays).
    </div>
    <form data-form-board class="form">
      <div class="field-row">${fields}</div>
      <div style="display:flex; gap:8px; align-items:center; margin-top:10px;">
        <button type="submit" class="btn btn-primary">Save Board Size</button>
        <span data-status-board></span>
      </div>
    </form>`;
}

function render(container, channels, cfg) {
  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Quests</h2>
        <div class="subtitle">The quest library and authoring — approvals and settlement happen on Operations</div>
      </header>

      <section class="card" data-sec="library">
        <div class="section-label">Quest Library</div>
        <div class="field-hint" style="margin-bottom:8px;">
          Quests are the tunable rewards. Members also earn automatically from
          faucets (daily logins &amp; streaks, XP conversion, game wins, QOTD…) —
          those rates live on <a href="#/economy-income-sources">Income Sources</a>.
          Sign-off claims and community-goal payouts are handled on
          <a href="#/economy-bank-manager">Operations</a>.
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
            <div class="field"><label>Title</label>
              <input type="text" name="title" maxlength="256" required /></div>
            <div class="field"><label>Type</label>
              <select name="qtype">
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
                <option value="community">Community goal</option>
                <option value="event">Event (every time it happens)</option>
              </select>
              <div class="field-hint" data-type-hint style="max-width:420px;"></div></div>
          </div>
          <div class="field"><label>Description</label>
            <textarea name="description" maxlength="2000" rows="2"></textarea></div>
          <div class="field"><label>Criteria (shown on the claim card)</label>
            <textarea name="criteria" maxlength="2000" rows="2"></textarea></div>
          <div class="field-row">
            <div class="field"><label>Reward (coins)</label>
              <input type="number" name="reward" min="0" step="1" value="10" style="max-width:120px;" />
              <div class="field-hint" data-reward-hint style="color:#d9a441;"></div></div>
            <div class="field"><label>Bonus XP</label>
              <input type="number" name="reward_xp" min="0" step="1" value="0" style="max-width:120px;" />
              <div class="field-hint">Levelling XP paid with the coins (no booster multiplier).</div></div>
            <div class="field" data-community-target style="display:none;"><label>Community target</label>
              <input type="number" name="community_target" min="0" step="1" style="max-width:120px;" /></div>
            <div class="field" data-community-auto style="display:none;"><label>Community target</label>
              <div class="field-hint">Auto-sized when the scheduler kicks the run off — a typical week lands ~75%, a push clears it. No manual override.</div></div>
            <div class="field" data-rotate-field><label>Rotate tag</label>
              <input type="text" name="rotate_tag" maxlength="64" style="max-width:160px;" /></div>
            <div class="field"><label>Pair tag</label>
              <input type="text" name="pair_tag" maxlength="64" style="max-width:160px;" />
              <div class="field-hint">Exactly two active quests of the same cadence sharing a tag land on boards together (e.g. host + play).</div></div>
          </div>
          <div class="field-row">
            <div class="field"><label>Starts (optional)</label>
              <input type="datetime-local" name="starts_at" /></div>
            <div class="field"><label>Ends (optional)</label>
              <input type="datetime-local" name="ends_at" /></div>
          </div>

          <div class="field" data-completion-block>
            <label>How it completes</label>
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
          <div class="field" data-trigger-words style="display:none;"><label>Trigger words</label>
            <textarea name="trigger_words" maxlength="1000" rows="2" placeholder="e.g. good morning, gm"></textarea>
            <div class="field-hint">Comma or newline separated; whole-phrase, case-insensitive.</div></div>
          <div class="field" data-trigger-channel style="display:none;"><label>Trigger channel</label>
            <span data-picker="trigger-channel"></span>
            <div class="field-hint">If set, only messages in this channel (or its threads) count.</div></div>
          <div class="field" data-trigger-kind style="display:none;"><label>Game trigger</label>
            <select name="trigger_kind">${Object.entries(KIND_LABELS).map(([k, v]) =>
              `<option value="${k}">${esc(v)}</option>`).join("")}
            </select>
            <div class="field-hint" data-kind-hint></div></div>
          <div class="field" data-target-count style="display:none;"><label>How many times</label>
            <input type="number" name="target_count" min="1" max="10000" step="1" value="1" style="max-width:110px;" />
            <div class="field-hint">1 = the first occurrence completes it. Higher = a counted quest ("do it N times this period") with a progress bar on /quests.</div></div>

          <label style="display:flex; gap:6px; align-items:center; margin:8px 0;">
            <input type="checkbox" name="signoff" /> Requires manager sign-off
            <span class="field-hint" style="margin:0;">(completion files a claim you approve on Operations instead of paying instantly)</span>
          </label>
          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary" data-submit-quest>Create Quest</button>
            <button type="button" class="btn" data-cancel-edit style="display:none;">Cancel Edit</button>
            <span data-status-quest></span>
          </div>
        </form>
        <div class="ai-gen" data-quest-ai style="margin-top:14px;">
          <div class="section-label" style="font-size:.85em;">✨ Need ideas?</div>
          <div class="field-row" style="align-items:flex-end;">
            <div class="field"><label>AI idea theme (optional)</label>
              <input type="text" data-ai-theme maxlength="200" placeholder="e.g. summer event, voice chat, art"
                     style="max-width:260px;" /></div>
            <div class="field"><label>How many</label>
              <input type="number" data-ai-count min="1" max="10" step="1" value="5" style="max-width:90px;" /></div>
            <div class="field" style="align-self:flex-end;">
              <button type="button" class="btn" data-ai-generate>Generate Ideas</button></div>
          </div>
          <div class="field-hint" style="opacity:.75;">Ideas use the quest type selected above. Click one to load it into the form — nothing is saved until you create it.</div>
          <div data-ai-results></div>
        </div>
      </section>
    </div>`;

  wireAuthoring(container, channels);
  wireBoard(container);
  refreshQuests(container);
}

// ── board size ───────────────────────────────────────────────────────

function wireBoard(container) {
  const form = container.querySelector("[data-form-board]");
  if (!form) return; // manager-role view: read-only, nothing to wire
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const status = form.querySelector("[data-status-board]");
    const payload = {};
    for (const [key] of BOARD_FIELDS) {
      payload[key] = Number(form.querySelector(`[name="${key}"]`).value);
    }
    try {
      await apiPut("/api/economy/config", payload);
    } catch (err) {
      showStatus(status, err.message, false);
      return;
    }
    // Keep the cached config in step so the library summary's "→ N shown"
    // reflects the save without a reload.
    boardCfg = { ...boardCfg, ...payload };
    showStatus(status, "Saved", true);
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
    host.innerHTML = `<div class="error">${esc(err.message)}</div>`;
    return;
  }
  renderSlotSummary(container, quests);
  if (!quests.length) {
    host.innerHTML = `<div class="empty">No quests yet — create one below.</div>`;
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
            <input type="checkbox" data-active-toggle="${q.id}"${q.active ? " checked" : ""} /> active
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
        <thead><tr><th>Title</th><th>Type</th><th>Reward</th><th>How it completes</th><th>Active</th><th></th></tr></thead>
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
      if (!(await confirmDialog("Delete this quest? Denied/expired claim history goes with it.", { danger: true, confirmLabel: "Delete" }))) return;
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
  manual: "Members claim it themselves from /bank quests (or /quests).",
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
  const submitBtn = form.querySelector("[data-submit-quest]");
  const cancelBtn = form.querySelector("[data-cancel-edit]");
  const authorLabel = container.querySelector("[data-author-label]");
  const triggerPicker = mountChannelPicker(
    form.querySelector('[data-picker="trigger-channel"]'), channels, "0",
    { emptyLabel: "(any channel)" },
  );
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
        ? "Every member's action counts toward the shared goal — the biweekly scheduler activates it, sizes the target, and pays the 40/70/100% tiers."
        : "Auto-completes the quest the first time it happens each period.";
  };
  const updateCommunity = () => {
    const qtype = qtypeSel.value;
    const isCommunity = qtype === "community";
    const isEvent = qtype === "event";
    typeHint.textContent = TYPE_HINTS[qtype] || "";
    rotateField.style.display = qtype === "daily" ? "" : "none";
    // An event quest IS its game trigger; community may be manual (the old
    // Operations-driven goal) or game-triggered (the auto-tracking weekly)
    // but never phrase-completed.
    if (isEvent) setCompletion("game");
    if (isCommunity && completion() === "phrase") setCompletion("manual");
    form.querySelectorAll("[name=completion]").forEach((r) => {
      r.disabled =
        (isEvent && r.value !== "game") ||
        (isCommunity && r.value === "phrase");
    });
    const mode = completion();
    completionHint.textContent = COMPLETION_HINTS[mode] || "";
    // Manual community goals take a hand-set target; auto-tracking ones are
    // sized by the scheduler at kickoff.
    communityField.style.display = isCommunity && mode !== "game" ? "" : "none";
    communityAuto.style.display = isCommunity && mode === "game" ? "" : "none";
    const channelScoped =
      mode === "phrase" ||
      (mode === "game" && CHANNEL_SCOPED_KINDS.has(kindSel.value));
    wordsField.style.display = mode === "phrase" ? "" : "none";
    channelField.style.display = channelScoped ? "" : "none";
    kindField.style.display = mode === "game" ? "" : "none";
    // Counted quests need a trigger to count and a calendar cadence to
    // count within — events pay every occurrence, no target; a community
    // counter has the guild-wide target instead.
    const countable = mode === "game" && ["daily", "weekly", "monthly"].includes(qtype);
    targetField.style.display = countable ? "" : "none";
    updateKindHint();
  };
  rewardInput.addEventListener("input", updateHint);
  qtypeSel.addEventListener("change", () => { updateHint(); updateCommunity(); });
  kindSel.addEventListener("change", updateCommunity);
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
    const body = {
      title: form.querySelector("[name=title]").value.trim(),
      description: form.querySelector("[name=description]").value,
      criteria: form.querySelector("[name=criteria]").value,
      qtype,
      reward: parseInt(rewardInput.value, 10) || 0,
      reward_xp: parseInt(form.querySelector("[name=reward_xp]").value, 10) || 0,
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
    if (qtype === "community") {
      if (mode === "game") {
        // Auto-tracking weekly: the scheduler sizes the target at kickoff.
        body.trigger_kind = kindSel.value;
        body.community_target = null;
        if (CHANNEL_SCOPED_KINDS.has(kindSel.value)) {
          const trigCh = triggerPicker.getValue();
          body.trigger_channel_id = !trigCh || trigCh === "0" ? null : trigCh;
        }
      } else {
        const t = form.querySelector("[name=community_target]").value;
        body.community_target = t === "" ? null : parseInt(t, 10);
      }
    } else if (mode === "game") {
      body.trigger_kind = kindSel.value;
      if (["daily", "weekly", "monthly"].includes(qtype)) {
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
      card.addEventListener("click", () => loadIdea(idea));
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
