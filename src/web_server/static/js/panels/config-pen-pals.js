import {
  loadConfig, loadChannels, loadCategories, loadRoles, loadMembers,
  toMemberOptions, mountPicker, mountChannelPicker, mountRolePicker, mountCategoryPicker,
  apiPut, showStatus, esc, guardForm, renderMetaWarning,
} from "../config-helpers.js";
import { confirmDialog } from "../ui.js";

// Timer fields: [input name, visible label, minimum, multiplier to seconds]
const TIMER_FIELDS = [
  ["session_hours", "Session Length", 1, 3600],
  ["match_cooldown_days", "Wait Before Matching Again", 0, 86400],
  ["max_question_swaps", "Question Swaps Allowed", 0, 1],
  ["warn_minutes", "Closing-Soon Warning", 0, 60],
  ["question_suppress_minutes", "Stop New Questions When Less Than", 0, 60],
];

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading Pen Pals settings…</div></div>`;

  (async () => {
    const [config, channels, categories, roles, members] = await Promise.all([
      loadConfig(), loadChannels(), loadCategories(), loadRoles(), loadMembers(),
    ]);
    const pp = config.pen_pals || {};

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Pen Pals</h2>
          <div class="subtitle">Private one-day channels that match two members and give them something to talk about · ${pp.pool_size ?? 0} member${(pp.pool_size ?? 0) === 1 ? "" : "s"} waiting to be matched</div>
        </header>
        ${renderMetaWarning()}

        <div data-off-banner class="field-hint" role="status"
          style="border:1px solid var(--rule); border-radius:6px; padding:10px; margin-bottom:14px; line-height:1.5;">
          Pen Pals is currently off — nothing on this page takes effect until you check
          <a href="#" data-focus-toggle>Run Pen Pals</a> and save.
        </div>

        <form class="form form-cards" data-form>
          <div class="card">
            <div class="section-label">Setup</div>
            <div class="field">
              <label style="display:flex; gap:6px; align-items:center;">
                <input type="checkbox" name="enabled" ${pp.enabled ? "checked" : ""} /> Run Pen Pals
              </label>
              <div class="field-hint">When unchecked, nobody can join the pool and no new pairs are made. Chats already open are left to finish normally.</div>
            </div>

            <div class="field">
              <label>Channel Category</label>
              <span data-picker="category_id"></span>
              <div class="field-hint">Every pen pal chat is created as a private channel inside this category. Required — without it no chat can be created.</div>
            </div>

            <div class="field">
              <label>Members Must Have This Role</label>
              <span data-picker="opt_in_role_id"></span>
              <div class="field-hint">Only holders of this role can join the pool. Choose "(none)" to let everyone in.</div>
            </div>

            <div class="field">
              <label for="pp-question-category">Question Ratings</label>
              <select name="question_category" id="pp-question-category">
                <option value="sfw" ${(pp.question_category || "sfw") === "sfw" ? "selected" : ""}>Safe for work only</option>
                <option value="all" ${pp.question_category === "all" ? "selected" : ""}>Everything, including adult questions</option>
              </select>
              <div class="field-hint">Which conversation starters the bot may post. Adult questions are only appropriate where your category's channels are age-gated.</div>
            </div>

            <div class="field">
              <label>Log Channel</label>
              <span data-picker="log_channel_id"></span>
              <div class="field-hint">Posts a line here each time two members are paired, so moderators can keep an eye on it. "(disabled)" logs nothing.</div>
            </div>

            <div class="field">
              <label>Signup Panel Channel</label>
              <span data-picker="panel_channel_id"></span>
              <div class="field-hint">A Join / Leave panel is kept posted here for members to sign up. Changing the channel moves the panel for you. "(disabled)" removes it, leaving only the slash commands.</div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-status></span>
          </div>
        </form>

        <form class="form form-cards" data-timers-form>
          <div class="card">
            <div class="section-label">Timing &amp; Limits</div>
            <div class="field">
              <label for="pp-session_hours">Session Length (hours)</label>
              <input type="number" name="session_hours" id="pp-session_hours" required min="1" step="1" value="${Math.round((pp.session_seconds ?? 86400) / 3600)}" style="max-width:140px;" />
              <div class="field-hint">How long a pair's channel stays open. When the time is up the channel and its messages are deleted.</div>
            </div>

            <div class="field">
              <label for="pp-match_cooldown_days">Wait Before Matching Again (days)</label>
              <input type="number" name="match_cooldown_days" id="pp-match_cooldown_days" required min="0" step="1" value="${Math.round((pp.match_cooldown_seconds ?? 2592000) / 86400)}" style="max-width:140px;" />
              <div class="field-hint">A member isn't paired again until this long after their last chat began. Enter 0 to allow back-to-back chats. Either way nobody is ever in two chats at once.</div>
            </div>

            <div class="field">
              <label for="pp-max_question_swaps">Question Swaps Allowed</label>
              <input type="number" name="max_question_swaps" id="pp-max_question_swaps" required min="0" step="1" value="${pp.max_question_swaps ?? 3}" style="max-width:140px;" />
              <div class="field-hint">How many times a pair may ask for a different conversation starter during one session. Enter 0 to give them no swaps.</div>
            </div>

            <div class="field">
              <label for="pp-warn_minutes">Closing-Soon Warning (minutes)</label>
              <input type="number" name="warn_minutes" id="pp-warn_minutes" required min="0" step="1" value="${Math.round((pp.warn_seconds ?? 3600) / 60)}" style="max-width:140px;" />
              <div class="field-hint">A heads-up is posted in the chat when this much time is left, so nobody is caught out by the channel disappearing. Enter 0 for no warning.</div>
            </div>

            <div class="field">
              <label for="pp-question_suppress_minutes">Stop New Questions When Less Than (minutes)</label>
              <input type="number" name="question_suppress_minutes" id="pp-question_suppress_minutes" required min="0" step="1" value="${Math.round((pp.question_suppress_seconds ?? 7200) / 60)}" style="max-width:140px;" />
              <div class="field-hint">Near the end of a session, a fresh question just goes unanswered — no new one is posted once the time left drops below this. Enter 0 to keep posting until the end.</div>
            </div>
          </div>

          <div style="display:flex; gap:8px; align-items:center;">
            <button type="submit" class="btn btn-primary">Save</button>
            <span data-timers-status></span>
          </div>
        </form>

        <div class="section-label">Members Who Must Never Be Matched</div>
        <div class="card">
          <div class="field-hint" style="margin-bottom:10px;">
            Pairs listed here are never matched with each other — use it for separations your
            moderators have decided on. Members can also block people themselves with
            <code>/penpals block</code>; those personal blocks are private and don't appear here.
            Changes here save straight away.
          </div>
          <div class="field" style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;">
            <div style="flex:1 1 200px;min-width:0;">
              <label>First Member</label>
              <span data-picker="sep_a"></span>
            </div>
            <div style="flex:1 1 200px;min-width:0;">
              <label>Second Member</label>
              <span data-picker="sep_b"></span>
            </div>
            <button type="button" class="btn btn-primary" data-add-sep>Add Separation</button>
          </div>
          <div data-sep-list style="display:flex;flex-direction:column;gap:6px;"></div>
          <span data-sep-status></span>
        </div>
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");

    // Searchable pickers replace the old plain <select>s (W-C4). "0" stays the
    // unset sentinel so the posted payload is byte-identical to before.
    const categoryPicker = mountCategoryPicker(
      form.querySelector('[data-picker="category_id"]'), categories,
      String(pp.category_id || "0"), { label: "Channel Category" },
    );
    const optInPicker = mountRolePicker(
      form.querySelector('[data-picker="opt_in_role_id"]'), roles,
      String(pp.opt_in_role_id || "0"), { label: "Members Must Have This Role" },
    );
    const logPicker = mountChannelPicker(
      form.querySelector('[data-picker="log_channel_id"]'), channels,
      String(pp.log_channel_id || "0"), { label: "Log Channel" },
    );
    const panelPicker = mountChannelPicker(
      form.querySelector('[data-picker="panel_channel_id"]'), channels,
      String(pp.panel_channel_id || "0"), { label: "Signup Panel Channel" },
    );

    // W-C6: the master toggle now visibly gates everything under it.
    const enabledBox = form.querySelector('input[name="enabled"]');
    const offBanner = container.querySelector("[data-off-banner]");
    function syncBanner() {
      offBanner.style.display = enabledBox.checked ? "none" : "";
    }
    enabledBox.addEventListener("change", syncBanner);
    syncBanner();
    container.querySelector("[data-focus-toggle]").addEventListener("click", (e) => {
      e.preventDefault();
      enabledBox.focus();
      enabledBox.scrollIntoView({ block: "center", behavior: "smooth" });
    });

    guardForm(form);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      // Warn — but don't block — when the feature can't work as configured.
      if (enabledBox.checked && (categoryPicker.getValue() || "0") === "0") {
        showStatus(status, false, "Pick a Channel Category — Pen Pals has nowhere to create chats without one.");
        return;
      }
      try {
        await apiPut("/api/config/pen-pals", {
          enabled:            enabledBox.checked,
          category_id:        categoryPicker.getValue() || null,
          opt_in_role_id:     optInPicker.getValue() || null,
          question_category:  fd.get("question_category"),
          log_channel_id:     logPicker.getValue() || null,
          panel_channel_id:   panelPicker.getValue() || null,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    const timersForm = container.querySelector("[data-timers-form]");
    const timersStatus = container.querySelector("[data-timers-status]");
    guardForm(timersForm);

    timersForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(timersForm);
      // Validate before posting: a blank box used to silently fall back to a
      // default the user never chose (W-C5).
      const vals = {};
      for (const [name, label, min] of TIMER_FIELDS) {
        const raw = String(fd.get(name) ?? "").trim();
        const v = parseInt(raw, 10);
        if (raw === "" || !Number.isFinite(v) || v < min) {
          showStatus(timersStatus, false, `${label} must be a whole number of ${min} or more.`);
          timersForm.querySelector(`[name="${name}"]`).focus();
          return;
        }
        vals[name] = v;
      }
      try {
        await apiPut("/api/config/pen-pals/timers", {
          session_seconds:            vals.session_hours * 3600,
          match_cooldown_seconds:     vals.match_cooldown_days * 86400,
          max_question_swaps:         vals.max_question_swaps,
          warn_seconds:               vals.warn_minutes * 60,
          question_suppress_seconds:  vals.question_suppress_minutes * 60,
        });
        showStatus(timersStatus, true);
      } catch (err) {
        showStatus(timersStatus, false, err.message);
      }
    });

    // ── Never-match separations ──────────────────────────────────────
    const memberName = (id) => {
      const m = members.find((x) => String(x.id) === String(id));
      if (!m) return `Member ${id}`;
      return m.display_name && m.display_name !== m.name ? m.display_name : m.name;
    };

    let separations = (pp.separations || []).map((s) => ({
      user_a: String(s.user_a), user_b: String(s.user_b),
    }));
    const memberOpts = toMemberOptions(members);
    const pickerA = mountPicker(container.querySelector('[data-picker="sep_a"]'),
      memberOpts, "0", { emptyValue: "0", emptyLabel: "(pick a member)", placeholder: "Search members…", label: "First Member" });
    const pickerB = mountPicker(container.querySelector('[data-picker="sep_b"]'),
      memberOpts, "0", { emptyValue: "0", emptyLabel: "(pick a member)", placeholder: "Search members…", label: "Second Member" });
    const sepList = container.querySelector("[data-sep-list]");
    const sepStatus = container.querySelector("[data-sep-status]");

    const renderSeps = () => {
      if (!separations.length) {
        sepList.innerHTML = `<div class="empty">No separations yet — any two members may be matched.</div>`;
        return;
      }
      sepList.innerHTML = separations.map((s, i) => `
        <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;padding:6px 10px;border:1px solid var(--border,#333);border-radius:6px;">
          <span>${esc(memberName(s.user_a))} <span style="opacity:.6;" aria-label="never matched with">⊘</span> ${esc(memberName(s.user_b))}</span>
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
      // Member ids stay strings.
      const a = pickerA.getValue(), b = pickerB.getValue();
      if (!a || a === "0" || !b || b === "0") {
        showStatus(sepStatus, false, "Pick a member in both boxes.");
        return;
      }
      if (a === b) {
        showStatus(sepStatus, false, "Pick two different members.");
        return;
      }
      const pair = { user_a: a, user_b: b };
      if (separations.some((s) => samePair(s, pair))) {
        showStatus(sepStatus, false, "Those two are already kept apart.");
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
      const idx = parseInt(btn.dataset.removeSep, 10);
      const pair = separations[idx];
      if (!pair) return;
      const ok = await confirmDialog(
        `Allow ${memberName(pair.user_a)} and ${memberName(pair.user_b)} to be matched again? `
        + "Pen Pals may pair them with each other from now on.",
        { title: "Remove Separation", danger: true, confirmLabel: "Remove Separation" },
      );
      if (!ok) return;
      separations.splice(idx, 1);
      renderSeps();
      await persistSeps();
    });

    renderSeps();
  })();
}
