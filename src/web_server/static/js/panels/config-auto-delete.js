import {
  loadConfig,
  loadChannels,
  channelName,
  apiPut,
  apiDelete,
  showStatus,
  guardForm,
  mountChannelPicker,
  renderMetaWarning,
} from "../config-helpers.js";
import { toast, confirmDialog } from "../ui.js";

const UNITS = [
  { label: "Minutes", seconds: 60 },
  { label: "Hours", seconds: 3600 },
  { label: "Days", seconds: 86400 },
];

function bestUnit(totalSeconds) {
  if (totalSeconds % 86400 === 0) return { value: totalSeconds / 86400, unit: 86400 };
  if (totalSeconds % 3600 === 0) return { value: totalSeconds / 3600, unit: 3600 };
  return { value: Math.round(totalSeconds / 60), unit: 60 };
}

function unitOptions(selectedSeconds) {
  return UNITS.map(
    (u) => `<option value="${u.seconds}"${u.seconds === selectedSeconds ? " selected" : ""}>${u.label}</option>`
  ).join("");
}

function formatTs(ts) {
  if (!ts || ts <= 0) return "Never";
  return new Date(ts * 1000).toLocaleString();
}

// Read a "value + unit" pair into whole seconds, naming the field when the
// number is blank or out of range (W-C5) instead of posting NaN.
function readDuration(fd, prefix, label, statusEl, form) {
  const raw = String(fd.get(`${prefix}_value`) ?? "").trim();
  const value = parseInt(raw, 10);
  if (raw === "" || !Number.isFinite(value) || value < 1) {
    showStatus(statusEl, false, `${label} must be a whole number of 1 or more.`);
    const el = form.querySelector(`[name=${prefix}_value]`);
    if (el) el.focus();
    return null;
  }
  return value * parseInt(fd.get(`${prefix}_unit`), 10);
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading auto-delete schedules…</div></div>`;

  (async () => {
    const [config, channels] = await Promise.all([loadConfig(), loadChannels()]);
    render(container, config.auto_delete || [], channels);
  })();
}

function render(container, rules, channels) {
  let seq = 0;
  function ruleRow(r) {
    const age = bestUnit(r.max_age_seconds);
    const interval = bestUnit(r.interval_seconds);
    const chName = channelName(channels, r.channel_id);
    const uid = `ad-${++seq}`;
    return `
      <form class="form card" style="margin-bottom:16px;" data-channel="${r.channel_id}">
        <div class="section-label">${chName}</div>
        <div class="field-row">
          <div class="field">
            <label for="${uid}-age">Delete Messages Older Than</label>
            <div style="display:flex; gap:4px; flex-wrap:wrap;">
              <input type="number" name="age_value" id="${uid}-age" required min="1" step="1" value="${age.value}" style="max-width:90px;" />
              <select name="age_unit" aria-label="Delete age unit">${unitOptions(age.unit)}</select>
            </div>
            <div class="field-hint">Anything in this channel older than this is deleted permanently — there is no undo.</div>
          </div>
          <div class="field">
            <label for="${uid}-interval">Check This Channel Every</label>
            <div style="display:flex; gap:4px; flex-wrap:wrap;">
              <input type="number" name="interval_value" id="${uid}-interval" required min="1" step="1" value="${interval.value}" style="max-width:90px;" />
              <select name="interval_unit" aria-label="Check interval unit">${unitOptions(interval.unit)}</select>
            </div>
            <div class="field-hint">How often the cleanup sweep runs. A shorter interval deletes closer to the age limit.</div>
          </div>
        </div>
        <div class="field">
          <label style="display:flex; gap:6px; align-items:center;">
            <input type="checkbox" name="media_only"${r.media_only ? " checked" : ""} />
            Only Delete Messages With Attachments
          </label>
          <div class="field-hint">When checked, images, videos, and files are removed but plain text messages are left alone.</div>
        </div>
        <div class="field-hint">Last sweep: ${formatTs(r.last_run_ts)}</div>
        <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
          <button type="submit" class="btn btn-primary">Save</button>
          <button type="button" class="btn btn-danger" data-remove="${r.channel_id}">Remove</button>
          <span data-status></span>
        </div>
      </form>`;
  }

  container.innerHTML = `
    <div class="panel">
      <header>
        <h2>Auto-Delete Schedules</h2>
        <div class="subtitle">Delete old messages from a channel on a repeating schedule</div>
      </header>
      ${renderMetaWarning()}
      <div data-rules>${rules.length
        ? rules.map(ruleRow).join("")
        : '<div class="empty">No channels are being cleaned up yet. Add your first schedule below.</div>'}</div>

      <div class="section-label">Add a Schedule</div>
      <form class="form card" data-add-form>
        <div class="field">
          <label>Channel</label>
          <span data-picker="channel_id"></span>
          <div class="field-hint">The channel to clean up. One schedule per channel — adding a channel that already has a schedule replaces it.</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="ad-new-age">Delete Messages Older Than</label>
            <div style="display:flex; gap:4px; flex-wrap:wrap;">
              <input type="number" name="age_value" id="ad-new-age" required min="1" step="1" value="30" style="max-width:90px;" />
              <select name="age_unit" aria-label="Delete age unit">${unitOptions(86400)}</select>
            </div>
            <div class="field-hint">Anything older than this is deleted permanently — there is no undo.</div>
          </div>
          <div class="field">
            <label for="ad-new-interval">Check This Channel Every</label>
            <div style="display:flex; gap:4px; flex-wrap:wrap;">
              <input type="number" name="interval_value" id="ad-new-interval" required min="1" step="1" value="1" style="max-width:90px;" />
              <select name="interval_unit" aria-label="Check interval unit">${unitOptions(86400)}</select>
            </div>
            <div class="field-hint">How often the cleanup sweep runs.</div>
          </div>
        </div>
        <div class="field">
          <label style="display:flex; gap:6px; align-items:center;">
            <input type="checkbox" name="media_only" />
            Only Delete Messages With Attachments
          </label>
          <div class="field-hint">When checked, images, videos, and files are removed but plain text messages are left alone.</div>
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          <button type="submit" class="btn btn-primary">Add Schedule</button>
          <span data-add-status></span>
        </div>
      </form>
    </div>`;

  // Save handlers for existing rules
  for (const r of rules) {
    const form = container.querySelector(`[data-channel="${r.channel_id}"]`);
    const status = form.querySelector("[data-status]");
    guardForm(form);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const maxAge = readDuration(fd, "age", "Delete Messages Older Than", status, form);
      if (maxAge === null) return;
      const interval = readDuration(fd, "interval", "Check This Channel Every", status, form);
      if (interval === null) return;
      try {
        await apiPut(`/api/config/auto-delete/${r.channel_id}`, {
          max_age_seconds: maxAge,
          interval_seconds: interval,
          media_only: form.querySelector("[name=media_only]").checked,
        });
        showStatus(status, true);
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });
  }

  // Remove handlers
  container.querySelectorAll("[data-remove]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = channelName(channels, btn.dataset.remove);
      const ok = await confirmDialog(
        `Stop auto-deleting messages in ${name}? Messages already deleted cannot be recovered.`,
        { title: "Remove Schedule", danger: true, confirmLabel: "Remove" },
      );
      if (!ok) return;
      try {
        await apiDelete(`/api/config/auto-delete/${btn.dataset.remove}`);
        const fresh = await loadConfig();
        render(container, fresh.auto_delete || [], channels);
      } catch (err) {
        toast(err.message, "error");
      }
    });
  });

  // Add handler
  const addForm = container.querySelector("[data-add-form]");
  const addStatus = container.querySelector("[data-add-status]");
  const addPicker = mountChannelPicker(
    addForm.querySelector('[data-picker="channel_id"]'),
    channels,
    "0",
    { emptyLabel: "(pick a channel)", label: "Channel" },
  );
  guardForm(addForm);
  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(addForm);
    // Snowflakes stay strings; "0" is the unset sentinel the picker returns.
    const channelId = addPicker.getValue() || "0";
    if (channelId === "0") {
      showStatus(addStatus, false, "Pick a channel first.");
      return;
    }
    const maxAge = readDuration(fd, "age", "Delete Messages Older Than", addStatus, addForm);
    if (maxAge === null) return;
    const interval = readDuration(fd, "interval", "Check This Channel Every", addStatus, addForm);
    if (interval === null) return;
    try {
      await apiPut(`/api/config/auto-delete/${channelId}`, {
        max_age_seconds: maxAge,
        interval_seconds: interval,
        media_only: addForm.querySelector("[name=media_only]").checked,
      });
      const fresh = await loadConfig();
      render(container, fresh.auto_delete || [], channels);
    } catch (err) {
      showStatus(addStatus, false, err.message);
    }
  });
}
