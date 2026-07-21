import {
  loadConfig, loadChannels, loadCategories, loadRoles, loadMembers,
  channelSelect, categorySelect, roleSelect,
  toMemberOptions, mountPicker,
  apiPut, showStatus, esc,
} from "../config-helpers.js";

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading…</div></div>`;

  (async () => {
    const [config, channels, categories, roles, members] = await Promise.all([
      loadConfig(), loadChannels(), loadCategories(), loadRoles(), loadMembers(),
    ]);
    const pp = config.pen_pals || {};

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Pen Pals</h2>
          <div class="subtitle">Private 24-hour matched channels with prompted questions · ${pp.pool_size ?? 0} member${(pp.pool_size ?? 0) === 1 ? "" : "s"} waiting in pool</div>
        </header>
        <form class="form" data-form>

          <div class="field">
            <label>Enabled</label>
            <select name="enabled">
              <option value="1" ${pp.enabled ? "selected" : ""}>On</option>
              <option value="0" ${!pp.enabled ? "selected" : ""}>Off</option>
            </select>
          </div>

          <div class="field">
            <label>Channel Category</label>
            <select name="category_id">${categorySelect(categories, pp.category_id)}</select>
            <div class="field-hint">Discord category where pen pal channels are created. Required.</div>
          </div>

          <div class="field">
            <label>Opt-in Role</label>
            <select name="opt_in_role_id">${roleSelect(roles, pp.opt_in_role_id)}</select>
            <div class="field-hint">If set, only members with this role can join. Leave blank to allow everyone.</div>
          </div>

          <div class="field">
            <label>Question Category</label>
            <select name="question_category">
              <option value="sfw" ${(pp.question_category || "sfw") === "sfw" ? "selected" : ""}>SFW only</option>
              <option value="all" ${pp.question_category === "all" ? "selected" : ""}>All (including NSFW)</option>
            </select>
          </div>

          <div class="field">
            <label>Log Channel</label>
            <select name="log_channel_id">${channelSelect(channels, pp.log_channel_id)}</select>
            <div class="field-hint">Where pairing confirmations are posted. Optional.</div>
          </div>

          <div class="field">
            <label>Signup Panel Channel</label>
            <select name="panel_channel_id">${channelSelect(channels, pp.panel_channel_id)}</select>
            <div class="field-hint">A persistent Join / Leave panel is posted here. Changing this channel moves the panel automatically.</div>
          </div>

          <div><button type="submit" class="btn btn-primary">Save</button><span data-status></span></div>
        </form>

        <header>
          <h2>Pairing Mechanics</h2>
          <div class="subtitle">Session timing and question-swap limits</div>
        </header>
        <form class="form" data-timers-form>

          <div class="field">
            <label>Session Length (hours)</label>
            <input type="number" name="session_hours" min="1" step="1" value="${Math.round((pp.session_seconds ?? 86400) / 3600)}" />
            <div class="field-hint">How long a matched channel stays open before it's torn down.</div>
          </div>

          <div class="field">
            <label>Re-match Cooldown (days)</label>
            <input type="number" name="match_cooldown_days" min="0" step="1" value="${Math.round((pp.match_cooldown_seconds ?? 2592000) / 86400)}" />
            <div class="field-hint">A member won't be paired again until this long after their last pen pal started — set 0 to allow back-to-back chats. Nobody is ever in two chats at once regardless.</div>
          </div>

          <div class="field">
            <label>Max Question Swaps</label>
            <input type="number" name="max_question_swaps" min="0" step="1" value="${pp.max_question_swaps ?? 3}" />
            <div class="field-hint">How many times a pair can swap the conversation-starter question per session.</div>
          </div>

          <div class="field">
            <label>Close Warning (minutes)</label>
            <input type="number" name="warn_minutes" min="0" step="1" value="${Math.round((pp.warn_seconds ?? 3600) / 60)}" />
            <div class="field-hint">Post a "closing soon" notice when this much session time remains.</div>
          </div>

          <div class="field">
            <label>Question Suppress Window (minutes)</label>
            <input type="number" name="question_suppress_minutes" min="0" step="1" value="${Math.round((pp.question_suppress_seconds ?? 7200) / 60)}" />
            <div class="field-hint">Skip posting a new auto-question if less than this much session time remains.</div>
          </div>

          <div><button type="submit" class="btn btn-primary">Save</button><span data-timers-status></span></div>
        </form>

        <header>
          <h2>Never-match separations</h2>
          <div class="subtitle">Pairs of members Pen Pals must never match together — for mod-enforced separations. Members can also block people themselves with <code>/penpals block</code>; those personal blocks don't show here.</div>
        </header>
        <div class="field" style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;">
          <div style="flex:1 1 200px;min-width:0;">
            <label>Member A</label>
            <span data-picker="sep_a"></span>
          </div>
          <div style="flex:1 1 200px;min-width:0;">
            <label>Member B</label>
            <span data-picker="sep_b"></span>
          </div>
          <button type="button" class="btn btn-primary" data-add-sep>Add Separation</button>
        </div>
        <div data-sep-list style="display:flex;flex-direction:column;gap:6px;"></div>
        <span data-sep-status></span>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await apiPut("/api/config/pen-pals", {
          enabled:            fd.get("enabled") === "1",
          category_id:        fd.get("category_id") || null,
          opt_in_role_id:     fd.get("opt_in_role_id") || null,
          question_category:  fd.get("question_category"),
          log_channel_id:     fd.get("log_channel_id") || null,
          panel_channel_id:   fd.get("panel_channel_id") || null,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    const timersForm = container.querySelector("[data-timers-form]");
    const timersStatus = container.querySelector("[data-timers-status]");

    timersForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(timersForm);
      try {
        await apiPut("/api/config/pen-pals/timers", {
          session_seconds:            (parseInt(fd.get("session_hours")) || 24) * 3600,
          match_cooldown_seconds:     (parseInt(fd.get("match_cooldown_days")) || 0) * 86400,
          max_question_swaps:         parseInt(fd.get("max_question_swaps")) || 0,
          warn_seconds:               (parseInt(fd.get("warn_minutes")) || 0) * 60,
          question_suppress_seconds: (parseInt(fd.get("question_suppress_minutes")) || 0) * 60,
        });
        showStatus(timersStatus, true);
      } catch (err) {
        showStatus(timersStatus, false, err.message);
      }
    });

    // ── Never-match separations ──────────────────────────────────────
    const memberName = (id) => {
      const m = members.find((x) => String(x.id) === String(id));
      if (!m) return `User ${id}`;
      return m.display_name && m.display_name !== m.name ? m.display_name : m.name;
    };

    let separations = (pp.separations || []).map((s) => ({
      user_a: String(s.user_a), user_b: String(s.user_b),
    }));
    const memberOpts = toMemberOptions(members);
    const pickerA = mountPicker(container.querySelector('[data-picker="sep_a"]'),
      memberOpts, "0", { emptyValue: "0", emptyLabel: "(pick a member)", placeholder: "Search members…" });
    const pickerB = mountPicker(container.querySelector('[data-picker="sep_b"]'),
      memberOpts, "0", { emptyValue: "0", emptyLabel: "(pick a member)", placeholder: "Search members…" });
    const sepList = container.querySelector("[data-sep-list]");
    const sepStatus = container.querySelector("[data-sep-status]");

    const renderSeps = () => {
      if (!separations.length) {
        sepList.innerHTML = `<div class="empty">No separations yet. Members are matched freely.</div>`;
        return;
      }
      sepList.innerHTML = separations.map((s, i) => `
        <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;padding:6px 10px;border:1px solid var(--border,#333);border-radius:6px;">
          <span>${esc(memberName(s.user_a))} <span style="opacity:.6;">⊘</span> ${esc(memberName(s.user_b))}</span>
          <button type="button" class="btn btn-small" data-remove-sep="${i}">Remove</button>
        </div>
      `).join("");
    };

    const samePair = (x, y) =>
      (x.user_a === y.user_a && x.user_b === y.user_b) ||
      (x.user_a === y.user_b && x.user_b === y.user_a);

    const persistSeps = async () => {
      try {
        await apiPut("/api/config/pen-pals/separations", { separations });
        showStatus(sepStatus, true, "Saved");
      } catch (err) {
        showStatus(sepStatus, false, err.message);
      }
    };

    container.querySelector("[data-add-sep]").addEventListener("click", async () => {
      const a = pickerA.getValue(), b = pickerB.getValue();
      if (!a || a === "0" || !b || b === "0") {
        showStatus(sepStatus, false, "Pick two members");
        return;
      }
      if (a === b) {
        showStatus(sepStatus, false, "Pick two different members");
        return;
      }
      const pair = { user_a: a, user_b: b };
      if (separations.some((s) => samePair(s, pair))) {
        showStatus(sepStatus, false, "Already separated");
        return;
      }
      separations.push(pair);
      renderSeps();
      pickerA.setValue("0");
      pickerB.setValue("0");
      await persistSeps();
    });

    sepList.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-remove-sep]");
      if (!btn) return;
      separations.splice(parseInt(btn.dataset.removeSep), 1);
      renderSeps();
      await persistSeps();
    });

    renderSeps();
  })();
}
