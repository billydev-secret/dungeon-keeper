import {
  loadConfig, loadChannels, loadCategories, loadRoles,
  channelSelect, categorySelect, roleSelect,
  apiPut, showStatus,
} from "../config-helpers.js";

const DOW_OPTIONS = [
  { value: "-1", label: "Disabled" },
  { value: "0",  label: "Monday" },
  { value: "1",  label: "Tuesday" },
  { value: "2",  label: "Wednesday" },
  { value: "3",  label: "Thursday" },
  { value: "4",  label: "Friday" },
  { value: "5",  label: "Saturday" },
  { value: "6",  label: "Sunday" },
];

function dowSelect(selected) {
  const sel = String(selected ?? -1);
  return DOW_OPTIONS.map(o =>
    `<option value="${o.value}" ${sel === o.value ? "selected" : ""}>${o.label}</option>`
  ).join("");
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading…</div></div>`;

  (async () => {
    const [config, channels, categories, roles] = await Promise.all([
      loadConfig(), loadChannels(), loadCategories(), loadRoles(),
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
            <div class="field-hint">Where auto-round summaries are posted. Optional.</div>
          </div>

          <div class="field">
            <label>Auto-round Day</label>
            <select name="auto_round_dow" data-dow>${dowSelect(pp.auto_round_dow)}</select>
            <div class="field-hint">Day of the week to automatically drain the pool and pair everyone waiting.</div>
          </div>

          <div class="field" data-hour-field>
            <label>Auto-round UTC Hour</label>
            <input type="number" name="auto_round_hour" min="0" max="23" value="${pp.auto_round_hour ?? 12}" />
            <div class="field-hint">UTC hour (0–23) the auto-round fires.</div>
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
            <div class="field-hint">A member won't be auto-paired again until this long after their last pen pal.</div>
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
      </div>
    `;

    const form = container.querySelector("[data-form]");
    const status = container.querySelector("[data-status]");
    const dowSel = form.querySelector("[data-dow]");
    const hourField = form.querySelector("[data-hour-field]");

    function syncHour() {
      hourField.style.display = dowSel.value === "-1" ? "none" : "";
    }
    syncHour();
    dowSel.addEventListener("change", syncHour);

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
          auto_round_dow:     parseInt(fd.get("auto_round_dow")),
          auto_round_hour:    parseInt(fd.get("auto_round_hour")) || 12,
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
  })();
}
